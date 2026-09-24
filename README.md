# KYC Onboarding & Compliance Review Desk

> ## ⚠ Demo — synthetic data, not client work
>
> Every applicant, document result and sanctions match in this repository is
> **invented**. No real person's data has ever been in it. It is not a product,
> it was not built for a client, and nothing here has processed a real
> customer.
>
> It exists to demonstrate an approach to a specific problem: **how a
> regulated onboarding system stays correct when the things it depends on are
> unreliable.**

[![tests](https://github.com/Denxhinjo/kyc-compliance-desk/actions/workflows/tests.yml/badge.svg)](https://github.com/Denxhinjo/kyc-compliance-desk/actions/workflows/tests.yml)

A customer identity-verification flow (KYC/AML) and the back-office desk where
a compliance officer handles the cases that cannot be decided automatically.

---

## Contents

- [The problem this is really about](#the-problem-this-is-really-about)
- [Architecture](#architecture)
- [Running it locally](#running-it-locally)
- [Deploying](#deploying)
- [What each part does](#what-each-part-does)
- [Numbers](#numbers)
- [Limitations — what this does not do](#limitations--what-this-does-not-do)
- [Reading the code](#reading-the-code)

---

## The problem this is really about

A KYC system is mostly a coordination problem wearing a compliance costume.
You depend on an identity-verification vendor you do not control, over a
network that loses messages, to produce a result you are legally required to
act on. Everything difficult follows from that.

### Messages arrive twice, out of order, or never

The vendor sends a webhook when a check completes. That webhook is a delivery
*attempt*, not a guarantee.

**It will arrive twice.** Not occasionally — routinely. The vendor cannot
distinguish "my request never arrived" from "your response was lost on the way
back": both look identical from their side, so resending is the only safe
choice. Add your own deploys, 500s and timeouts and duplicates stop being an
edge case and become a certainty.

**It will arrive out of order.** Two results sent seconds apart can arrive in
either sequence. "Apply what the message says" is therefore a bug: the last
message to arrive wins, whether or not it is the newest.

**Sometimes it will not arrive at all.** Didit retries twice — roughly one
minute and four minutes — then drops the delivery permanently. A five-minute
deploy, a database blip, an expired tunnel, and the vendor has a verdict you
will never hear. Without a recovery path, the applicant waits forever and the
only thing that notices is a human, eventually, if you are lucky.

### How this system answers each of those

**Duplicates are made harmless by a unique constraint, not by application
logic.**

```sql
insert into vendor_events (vendor, vendor_event_id, ...) values (...)
on conflict (vendor, vendor_event_id) do nothing
returning id;
```

No row returned means the message has been seen. The job is enqueued **only if
the insert actually happened, in the same transaction** — so three deliveries
produce one stored event and exactly one job. Verified: three identical
webhooks → `200`, `200`, `200`, one row, one job.

**Ordering is decided by rules, not by arrival time.** An explicit state
machine governs which transitions are legal — and it lives in the **database**,
as a table of permitted edges enforced by a `BEFORE UPDATE` trigger, so both
services are bound by it whether they consult it or not:

```
started → submitted → checking → screening → decided
```

Forward skips are allowed, because a fast vendor genuinely can jump a stage.
Backward moves never are. A late `checking` arriving at a `decided`
application is not a race that was lost — it is a transition that does not
exist.

Note what is absent from that diagram: **nothing reaches `decided` except from
`screening`.** That is not tidiness, it is a compliance rule — *you may not
decide on a customer you have not screened* — and it is enforced by Postgres
rather than by everyone remembering:

```
$ psql -c "update applications set status = 'decided' where id = '<a new case>'"
ERROR:  illegal application lifecycle transition: started -> decided
HINT:   Nothing reaches 'decided' except from 'screening'.
```

`worker/lifecycle.py` keeps a copy of the table so the worker can decide without
a round trip, and a test asserts the copy matches the schema — so drift is a
test failure rather than a surprise. The database is authoritative.

**Two guards, because they answer different questions.** The state machine
governs *legality*. The vendor's own timestamp governs *recency*. Neither
substitutes for the other:

| | Catches | Blind to |
| --- | --- | --- |
| State machine | `decided → checking`; deciding without screening | Two results that map to the *same* stage |
| Vendor timestamp | An older `Declined` overwriting a newer `Approved` | An illegal jump, however new the message |

The comparison is the vendor's clock against the vendor's clock. Comparing our
observation time to their event time would mix two clocks, and the skew between
them is exactly where such a guard stops working without telling you.

**Missing messages are caught by asking.** Every 15 minutes a self-rescheduling
job finds applications that have been waiting on the vendor longer than they
should and asks the vendor's API directly. Webhooks become an **optimisation**
that makes the common case fast; the sweeper is what makes the system
*correct*. **Push for latency, poll for correctness.**

Both paths converge on the same function. The sweeper exists to be right when
the webhook path fails, and two implementations would mean two sets of bugs,
with the recovery path exercised only during incidents.

### Exactly-once does not exist

A handler calls the vendor's API and then marks the job done. Those cannot be
one atomic operation, because an HTTP request cannot be rolled back. If the
process dies in between, the job runs again.

No configuration fixes this. It is a property of distributed systems, not a
gap in a library. What you get is **at-least-once delivery**, and the
obligation that follows is that **every handler must be idempotent** — safe to
run twice.

The technique used throughout, and the reason it is demonstrable rather than
merely claimed:

> **Compute the desired state. Compare it with the current state. Write only
> the difference. Record only what was written.**

Run a handler again and the difference is empty, so nothing is written — and
no new audit rows appear, because `audit_events` records state *changes*, not
handler *invocations*. Get that last part wrong and a perfectly idempotent
handler still leaves a growing trail of identical rows, which looks exactly
like a bug.

Verified by re-running a processed job and comparing before and after: status,
audit count, the linked event and **`updated_at`** all byte-identical.
`updated_at` is the interesting one — a trigger bumps it on any `UPDATE`, so
its not moving proves the handler did not even issue a redundant write.

### And the same idea at the other end

Two compliance officers can open the same case. Three layers stop them both
deciding it, and only the last is a guarantee:

| Layer | Mechanism | What it gives |
| --- | --- | --- |
| UI | a decided case renders with no buttons | stops the honest mistake |
| Transaction | `SELECT … FOR UPDATE`, then re-read | determinism, and a good error message |
| Database | partial unique index on terminal decisions | **impossibility** |

Plain `FOR UPDATE`, **not** `SKIP LOCKED` — deliberately the opposite of the
job queue. The queue skips contended rows because another worker will take
them and nobody needs telling. Here the loser of a race is a *person waiting
for an answer*, so the second transaction blocks, re-reads, and can report who
decided it. `SKIP LOCKED` would have made their submission vanish.

---

## Architecture

```
  browser ──► /web  (Next.js + TypeScript)
                   │  writes rows
                   ▼
              PostgreSQL  ◄── /db   numbered SQL migrations
                   ▲             (owned by neither service)
                   │  claims jobs
              /worker (Python)
                   │
                   └──► identity vendor API
```

**The two services never call each other.** There is no HTTP between them.
They coordinate only through a `jobs` table claimed with
`FOR UPDATE SKIP LOCKED`. That removes an internal API, service discovery,
shared auth and CORS from the system — and means that if the worker is down,
work queues up instead of failing.

**`/db` belongs to neither service.** Two languages share one database, so the
schema cannot be owned by either one's ORM. Migrations are numbered SQL files
applied by a standalone runner, and both services are clients of a schema
neither defines. The cost, stated plainly: no generated types, and mismatches
surface at runtime rather than at build time.

Raw SQL in both services — `pg` in TypeScript, `psycopg` in Python. No ORM.

### Why the queue is a Postgres table

Not "one less service to run". **Transactional enqueue.**

If the queue were Redis and the data Postgres, this has a hole in it:

```
insert the vendor_event row      (Postgres)
enqueue a job                    (Redis)
```

Crash between those two lines and you have either a stored event nobody will
process, or a job pointing at a row that does not exist. **There is no
ordering of those two statements that is correct.** With the queue in
Postgres it is one transaction and either both happened or neither did.

**When a table stops being a good queue**, honestly: every claim is a write, so
at thousands of jobs a second you are spending database capacity and vacuum
pressure on queue mechanics rather than on users; polling trades latency for
load, and `LISTEN`/`NOTIFY` is the upgrade path; completed jobs accumulate and
need retention; concurrency is bounded by connections; and it is a work queue,
not a message bus — no fan-out, no replay, no topics. In one sentence: **when
the queue's own traffic starts competing with your users' queries, move it
out.**

### The audit log

`audit_events` is append-only, and not by convention. Migration `006` puts
`BEFORE UPDATE`, `BEFORE DELETE` and `BEFORE TRUNCATE` triggers on it that
raise an exception:

```
update audit_events set actor = 'x'      → ERROR: audit_events is append-only
delete from audit_events where false     → ERROR: … DELETE is not permitted
truncate audit_events                    → ERROR: … TRUNCATE is not permitted
```

The `where false` case matters — it matches no rows and is *still* refused,
because the triggers are `FOR EACH STATEMENT` rather than `FOR EACH ROW`. And
`TRUNCATE` needs its own trigger, because it does not fire `DELETE` triggers;
without it the whole log could be emptied by a statement the other two never
see.

The honest limit: **a superuser can disable a trigger.** No in-database
mechanism survives someone with full control of the database. Real
institutions ship audit records to external append-only (WORM) storage, where
the people who can edit the database do not control the archive. That is out
of scope here, and this says so rather than implying a guarantee it does not
have.

The rule binds the authors too: the seed script writes six weeks of backdated
history and **cannot undo it**, so it refuses to run twice without `--force`.

---

## Running it locally

Requires Docker Desktop, Node 22+, Python 3.12+.

```bash
cp .env.example .env          # then edit if needed

docker compose up -d          # Postgres on host port 5434
python db/migrate.py up       # apply the schema

cd web && npm install && npm run dev      # http://localhost:3001
cd worker
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
.venv/Scripts/python main.py                              # the queue consumer
```

Then:

| Where | What |
| --- | --- |
| `/apply` | the applicant's journey |
| `/login` | the compliance review desk (credentials are printed on the page) |
| `/stats` | throughput, automation rate, latency — all computed from the database |

Fill the database with six weeks of plausible history:

```bash
cd worker && .venv/Scripts/python seed.py --count 500 --weeks 6
```

Run the tests — 182 of them:

```bash
cd worker && .venv/Scripts/python -m pytest
```

132 are pure: no database, no network, no fixtures, because the scoring and
lifecycle logic are functions with no I/O in them. They run on a fresh clone
with nothing installed but the dependencies.

The other 50 need Postgres, and would be worthless without it — SKIP LOCKED,
the lifecycle trigger, the partial unique indexes and the append-only rules are
all database guarantees, and a mock of a database proves nothing about them.
They run against a throwaway database (`DATABASE_URL` with `_test` appended)
whose schema is dropped and rebuilt by the real `db/migrate.py` each session,
so every run also exercises the migration runner against an empty database.
With no database reachable they skip cleanly rather than failing, which is why
the command above works either way.

CI runs both on every push, against a real `postgres:16`.

> **Do not run `npm run build` while `npm run dev` is running.** They share the
> `.next` directory, and the build replaces the chunks the dev server is
> serving; every page then fails with `Cannot find module './vendor-chunks/…'`,
> which looks like broken application code and is not. Use `npm run build:check`,
> which builds into a separate directory. If it has already happened: stop the
> dev server, delete `.next`, start it again.

---

## Deploying

The live demo runs on free tiers: **Vercel** for the Next.js app, **Neon** for
Postgres, and the worker as an **on-demand function** rather than a long-lived
process.

That last part is a real architectural difference and worth stating plainly.
Locally, and in the repository, the worker is `worker/main.py`: a loop that
claims a job, runs it, sleeps, and repeats for as long as the process lives.
That is the right shape for the job and it is what `docker compose up` gives
you. It is also a process that has to be running somewhere all the time, and
somewhere all the time costs money. I am not paying a monthly bill to host a
portfolio demo, and that is a good enough reason.

So production has a second entry point. `POST /api/drain` claims a **bounded
batch** — five jobs — runs them, and returns. It is called repeatedly instead
of looping: once by the web app the moment work is enqueued, so an applicant
sees a decision in seconds, and every five minutes by a GitHub Actions cron, so
that the reaper and the sweeper still run and nothing is stranded if that first
call is lost.

**Same handlers, two runtimes.** `worker/runner.py` holds the part that must
not differ — the transaction boundary, the retry accounting, what counts as a
permanent failure — and both entry points import it. Jobs are still claimed one
at a time with `FOR UPDATE SKIP LOCKED`; two concurrent drains behave exactly
like two concurrent workers because they execute the identical statement. The
500-job no-double-processing proof is run against both.

Moving to a function forced one other change, and it improved the system: the
sanctions list used to be held in memory, 86MB of it, loaded at boot. A
function that must start, answer and exit cannot afford that. The list now
lives in Postgres and is searched in two stages — a `pg_trgm` index narrows
19,393 entries to about 27 candidates, then rapidfuzz scores those with exactly
the code that ran before. An equivalence test compares both matchers over 400
applicants and asserts identical matches, strengths and decisions; it found
three real bugs while being written, which are written up in
[docs/decisions.md](docs/decisions.md).

None of this changes what the repository is. `docker compose up` still runs the
long-lived worker, the test suite still exercises the full asynchronous
pipeline — idempotency, out-of-order results, the sweeper, concurrent claims —
and none of those tests care which entry point is in front of them.

The full deployment runbook is in [docs/deploy.md](docs/deploy.md).

---

## What each part does

### Identity verification

The vendor is **Didit**. Sumsub was the original choice but requires a business
account, so sandbox credentials were unobtainable for a demo anyone should be
able to clone and run.

The integration is written against Didit's published contract
([sessions](https://docs.didit.me/sessions-api/create-session),
[webhooks](https://docs.didit.me/integration/webhooks)) and **has not been
tested against their live API**, because this project has no Didit account.
Saying so plainly matters more than the claim would be worth.

`DIDIT_MODE=simulator` (the default) runs a local stand-in that speaks the same
contract: it issues sessions, presents a verification screen, and posts back
correctly signed `status.updated` webhooks. It lives inside `/web` under
`mock-vendor/` — two services and one database is the ceiling, and demo
scaffolding does not get to raise it. `DIDIT_MODE=live` points the same code at
the real API and makes the simulator routes 404.

### Webhook security

Three independent defences, because each leaves a gap the others close:

| Layer | Stops | Does not stop |
| --- | --- | --- |
| HMAC signature over the **raw bytes** | Forgery — a stranger posting "approve me" | Replay of a genuine captured message |
| `X-Timestamp` freshness (±5 min) | Replay outside the window | Replay inside it |
| `UNIQUE (vendor, vendor_event_id)` | A replay having any effect at all | — |

The signature is verified over the raw request body **before any JSON
parsing**. Parsing and re-serialising produces different bytes, and the
tempting fix — verifying the re-serialised form — means verifying a string you
constructed rather than the one the vendor signed, which silently destroys the
guarantee while appearing to work.

Didit recommends their `X-Signature-V2` header, which signs a canonicalised
re-serialisation. **This deliberately uses `X-Signature` over the raw bytes
instead.** Verifying V2 means reimplementing their JSON serialiser exactly —
key ordering, separators, Unicode escaping, float normalisation — and every one
of those is somewhere an implementation can differ subtly, with the failure
appearing as a vendor outage rather than a crash. Raw bytes have exactly one
interpretation.

Rejected requests are **never written to the database**: that path is
unauthenticated, so any write there is a free resource-exhaustion vector.

### Sanctions screening and risk scoring

Three list sources behind one interface, chosen by `SANCTIONS_SOURCE`:

| Source | Licence | Role |
| --- | --- | --- |
| `synthetic` | fabricated, committed | a fresh clone works offline; no licence question |
| `ofac` | **public domain** — a US Government work, 17 U.S.C. § 105 | real data, 19,385 entries; `python scripts/download_ofac.py` |
| `opensanctions` | **CC-BY-NC** — commercial use needs a licence | supported, documented, deliberately not shipped |

Every screening run records **which version of the list it used** — source,
the publisher's own publication date, a sha256 of the file, and the entity
count — and every match points at that snapshot. So a decision can be traced
back to the exact list content that informed it:

```
decision          rejected, by staff:officer@example.com, score 60
  ruleset         2026-09-1, thresholds {approve <20, reject >=80}
  match           OFAC SDN, "ELLISSA HOLDING", 100%
  list version    ofac, published 2026-09-18, 19,393 entries
                  sha256 3344c6ea837c2d74ba19688be26a49b06d839031856aa6733b4413553996a950
```

The officer sees the same line on the case, beside the matches.

**What that hash does and does not prove.** It is the sha256 of the derived
JSON file the worker loaded. It makes the pipeline **tamper-evident within this
system**: the file loaded is the file recorded, and neither the snapshot row nor
the link from a match can be altered afterwards without the digest ceasing to
agree.

It is **not** independent verification, for two reasons worth being exact
about. The file is gitignored and not archived, so in three months nobody will
hold the bytes that produced this digest — OFAC's publication will have moved
on. And the digest covers *our* transformed JSON rather than OFAC's XML, so
even a reader holding the original source file could not reproduce it without
running this exact parser. Hashing the source artifact as well would fix the
second half; a WORM archive of the source file would fix the first. Neither is
built.

So: this answers *"was this person screened against the list this system
recorded, and has anything been altered since?"* — which is a real and useful
question. It does not answer *"was that file genuinely OFAC's publication of
2026-09-18?"*, and it does not make the list fresh. See the limitations.

Risk scoring lives in [`worker/scoring.py`](worker/scoring.py) as **pure
functions**: no database, no network, no clock. Every input arrives as an
argument, including the thresholds.

**Why a points system rather than a model.** Not because a model would score
worse — it might well score better. Because of what has to happen *after* the
score exists. An officer acts on the reason, not the number. A regulator asks
why a particular person was refused, and "the model said so" is not an answer.
The training data does not exist, because you refuse the risky applicants and
never learn whether they would have been fine. And a model trained on past
decisions learns past bias, with name and nationality as excellent proxies for
ethnicity. **The output has to be an argument, not a prediction.**

**No single signal can auto-reject**, and that conclusion was forced by
measurement rather than taste. With a confirmed sanctions match worth enough to
refuse on its own, **0.58% of applicants who were on no list at all were
auto-rejected** purely for having a common name — `Carlos Garcia` against
`Carlos Alberto GAXIOLA GARCIA`. On a book of 100,000 that is 580 real people
refused by a string comparison. Lowering that signal so refusal requires a
second independent finding cut false rejections fivefold **and left recall
unchanged**. It is also what firms actually do: a potential match is escalated
and a human confirms identity before the firm acts.

The full argument, with numbers in both directions, is in
[`docs/decisions.md`](docs/decisions.md).

### The review desk

An officer's job is narrower than it sounds: **is this the person on the list,
or someone who shares their name?** The strings cannot answer it, so the
officer uses what the matcher never sees — date of birth, nationality, the
document result.

Everything in the layout follows from that being a *comparison* task. Applicant
details, document result and screening matches sit side by side rather than
behind tabs, because the two dates of birth have to be readable in one glance;
tabs would make the officer carry one in their head, and at case ninety of a
shift that is where mistakes come from. The queue is oldest-first with no sort
control, because the longest-waiting case carries the most regulatory exposure
and a sortable queue will eventually hide it for a fortnight.

A **written reason is required**, enforced by a `CHECK` constraint rather than
only by the form. An auditor samples decisions and asks one question per case:
*was this reasonable on the evidence available at the time?* That is only
answerable if the officer wrote down what they concluded. No length check can
force anyone to think — what it does is make the *absence* of a reason
impossible, and a one-word reason is itself a finding.

---

## Numbers

A **dated snapshot**, not a standing claim. Every figure on `/stats` is
computed from the database on each request; none is hardcoded. These are what
it read on one particular day, from a reproducible seed:

```bash
python db/migrate.py up
cd worker
python seed.py --count 600 --weeks 6 --seed 20260921
python seed.py --count 260 --weeks 2 --seed 8891 --force
```

| Figure | Value | Measured over |
| --- | --- | --- |
| Decisions made without a human | **80.2%** | 599 of 747 decided applications |
| Median time to decision, automatic | **9 minutes** | 394 automatic decisions in the last 500 |
| Median time to decision, reviewed by a human | **15.8 hours** | 106 reviewed cases in the last 500 |
| 90th percentile, overall | **15.9 hours** | the last 500 decisions |
| Reached a decision | **747** | of 860 applications created |

*Read on 2026-09-22, against the real OFAC SDN list (19,393 entries).*

**Quote `/stats`, not this table.** The auto-decided share is a property of the
applicant mix, so it moves whenever the data is reseeded — it has read 87.6% and
80.2% on different days of writing this. A number copied into prose is a number
that goes quietly wrong, which is why the page states its own denominators and
why the figures above carry a date.

Three notes, because a number without them is not publishable:

**The median is split by who decided, because a combined figure is
misleading.** With the large majority decided automatically in seconds, the
machine's nine minutes drowns the number anyone actually wants. The combined
median describes the machine, not the process.

**The auto-decided share is a property of the applicant mix**, not an
efficiency claim — and of a synthetic mix at that. It is quoted with its
denominator everywhere it appears, for exactly this reason.

**The oldest case in the queue has been waiting weeks**, which in a real firm
would be an audit finding rather than a statistic. It is there because the
seeder keeps a small floor of genuinely stuck cases, and it is what the red
waiting-time styling on the desk exists to surface. A queue screen that never
shows an ugly number is not demonstrating anything.

### Making synthetic data that does not look synthetic

The arrival histogram, real output from the seeder:

```
 hour  count
    2      1
    5      2
    8      9  ####
   10     34  #################
   13     23  ###########
   17     31  ###############
   18     51  #########################
   20     54  ###########################
   23     14  #######
```

Quiet overnight, a morning ramp, a midday plateau, an evening peak, a tail to
midnight. `random.uniform(start, end)` gives a flat block, and a histogram is
the first thing anyone plots.

The tells that give generated data away, and what was done about each:

| Tell | Fix |
| --- | --- |
| Flat timestamps | Rejection sampling on hour-of-day × weekday weights |
| Uniform name frequency | Zipf-biased draw — a few common, a long tail of singletons |
| Uniform ages | Lognormal, skewed young with a tail |
| Everyone completes | 14% never finish — the gap between 500 created and 421 decided |
| Tidy 33/33/33 outcomes | Outcomes are not assigned; the real scoring code computes them |
| Machine-even durations | Lognormal, so there is a long right tail |
| Insertion order ≠ time order | Rows inserted **chronologically**, so ids and `created_at` agree |

The single most valuable detail is that **officers work office hours**. It is
what makes the queue build overnight and across weekends and drain on the next
working morning — and what makes median time-to-decision a real number rather
than a constant.

Every seeded audit event carries `"seeded": true`. That table is the one thing
meant to be unvarnished truth, and the seeder fabricates six weeks of it.

---

## Limitations — what this does not do

Naming the gaps is what makes the rest credible. This is a demonstration of an
approach, not a system that should go near a real customer. Specifically:

### It has never touched real data or a real vendor

- The Didit integration is written from published documentation and **has never
  been run against the live API**. It compiles and is exercised end to end
  against a simulator that speaks the same contract — which proves our half of
  the wire and nothing about theirs.
- Every applicant is invented. No real identity document has been processed.
- The sanctions list used by default is **fabricated**. The real OFAC list is
  available via a downloader, but even OFAC alone is US-centric: a real firm
  screens against EU, UK and UN lists as well.

### Screening is not adequate for real use

- **No ongoing monitoring.** Customers are screened once, at onboarding. Real
  obligations require rescreening the entire book whenever lists change — a
  person can be sanctioned the day after you approve them.
- **The list is loaded once per process and never refreshed.** A long-running
  worker screens against the list as it stood when it started, and sanctions
  lists change daily. This is now *visible* rather than invisible — every case
  records which list version screened it, so a stale one is readable evidence
  — but **visible is not fixed**. Nothing reloads the list, and nothing warns
  when the one in memory has aged. A worker up for three weeks is screening
  against a three-week-old list and will tell you so only if you look.
- **The list file is not archived.** Snapshots record a sha256 of the file that
  was loaded, but the file itself is gitignored and kept nowhere. The digest is
  therefore tamper-evidence within this system rather than something a third
  party can independently check — in three months the bytes are gone. A real
  deployment would archive the source artifact to WORM storage, which is the
  same argument this project already makes about audit records.
- **The hash is recorded, not verified.** Nothing compares it against a known
  good value, so a truncated or swapped file produces a fresh snapshot row and
  screening continues silently against whatever it contains. The cross-checks
  that would catch it — the publisher's own declared record count, and a sanity
  comparison against the previous snapshot — are not implemented.
- **951 single-token OFAC entries are unmatched by construction**, because
  matching them against every applicant sharing a first name produced a 31.8%
  false rejection rate. Real systems resolve mononyms with passport numbers and
  dates of birth, which this does not collect.
- **Transliteration recall is 97%, not 100%.** Two of 67 tested transliteration
  variants were missed. Each miss, in a real firm, is a regulatory breach.
- **No name-frequency weighting.** A 90% match against `Ahmed Hassan` carries
  far less information than a 90% match against a rare name, and this treats
  them identically.
- **No entity screening, no beneficial ownership, no adverse media** beyond a
  scoring hook with nothing behind it.

### The risk model is illustrative

- The points and thresholds are **defensible, not validated**. Nobody has
  back-tested them against real outcomes, because no real outcomes exist.
- **The FATF increased-monitoring list is sourced but will go stale.** It is
  the complete set from the statement of 19 June 2026, retrieved 24 September
  2026, and it carries that date on every decision scored against it. FATF
  revises the list roughly three times a year, so it needs replacing after each
  plenary -- wholesale, never country by country, because a set assembled from
  two plenaries screens against countries already cleared and misses countries
  added since. Nothing here fetches it automatically.
- **The call-for-action list (Iran, DPRK, Myanmar) carries no publication
  date.** It was not re-fetched when the monitoring list was corrected, because
  the FATF statement returned HTTP 403, so the data records it as unsourced
  rather than borrowing the monitoring list's date.
- No segmentation by product, channel or transaction behaviour — all of which a
  real risk model uses.

### Security and operations

- **One shared staff account**, with the password in `.env` in plaintext. There
  is no user table, no per-user hashing, no password reset, no second factor.
- **Sessions cannot be revoked.** A signed cookie is valid until it expires,
  and there is no server-side record to invalidate.
- **Login rate limiting is in memory** — it resets on restart and is
  per-process, so several instances each get their own allowance.
- **No four-eyes principle.** One officer can approve any case alone. Real
  firms require a second reviewer above a threshold.
- **No role separation.** Every signed-in user can decide anything.
- **The browser is mostly not tested.** CI typechecks the web service and
  asserts the database contracts its server actions depend on, but nothing
  drives a page. The one exception is the disclosure boundary: a dedicated CI
  job boots Postgres, seeds, builds and starts the web app, and asserts that
  the public applicant status page never reveals what sanctions screening
  found. That job sets `STRICT_DISCLOSURE_TEST=1`, which turns every reason the
  check could not run — no web service, no database, nothing seeded worth not
  disclosing — into a failure rather than a skip, because a guard that silently
  does not run is worse than no guard.
  A bug in a React component, or in the TypeScript above the SQL, would pass.
  One case is guarded by reading the source rather than running it: a test
  fails if the desk's `for update` ever becomes `for update skip locked`, which
  would leave every database guarantee intact while telling the losing officer
  the case does not exist.
- **No pagination.** The queue shows 200 cases and silently hides the rest.
- **No backups, no disaster recovery, no runbook, no alerting.** Nothing tells
  anyone that the parked-jobs count is climbing.

### Data protection

- **No retention policy on personal data.** Applications and audit events are
  kept forever. Real KYC records have statutory retention *and* deletion
  obligations, which conflict in interesting ways with an append-only audit log
  — a genuinely hard problem this does not attempt.
- **No subject access or erasure flow.** Both are legal requirements.
- **No encryption at rest beyond whatever the host provides**, and no field-level
  encryption of personal data.
- Document images are never stored, which is deliberate and right — but the
  vendor holds them, and the vendor relationship is out of scope here.

### What would have to change before real customers

At minimum: a real vendor contract and integration testing against it; ongoing
screening against multiple lists with versioned snapshots; a validated and
back-tested risk model with documented governance; per-user authentication with
MFA and role separation; four-eyes on high-risk decisions; a data retention and
erasure policy reconciled with the audit requirements; WORM storage for audit
records; end-to-end tests that drive a browser; monitoring and alerting with an
on-call rota; and a compliance function that has reviewed all of it. That is a team and
a year, not a demo.

---

## Reading the code

If you are only going to open three files:

1. **[`worker/scoring.py`](worker/scoring.py)** — the risk logic. Pure
   functions, no I/O, every rule justified where it is defined. The file this
   project would most want read.
2. **[`worker/jobs.py`](worker/jobs.py)** — the queue. Read `CLAIM_SQL` first;
   everything else is bookkeeping around it. The deliberately-broken
   `claim_job_naively` below it exists to demonstrate the race condition rather
   than merely assert it.
3. **[`web/src/app/api/webhooks/didit/route.ts`](web/src/app/api/webhooks/didit/route.ts)**
   — the webhook. Four fast local operations and a great deal of explanation
   about why it does nothing more.

[`docs/decisions.md`](docs/decisions.md) records what was decided in each
phase, what was rejected, and the things that turned out to be wrong.
[`docs/concepts.md`](docs/concepts.md) is a plain-English glossary of every
term, fintech and technical.

---

*Built as a portfolio piece. Synthetic data throughout.*
