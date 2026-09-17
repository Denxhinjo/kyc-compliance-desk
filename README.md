# KYC Onboarding & Compliance Review Desk

> **Demo — synthetic data only.** A portfolio project, not a product and not
> client work. No real people, no real identity documents, no real money.

A customer identity-verification flow (KYC/AML) and the back-office review desk
where a compliance officer handles the cases that cannot be decided
automatically.

## Architecture

```
  browser ──► /web (Next.js + TypeScript)
                   │
                   │  writes rows
                   ▼
              PostgreSQL  ◄── /db  numbered SQL migrations
                   ▲            (owned by neither service)
                   │  claims jobs
                   │
              /worker (Python)
```

The two services **never call each other**. There is no HTTP between them.
They coordinate only through a `jobs` table in Postgres, claimed with
`FOR UPDATE SKIP LOCKED`. That removes an internal API, service discovery,
shared auth and CORS from the system — and means that if the worker is down,
work queues up instead of failing.

| Directory | What lives there |
| --- | --- |
| `/web` | Next.js App Router. Sign-up flow, review desk, vendor webhook. |
| `/worker` | Python. Sanctions screening and risk scoring. |
| `/db` | Numbered SQL migrations. Owned by neither service. |
| `/docs` | `decisions.md` (why things are the way they are), `concepts.md` (glossary). |

Raw SQL in both services — `pg` in TypeScript, `psycopg` in Python. No ORM.

## Running locally

Requires Docker Desktop, Node 22+, Python 3.12+.

```bash
cp .env.example .env          # then edit if needed

docker compose up -d          # Postgres on host port 5434

cd web && npm install && npm run dev
# http://localhost:3001  (3000 is taken on this machine)

cd worker
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt     # macOS / Linux
.venv/Scripts/python main.py
```

`.env` at the repo root is the single source of truth, read by docker-compose,
the web app and the worker. It is gitignored; `.env.example` is the committed
inventory of every variable.

## Identity verification

The vendor is **Didit**. Sumsub was the original choice, but it requires a
business account, so sandbox credentials were unobtainable for a demo that
anyone should be able to clone and run.

The integration is written against Didit's published contract
([sessions](https://docs.didit.me/sessions-api/create-session),
[webhooks](https://docs.didit.me/integration/webhooks)) and **has not been
tested against their live API**, because this project has no Didit account.
Saying so plainly matters more than the claim would be worth.

By default `DIDIT_MODE=simulator` runs a local stand-in that speaks the same
contract: it issues sessions, presents a verification screen, and posts back
correctly signed `status.updated` webhooks. It lives inside `/web` under
`mock-vendor/` — two services and one database is the ceiling, and demo
scaffolding does not get to raise it. Setting `DIDIT_MODE=live` with an API
key, workflow id and webhook secret points the same code at the real API; the
simulator routes then return 404.

### The webhook

`POST /api/webhooks/didit` does four things and nothing else: verify the
signature over the raw bytes, store the message, enqueue a job, return 200.

**200 does not mean "I have done the work". It means "I have durably taken
responsibility for this message, and you may stop resending it."** Didit allows
a few seconds and retries on 5xx, 404, timeout or connection failure — twice,
then it drops the delivery. Doing real work inline would guarantee timeouts,
which cause retries, which cause concurrent duplicate processing.

Three independent defences, because each one leaves a gap the others close:

| Layer | Stops | Does not stop |
| --- | --- | --- |
| HMAC signature over raw bytes | Forgery — a stranger posting "approve me" | Replay of a genuine captured message |
| `X-Timestamp` freshness (±5 min) | Replay outside that window | Replay inside it |
| `UNIQUE (vendor, vendor_event_id)` | A replay having any effect at all | — |

Verified over the **raw request body, before any JSON parsing**. Didit
recommends their `X-Signature-V2` header, which signs a canonicalised
re-serialisation; we use `X-Signature` over the exact bytes instead, because
verifying V2 means reimplementing their serialiser and any mismatch is a silent
security bug. Raw bytes have exactly one interpretation.

## Status

Phase 6 complete. An applicant fills in the form, completes identity
verification, and watches the application move through its lifecycle on its own
— then it is screened against a real sanctions list, scored, and either
auto-approved, auto-rejected, or referred to a compliance officer who decides it
at the review desk.

### The review desk

`/desk`, behind a staff login. The screen the rest of the system exists to feed.

An officer's job is narrower than it sounds: **is this the person on the list,
or someone who shares their name?** Everything in the layout follows from that.
Applicant details, document result and screening matches sit side by side rather
than behind tabs, because the task is comparison — the applicant's date of birth
and the listed one have to be readable in the same glance. Score and flag
reasons are above the fold. The queue is oldest-first with no sort control,
because the longest-waiting case carries the most regulatory exposure.

A written reason is required, and enforced by a `CHECK` constraint rather than
only by the form. An auditor samples decisions and asks one question per case:
*was this reasonable on the evidence available at the time?* That is only
answerable if the officer wrote down what they concluded.

**Two officers cannot decide the same case.** Three layers:

| Layer | Mechanism | What it gives |
| --- | --- | --- |
| UI | decided cases render with no buttons | stops the honest mistake |
| Transaction | `SELECT … FOR UPDATE`, then re-read | determinism and a good error message |
| Database | partial unique index on terminal decisions | **impossibility** |

Plain `FOR UPDATE`, not `SKIP LOCKED` — the opposite of the job queue, on
purpose. The queue skips contended rows because another worker will take them.
Here the loser of a race is a person waiting for an answer, so the second
transaction blocks, re-reads, and reports who decided it.

The case timeline is read-only because `audit_events` physically rejects
`UPDATE`, `DELETE` and `TRUNCATE` (migration 006). There is no edit control to
build.
Results are applied through a vendor-neutral adapter and guarded by an explicit
state machine, so duplicate, late and out-of-order deliveries are harmless. A
sweeper reconciles anything the vendor never managed to tell us.

### The lifecycle

```
started -> submitted -> checking -> screening -> decided
```

Transitions are an explicit allow-list. Forward skips are permitted (a fast
vendor can jump a stage); backward moves never are; and nothing reaches
`decided` except from `screening` — "you may not decide on a customer you have
not screened", enforced by the state machine rather than by convention.

Two independent guards stop a late result overwriting a newer one, because they
answer different questions:

| Guard | Question | Catches |
| --- | --- | --- |
| State machine | Is this transition legal? | `decided -> checking`, and deciding without screening |
| `vendor_result_at` | Is this result newer? | An older `Declined` overwriting a newer `Approved` — same stage, so the state machine cannot see it |

The comparison is the vendor's clock against the vendor's clock. Comparing our
observation time to their event time would mix two clocks and fail silently
under skew.

### The sweeper

Webhooks are a delivery attempt, not a guarantee — Didit retries twice and then
drops the message. A deploy, a 500, or an expired tunnel and the vendor has a
verdict we never hear. Every 15 minutes a self-rescheduling job finds
applications waiting longer than they should and asks the vendor directly.

Webhooks are the optimisation; the sweeper is what makes the system correct.
Push for latency, poll for correctness.

### Sanctions screening and risk scoring

Screening runs against one of three sources, chosen by `SANCTIONS_SOURCE`:

| Source | Licence | Role |
| --- | --- | --- |
| `synthetic` | fabricated, committed | works offline on a fresh clone; no licence question |
| `ofac` | **public domain** (US Government work, 17 U.S.C. 105) | real data, 19,385 entries; `python scripts/download_ofac.py` |
| `opensanctions` | CC-BY-NC — commercial use needs a licence | supported, documented, deliberately not shipped |

Risk scoring lives in [worker/scoring.py](worker/scoring.py) as **pure
functions**: no database, no network, no clock. Every score carries the signals
that produced it, and the thresholds in force are stored alongside, so a
decision stays explainable after the rules change.

**No single signal can auto-reject.** Measured against the real OFAC list,
0.58% of applicants on no list at all reached the top match band purely by
having a common name — `Carlos Garcia` against `Carlos Alberto GAXIOLA GARCIA`.
Refusing those automatically would mean 580 wrongly rejected customers per
100,000. A name match now guarantees review; refusal needs a second independent
signal. Recall was unaffected. The full argument, with numbers in both
directions, is in [docs/decisions.md](docs/decisions.md).

```bash
# the desk: http://localhost:3001/login   (credentials are printed on the page)

cd worker && .venv/Scripts/python -m pytest    # 121 tests, no database needed
.venv/Scripts/python analyse_distribution.py --count 2000 --source ofac

cd web
npm run demo -- new                            # an applicant + a session
npm run demo -- complete <session> Approved    # vendor decides, webhook sent
npm run demo -- complete <session> Approved --drop   # vendor decides, delivery lost
npm run demo -- show <application>             # status + timeline
```

```bash
python db/migrate.py up                     # apply the schema

cd web  && npm run dev                      # http://localhost:3001/apply
cd worker && .venv/Scripts/python main.py   # consume the queue (run several)
```

**Do not run `npm run build` while `npm run dev` is running.** They share the
`.next` directory, so the build replaces the chunks the dev server is serving
and every page then fails with `Cannot find module './vendor-chunks/…'`. The
symptom looks like broken application code; it is not. Use `npm run build:check`
instead, which builds into a separate directory, or stop the dev server first.
If it has already happened: stop the dev server, delete `.next`, start it again.

Replay a webhook to see idempotency working:

```bash
cd web
npm run webhook -- --application <uuid> --times 3   # 3x 200, one row, one job
npm run webhook -- --application <uuid> --tamper    # 401
npm run webhook -- --application <uuid> --stale     # 401
```
