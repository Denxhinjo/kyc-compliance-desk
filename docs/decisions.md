# Decisions

A running log of what was built in each phase, what was decided, and why.
Written to be readable by someone who was not here.

---

## Phase 0 — Foundations

### What we built

A walking skeleton: every component exists and every connection between them is
proven, but nothing does useful work yet.

- `docker-compose.yml` running PostgreSQL 16 locally, with a health check.
- `/web` — Next.js (App Router) + TypeScript. One page that queries Postgres on
  every request and renders either the server version and clock, or the error.
- `/worker` — Python. A loop that asks Postgres for its own clock every ten
  seconds, holding one long-lived connection and reopening it if it dies.
- `/db` — empty but documented: the directory that will hold SQL migrations,
  owned by neither service.
- A single root `.env` (gitignored) with `.env.example` as its committed
  inventory, read by all three of compose, web and worker.

### Why `/db` belongs to neither service

This is the structural decision of the phase.

Normally migrations live inside the application, managed by that language's
ORM. That breaks here because two languages share one database. Three concrete
failures follow from putting migrations inside `/web`:

1. The Python worker learns about schema changes at runtime, in production,
   when a query fails.
2. Deployment ordering becomes a trap — the worker can start against a schema
   the web app has not migrated yet.
3. Column types get chosen for what the ORM models nicely rather than what
   Postgres does well, and the other service reverse-engineers intent from
   generated SQL.

Making `/db` a peer directory says something structural: the database is a
third participant, not a detail belonging to one service. Both services are
clients of a schema neither defines.

The honest cost: no generated types, and no compile-time guarantee that
application SQL matches the schema. Mismatches surface as runtime errors. For a
two-language system built around a shared database, that is the right trade —
and it is the trade real payments companies make.

### Decisions and alternatives

| Decided | Rejected | Reasoning |
| --- | --- | --- |
| `pg` (node-postgres) | `postgres.js` | `pg` takes plain SQL strings with `$1` placeholders, so the SQL stays visibly SQL. `postgres.js` tagged templates start to read like an ORM, which defeats the point. |
| `psycopg` 3 | `psycopg2` | v3 is current; v2 is maintenance-only. |
| Postgres in Docker, apps on the host | everything in Docker | Much faster edit–run loop, and it sidesteps container file-watching problems on Windows. |
| One root `.env` + `dotenv` in both services | a `.env` per service | Three copies of `DATABASE_URL` would drift. The cost is two small dependencies (`dotenv`, `python-dotenv`), which was judged worth it. |
| Pool in `/web`, single connection in `/worker` | pool in both | The worker is one loop doing one thing at a time. A pool there would be machinery without a job. |
| `application_name` set on both connections | leaving it unset | It makes each service identifiable in `pg_stat_activity`. Free, and invaluable when something is holding a lock. |
| stdlib `venv` | Poetry / uv | One fewer tool to install. |

### Things worth knowing

**The page is forced dynamic.** Next.js would otherwise notice the page has no
dynamic inputs and render it once at build time — so "database connected" would
report the state of the world during `next build`, not now. For a health check
that is worse than useless, so the page opts out of caching explicitly.

**The pool is cached on `globalThis`.** In development Next.js hot-reloads
modules on every save. Without that cache, each reload would create a new pool
and leak the old one's connections until Postgres refused new ones.

**Empty error messages are a real trap.** The first version of the error path
rendered a blank box when Postgres was down. The reason: Node tries IPv6 and
IPv4 in parallel, both are refused, and it throws an `AggregateError` whose own
`message` is an empty string — the detail lives in `.errors`. The health check
now unwraps that and includes the driver's `code` (`ECONNREFUSED`, or a
SQLSTATE like `28P01` for a bad password). The lesson generalises: the error
path deserves the same testing as the success path, because you only ever see
it on a bad day.

**The worker reconnects rather than crashing.** A worker runs for days;
Postgres restarts and connections get dropped. On an `OperationalError` it
discards the connection and tries again on the next beat. We demonstrated this
by stopping the database container mid-run: four failed heartbeats, then
automatic recovery on a fresh backend, with no restart.

**Ports.** 5432 and 5433 were already taken on the development machine, and 3000
was serving an unrelated app, so Postgres is exposed on **5434** and the web app
runs on **3001**. Both are set in one place (`.env` and `web/package.json`).

### Evidence

- `docker compose ps` — `kyc_db` healthy, `0.0.0.0:5434->5432/tcp`.
- The web page rendering `PostgreSQL 16.14`, database `kyc`, live server time.
- Worker log: successive heartbeats with the database's clock advancing ~10s.
- `pg_stat_activity` showing `kyc_web` and `kyc_worker` connected to database
  `kyc` at the same time, with the worker's `backend_pid` matching the pid it
  logged itself.
- Database stopped: page renders the `ECONNREFUSED` detail, worker logs errors;
  database restarted: both recover unattended.

---

## Phase 1 — Data model and audit log

### What we built

Six migrations, a migration runner, and the `logEvent()` helper in both
languages. Five tables: `applications` (the case file), `vendor_events` (the
postbox), `screening_results` (what the sanctions check found), `decisions`
(the verdict), `audit_events` (the story).

### Why `audit_events` is the important one

Every other table records **state**. This one records **causation**.

If `applications.status` says `decided` and `decisions.outcome` says
`rejected`, you know what happened but not why, in what order, or on whose
authority. A regulator's questions are almost always historical — who approved
this, what did they see, was it auto-decided or reviewed by a human, show me
every case where the officer overrode the system — and none of those are
answerable from current state, because `applications.status` is overwritten on
every transition. The intermediate states exist only if something wrote them
down as they happened.

That leads to the single most important rule in this phase:

> The audit write goes in the **same transaction** as the state change it
> describes.

If they are separate, a crash between them leaves a state change with no trail
— precisely the case where you most need one. This is why both helpers take a
connection/client as their first argument instead of opening their own. It
looks like an ergonomic detail. It is the whole design.

On the TypeScript side the parameter is typed as `PoolClient` rather than
"anything with a `.query` method", because the pool itself would satisfy the
looser type and passing the pool would silently put the audit write outside the
caller's transaction. Making that a compile error is cheaper than finding it in
production.

### Append-only in practice

Rows go in, rows never change, rows never leave. A correction is a new row, not
an edit. Three levels of enforcement, in increasing order of strength:
convention (worthless), `REVOKE` (real, but does not bind the table owner), and
a trigger that raises (binds everyone, owner included). We use the trigger, on
`UPDATE`, `DELETE` **and `TRUNCATE`** — `TRUNCATE` does not fire `DELETE`
triggers, so without the third one the whole log could be emptied by a
statement the other two never see.

The triggers are `FOR EACH STATEMENT`, not `FOR EACH ROW`, so they fire even
when a statement matches no rows: `delete from audit_events where false` is
refused too.

And the honest limit: a superuser can disable a trigger. No in-database
mechanism survives full control of the database. The real answer is shipping
audit records to external WORM storage where the people who can edit the
database do not control the archive. Out of scope here, and the README says so
rather than implying a guarantee that does not exist.

### Decisions and alternatives

| Decided | Rejected | Reasoning |
| --- | --- | --- |
| UUID primary key on `applications` | `bigserial` | The id appears in URLs and goes to the vendor. Sequential ids leak business volume and invite walking `/status/1`, `/status/2`. Internal tables keep integer keys. |
| `decisions` holds terminal outcomes only | also storing "referred to review" | If the table held verdicts and deferrals, "what was decided?" would stop having one answer. Referral is a status change plus an audit event. |
| One decision per application (unique index) | many | Re-deciding an appealed case would be a deliberate migration, not something that quietly starts happening. |
| `raw_body` **and** `payload` on `vendor_events` | just `jsonb` | `jsonb` reorders keys, drops duplicates and normalises numbers, so it cannot be used to re-verify a vendor signature computed over the exact bytes. jsonb to query, text to prove. |
| One row per screening match | one row per application with a boolean | A common name hits several list entries and the officer must dismiss each separately. |
| `match_score numeric(5,2)` | boolean matched yes/no | The threshold for "a hit" is a business decision, not a property of the data. |
| Mandatory `reason` as a DB constraint | form validation only | A form validation is a request; a constraint is a guarantee. |
| `risk_score_at_decision` stored | recompute on demand | Scoring rules change. "Why was this approved in March?" needs March's number. |
| No `down` migrations | reversible migrations | A rollback script written calm and run panicked, never tested, is worse than none. Undo by writing a new migration. |
| Runner in Python under `/db` | inside `/web`, or a shell script | It is a script, not a service. It re-declares its own config loading rather than importing `worker/db.py`, so the shared schema does not start depending on one of the two services. |

### The bug this phase caught, which is worth the whole phase

The first run of the Python smoke test printed a perfect seven-event timeline
— and had written nothing. Every row vanished.

The cause is a genuine psycopg trap. By default a psycopg connection opens a
transaction implicitly on the first statement and holds it open until you
commit. The script's first action was a `SELECT`, which started that implicit
transaction. Every subsequent `with conn.transaction():` block therefore found
a transaction already running, so instead of being a real transaction each one
became merely a **savepoint** inside it. Nothing was durable. A single stray
`rollback()` at the end of the script discarded all of it, and because the
script had read its own uncommitted writes, the output looked completely
correct.

The fix is `autocommit=True` on the worker's connection. That sounds like the
opposite of careful, and it is not: with no implicit transaction, every
`with conn.transaction():` block genuinely is the outermost one and commits
when it exits. The rule becomes visible — anything that must be atomic is
inside an explicit block, and anything outside one is a single self-contained
statement.

The general lesson, and the reason this is written down: **a test that only
checks its own output can pass while writing nothing.** The verification that
caught it was querying the database from a separate connection afterwards.

### Things worth knowing

- **`now()` is transaction start time**, not statement time. Several events
  written in one transaction share an identical `occurred_at`. Timelines are
  therefore ordered by `id`, not by timestamp.
- **Gaps in `audit_events.id` are normal.** Identity sequences do not return
  numbers after a rollback, so a rolled-back event burns an id permanently. A
  gap is not evidence of deletion.
- **`pg` is CommonJS.** Named imports (`import { Pool } from "pg"`) work under
  Next's bundler but fail under plain Node ESM, which cannot reliably see the
  named exports of a CJS module. `import pg from "pg"` works in both.
- **Checksums must normalise line endings**, or the same migration hashes
  differently on Windows and Linux and looks like it was tampered with.

### Evidence

- `migrate.py status` pending → applied; a second `up` is a clean no-op.
- Editing an applied migration is detected and refused, with both hashes shown.
- A full case: application created, three status transitions, a vendor event, a
  screening match, a decision — seven audit events, in order.
- One audit row written from TypeScript, six from Python, one table.
- A failing transaction leaves the status unchanged **and** no audit row,
  proving the two writes are genuinely bound together.
- `UPDATE`, `DELETE`, `DELETE ... WHERE false` and `TRUNCATE` on `audit_events`
  all refused; 7 rows still present afterwards.
- Six constraint violations refused: unknown status, lowercase country code,
  blank decision reason, second decision on one application, duplicate vendor
  event id, screening score above 100.

### Addendum: `referred` is a decision after all (migration 007)

The original design held only terminal outcomes in `decisions`, on the grounds
that "what was decided?" should have one answer. Reconsidered and reversed:
routing a case to a human **is** a decision. The system judged that it would
not decide, and that judgement has an author, a reason and a risk score — which
is precisely what this table exists to hold. Losing it to a status change would
throw away the "why" and keep only the "where".

`004_decisions.sql` was not edited. It is already applied and its checksum is
recorded; editing it is how a database and a repository start silently
disagreeing. The schema's history follows the same rule as the audit log — you
correct it by adding, never by rewriting. So the header comment in `004` is now
superseded by `007`, and `007` says so explicitly. A reader following the
migrations in order sees the reasoning change, which is more honest than a
repository that pretends the first decision was never made.

**The consequence that mattered.** A referred case now gets two rows — the
system's referral, then the human's verdict — so `one decision per application`
could no longer be true. Rather than drop that guarantee entirely, the unique
index was replaced with a **partial unique index**: one that applies only to
rows matching a `WHERE` clause.

```sql
create unique index decisions_one_terminal_per_application
    on decisions (application_id)
    where outcome in ('approved', 'rejected');
```

Any number of referrals, but still exactly one terminal verdict per case. That
is the guarantee actually worth keeping. Referrals are deliberately left
unconstrained, because a re-screened case could legitimately be referred more
than once, and `audit_events` records each in order.

The cost, stated plainly: queries asking for "the decision" must now say which
kind they mean. `where outcome in ('approved','rejected')` gets the verdict;
without it, a referral can come back instead — a decision about process rather
than about the applicant. That filter is the price of keeping the referral's
reasoning, and it is worth paying.

**Verified:** a referral inserts; a second referral inserts; a verdict inserts;
a second verdict is refused by the partial index; `'escalated'` is still refused
by the CHECK constraint.

---

## Phase 2 — The job queue

### What we built

A `jobs` table, `enqueueJob()` in TypeScript, and a Python worker that claims
work with `FOR UPDATE SKIP LOCKED`, retries with exponential backoff and
jitter, parks jobs that cannot succeed, and reclaims work abandoned by a worker
that died.

### `FOR UPDATE SKIP LOCKED`

Start with the version everybody writes first:

```sql
select id from jobs where status = 'queued' limit 1;   -- both workers see job 7
update jobs set status = 'running' where id = 7;       -- both proceed
```

Two workers run this microseconds apart, both `SELECT` before either `UPDATE`,
and both process job 7. The applicant is screened twice and two decisions are
written. In a system that moves money, this is how a payment gets sent twice.
The dangerous part is that it is *rare* — it works on a laptop and fails in
production under load.

`FOR UPDATE` takes a row-level lock, which fixes correctness and destroys
throughput: a second worker reaching the same row **blocks** until the first
commits. Ten workers then have the throughput of one.

`SKIP LOCKED` changes exactly one thing — a locked row is treated as though it
were not there. Worker B reaches job 7, sees it locked, and moves straight on
to job 8. Three workers hitting the queue simultaneously get three different
jobs and none of them waits.

What makes it airtight is that the exclusion is enforced by Postgres' lock
manager rather than by application logic. There is no window between checking
and setting, because there is no checking.

The `LIMIT 1` sits *inside* the locking sub-select deliberately: `SKIP LOCKED`
is applied during the scan, so Postgres keeps walking past locked rows until it
finds a free one. Put the limit outside and a busy first row yields "no work
available" when there is plenty.

**We measured the counterfactual rather than asserting it.** Same 200 jobs, two
workers, claiming without a lock: **380 executions of 200 jobs — 180 of them
ran twice.** One job was executed by both workers 8ms apart. With
`SKIP LOCKED`: 2,200 executions, 2,200 distinct jobs, zero duplicates.

### Why a database table is a legitimate queue

The real argument is not "one less service to run". It is **transactional
enqueue**.

If the queue were Redis and the data Postgres, this has a hole in it:

```
insert the vendor_event row      (Postgres)
enqueue a job                    (Redis)
```

Crash between those two lines and you have either a stored event nobody will
process, or a job pointing at a row that does not exist. **There is no ordering
of those two statements that is correct.** The usual fixes — the outbox
pattern, two-phase commit — are more machinery than the problem.

With the queue in Postgres it is one transaction, and either both happened or
neither did. Architecture rule 1 (the two services never call each other) is
what makes this possible: there is no synchronous handoff to get wrong.

### When it stops being a legitimate queue

Honest limits, because a portfolio piece that claims a design has no downsides
is not credible:

1. **Every claim is a write.** Claiming `UPDATE`s a row, which writes WAL and
   leaves a dead tuple. At thousands of jobs/sec you are spending database
   capacity and vacuum pressure on queue mechanics instead of on users.
2. **Polling trades latency for load.** We poll every second, so a job can wait
   a second. The fix is `LISTEN`/`NOTIFY`, and it is deliberately not here.
3. **Bloat.** Completed jobs accumulate; every status change leaves a dead
   tuple. This needs a retention policy and **this phase does not implement
   one**.
4. **Concurrency is bounded by connections.** A worker holds a connection for
   the length of a job, and Postgres tops out in the low hundreds without a
   pooler.
5. **It is a work queue, not a message bus.** One consumer per message, no
   fan-out, no replay, no topics.

In one sentence: **when the queue's own traffic starts competing with your
users' queries, move it out.** For a KYC pipeline doing thousands of
applications a day, that is not close.

### Why not Redis, Celery or pg-boss

| Rejected | Why |
| --- | --- |
| Redis + a worker | The transactional-enqueue hole above, plus a second datastore to run, monitor, back up and secure — and Redis' default durability will lose jobs on a hard restart. |
| Celery | Needs a broker anyway, so it inherits Redis' problems, and adds a result backend and a large config surface. Worse, it *hides the mechanics* — you would have a working queue you could not explain. |
| pg-boss | Genuinely the right idea and does this well, but it is **Node-only** and our consumer is Python. The queue schema would be owned and migrated by a JavaScript library that the Python service would have to reverse-engineer — exactly what `/db` exists to prevent. |

pg-boss is what to reach for in a Node-only shop. Two languages sharing one
database is what rules it out here.

### Decisions worth explaining

**`attempts` increments at CLAIM time, not at failure time.** If a job kills
the worker outright — OOM, segfault, power loss — no failure handler ever runs.
Counting at failure time would retry such a job forever, killing every worker
that touches it. Counting at claim time means a poison pill parks itself.

**There is no `failed` status.** A failure is either "try again later" (back to
`queued` with `run_after` in the future) or "stop trying" (`parked`). A
permanent `failed` state would be a bin nobody looks in. `parked` is a dead
letter queue: work set aside for a human once automatic recovery is exhausted.

**A partial index on `status = 'queued'`.** Done jobs become the overwhelming
majority of the table and are never claimed, so keeping them out of the claim
index keeps it small enough to stay in memory. Same technique as the partial
unique index on `decisions`.

**Equal jitter on backoff.** Exponential because hammering a service that is
down makes its outage longer. Jittered because otherwise a hundred jobs that
failed during one outage all retry at the same instant when it ends — a
thundering herd that can knock over the service that just recovered. Half the
delay fixed (so backoff still grows), half random (so the herd spreads).

**A stale-job reaper.** The cost of incrementing attempts at claim time is that
a crashed worker leaves its job `running` forever. Anything `running` past a
timeout goes back to `queued`, or straight to `parked` if its attempts are
already exhausted. Without this the retry design has no ending. The timeout
must exceed the slowest legitimate job — set it too low and it reclaims work
that is still in progress, causing exactly the double-processing the lock
prevents.

**`PermanentError`.** A malformed payload will not fix itself on attempt five.
Handlers can park a job immediately rather than burning retries and an hour of
backoff on something that can never succeed.

### Exactly-once does not exist

A handler calls a vendor API and then marks the job done. Those cannot be one
atomic operation, because an HTTP request cannot be rolled back. If the process
dies in between, the job runs again.

No configuration fixes this. It is a property of distributed systems, not a gap
in the library. What you get is **at-least-once delivery**, and the obligation
that follows is that **handlers must be idempotent** — safe to run twice. Same
word, same idea, arriving from the other direction as the duplicate-webhook
handling in Phase 3.

So the structure is: the handler's *database* writes and the "mark done" update
share one transaction; external I/O sits outside it and is assumed repeatable.

### Evidence

- 2,000 jobs, two workers started at the same instant, both working the full 26
  seconds (998 / 1,002 split). **2,200 execution events, 2,200 distinct jobs,
  zero processed twice.**
- The same test with locking removed: 380 executions of 200 jobs, 180 duplicated.
- Backoff observed: 4.3s then 8.0s (base 5s, equal jitter), then success on
  attempt 3.
- A poison job parked after 3/3 attempts with its error retained; a permanent
  error parked on attempt 1 without burning retries.
- Two abandoned `running` jobs reclaimed: one requeued and completed on attempt
  2, one parked because its attempts were already exhausted.
- A rolled-back transaction left no application, no audit event and no job.

The concurrency proof counts rows in `audit_events`, which rejects `UPDATE` and
`DELETE` at the database level. The evidence sits in a table that cannot be
massaged after the fact.

---

## Phase 3 — Applicant flow, the vendor, and the webhook

### The vendor changed, and why that is worth recording

Sumsub requires a business account. There was no route to sandbox credentials
for a portfolio project, so the stack changed to **Didit**, which offers
self-serve sandbox access and publishes its contract openly.

The replacement turned out to be better for the purpose. Sumsub has no reliable
per-event identifier, so idempotency would have had to key on a hash of the
request body — defensible, but a workaround. Didit's envelope carries
`event_id`, which is exactly what `vendor_events.vendor_event_id` was designed
for in Phase 1, and it sends `X-Timestamp` with a documented ±300s freshness
rule, which turns replay protection from a paragraph of theory into code.

Because there is still no account, the default is a **simulator** inside
`/web` under `mock-vendor/` that speaks Didit's contract: it issues sessions,
shows a verification screen, and posts back correctly signed webhooks. It is
not a separate service — two services and one database is the ceiling, and demo
scaffolding does not get to raise it. `DIDIT_MODE=live` points the same code at
the real API and makes the simulator routes 404.

The README states plainly that the integration is written against the published
contract and **has not been tested against the live API**. A portfolio piece
that overstates what it has verified is worse than one that admits a gap.

### Why the webhook does almost nothing

Didit allows a few seconds to respond and retries on 5xx, 404, timeout or
connection failure — twice, at roughly one and four minutes, then it drops the
delivery permanently.

So doing the real work inline would be self-defeating: ten seconds of
screening, a five-second vendor timeout, a retry that arrives while the first
copy is still mutating the same rows. And the failure reinforces itself — the
slower the processing, the more duplicates, which makes it slower still.

The reframe that settles it:

> **200 does not mean "I have done the work". It means "I have durably taken
> responsibility for this message, and you may stop resending it."**

Once the message is in `vendor_events` and a job is on the queue, that is a
true statement, because both are committed and Phase 2's retry machinery will
see the work through. So the endpoint does four fast local things: verify,
store, enqueue, return.

Two consequences of the retry policy worth designing around: a 500 from a
transient database problem costs the message permanently after about five
minutes — which is an argument for the Phase 4 sweeper, not a nicety. And a
404 counts as retryable, so a typo'd webhook path looks like an outage rather
than a misconfiguration.

### The signature, and the attack it prevents

A webhook endpoint is a public URL with no login. Anyone who finds it can post
to it. Without verification, the obvious attack is to post
`{"status":"Approved","vendor_data":"<their application id>"}` and approve
oneself. That is **forgery**, and it is the first thing anyone would try.

An HMAC signature closes it: the vendor and we share a secret, they sign the
body with it, we recompute. A match proves the message came from someone
holding the secret and was not altered in transit.

**Three implementation details, each a real bug if missed:**

*Verify the raw bytes, before parsing.* The signature covers exactly what was
transmitted. Parsing and re-serialising changes whitespace and key order, so
the signature stops matching — and the tempting fix is to verify against the
re-serialised form, which means verifying a string we constructed rather than
the one they signed. That silently destroys the guarantee while appearing to
work. In Next.js, `await request.arrayBuffer()` gives the raw bytes, and
nothing parses the body until verification passes.

*Compare in constant time.* `===` on strings returns at the first differing
byte, so it takes measurably longer the more leading bytes match, leaking the
signature a byte at a time. `crypto.timingSafeEqual` always compares the whole
buffer. It also throws on length mismatch — which would itself leak length —
so lengths are checked first and a mismatch is rejected outright.

*A signature does not stop replay.* Someone who captures a valid message can
resend it unchanged forever; it really is from the vendor and really is
unaltered.

**We deliberately ignore the vendor's recommended header.** Didit sends three
signatures and recommends `X-Signature-V2`, an HMAC over a canonicalised
re-serialisation — sorted keys, compact separators, unescaped Unicode, whole
floats normalised to integers. Verifying it means reimplementing *their* JSON
serialiser exactly, and each of those four rules is a place where an
implementation can differ subtly. The failure is not a loud crash: it is either
mismatches on some payloads, which look like a vendor outage, or a "fix" that
ends up verifying our own construction. `X-Signature` covers the raw bytes, and
raw bytes have exactly one interpretation. V2 exists for frameworks that cannot
reach the raw body; Next.js route handlers can. `X-Signature-Simple` is ignored
entirely — it authenticates the envelope but not the body, and Didit themselves
say that using it means treating the payload as untrusted.

### Idempotency, and why vendors resend

Vendors resend because **they cannot distinguish "my request never arrived"
from "your response was lost on the way back"**. Both look identical from their
side: bytes sent, nothing returned. Resending is the only safe choice. Add
deploys, 500s, timeouts and load balancers that retry on their own, and
duplicates stop being an edge case and become a guarantee.

Two layers:

**The database refuses the duplicate.** The insert is
`on conflict (vendor, vendor_event_id) do nothing returning id`. No row
returned means we have seen it, and **the job is enqueued only if the insert
actually happened, in the same transaction**. Three deliveries, one row, one
job.

**The handler is safe to run twice anyway**, because Phase 2's at-least-once
delivery means the worker itself can re-run a job. The technique:

> Compute the desired state. Compare with the current state. Write only the
> difference. Log only what was written.

The second run finds an empty difference, so nothing is written and **no new
audit rows appear**. That last part depends on `audit_events` recording state
*changes* rather than handler *invocations* — get that wrong and a perfectly
idempotent handler still leaves a growing trail of identical rows, which looks
exactly like a bug.

### Decisions worth explaining

**Rejected webhooks are not stored.** The verification path is unauthenticated,
so any database write there is a resource-exhaustion vector: an attacker could
fill a table for free. Rejections go to the server log, which is bounded and
rotated. A consequence is that `vendor_events.signature_verified` is true for
every row by construction; the column stays for a future quarantine flow.

**A missing `event_id` is a 400, not a 500.** Without it the message cannot be
deduplicated, so processing it would be unsafe. 400 because it will never
become valid — there is nothing for the vendor to retry.

**Age is checked as a KYC rule, not a form rule.** A regulated business cannot
onboard a minor, so under-18 is refused outright rather than flagged for
review. Hand-rolled validation rather than zod, so the rule is readable as a
rule rather than as a schema line.

**The vendor call happens outside the database transaction.** Holding a
transaction open across a network call to a third party means holding locks for
however long their API takes. The session is created first, then the result is
recorded.

**`vendor_data` is the link, not `application_id`.** Didit's payload has a
field called `application_id`, and it means *their* application — the app in
your Didit account. Ours arrives as `vendor_data`, which we set when creating
the session. Two different things with the same name, one of them a foreign key
in our schema. Reading the wrong one would fail in a way that looks like data
corruption.

**"Approved" from the vendor does not mean the customer is approved.** It means
the document check passed. Conflating a vendor's document verdict with our
onboarding decision would be the single worst mistake available in this phase,
so the handler maps every vendor status onto `checking` and leaves the
lifecycle to Phase 4 and the decision to Phase 5.

**Status only ever moves forward.** Webhooks can arrive out of order, and a
late "In Progress" must not drag an application that has reached `checking`
back a step.

### Two bugs found while testing

**Server Actions passed as inline closures are not server actions.** The verify
and simulator forms originally did
`useActionState(async () => startVerification(id), {})`. That wraps the action
in a *client* function, so the form silently loses progressive enhancement —
the rendered HTML contains no action fields at all and the form only works with
JavaScript. The fix is to pass the server action directly and carry the id in a
hidden input. Found because the no-JS submission produced no `$ACTION` fields
to post.

**A `"use server"` file may only export async functions.** `decodeToken` and
`buildEnvelope` were exported from the simulator's `actions.ts`, which is a
build error, not a style problem: every export in such a file becomes a
callable endpoint. Moved to a plain module. The error message points at the
export rather than at the directive that caused it, which is worth knowing.

### Evidence

- Form submitted, application created at `started`, `gb` normalised to `GB`,
  redirect to the status page. An invalid submission returned five field errors
  and created no row.
- Verification session created, id stored, audit event written, applicant
  redirected to the vendor screen.
- Completing verification posted a signed webhook to the real endpoint over
  HTTP, which stored the event and enqueued a job.
- **The same webhook three times: HTTP 200, 200, 200 — one `vendor_events` row,
  one job.** Responses `{"duplicate":false}` then `{"duplicate":true}` twice.
- Tampered body, wrong secret, missing header and a 6-minute-old timestamp: all
  **401**, `vendor_events` unchanged. The stale one logged
  `timestamp is 361s out of date (max 300s)`.
- **Idempotency**: state snapshot before and after re-running both jobs was
  identical — `checking|4 audit rows|max audit id 2595|3 linked|updated_at
  13:47:30.451893`. `updated_at` being unchanged proves not even a redundant
  `UPDATE` was issued, since the trigger from migration 001 would have bumped
  it.
- Webhook responses returned in ~100ms in development, of which most is
  framework overhead rather than the four statements.

---

## Phase 4 — The worker processing results

### Why a lifecycle rather than a stored result

The tempting design is a `verification_result` column: the vendor says
Approved, write "approved", done.

It breaks the moment anything goes wrong, because a result answers *what* and
never *where*. When a case has sat untouched for an hour, `result is null`
cannot distinguish between: the applicant never started; started and abandoned;
finished but the webhook was lost; finished and our worker crashed mid-job.
Those need four different responses — nudge the applicant, expire the case, ask
the vendor, retry the job — and a result column collapses all of them into
"nothing here yet".

A lifecycle makes the *system's* position explicit rather than only the
vendor's verdict, and that is what makes automated recovery possible at all.
The sweeper's entire premise is the question "which applications are in a state
they should have left by now?", and you cannot ask that of a nullable result.

It is also the honest model of the domain. KYC is a process with stages,
handoffs and waiting, not a function that returns a value.

### Why the state machine makes out-of-order delivery safe

At-least-once delivery plus network reordering means every message may arrive
twice and any two may arrive in either order. "Apply what the message says" is
therefore a bug: the last arrival wins regardless of whether it is the newest.

A state machine turns ordering from something you hope about into something the
code decides. Every update becomes "is this transition permitted from where we
are?", and the answer is a property of the pair of states, not of arrival time.
A late `checking` reaching a `decided` application is not a race that was lost;
it is a transition that does not exist.

The permitted edges:

```
started   → submitted, checking, screening
submitted → checking, screening
checking  → screening
screening → decided
decided   → (terminal)
```

Forward skips are allowed because a fast vendor genuinely can jump a stage.
Backward moves never are. And note what is absent: **nothing reaches `decided`
except `screening`.** That is not tidiness, it is a compliance rule — you may
not decide on a customer you have not screened — enforced by the shape of the
machine rather than by everyone remembering. There is a test whose only job is
to fail if someone adds a convenient shortcut.

The vendor's vocabulary is deliberately kept separate from ours. "Approved"
from Didit means the *document check* passed; whether the customer may be
onboarded depends on sanctions screening and risk scoring, which the vendor
knows nothing about. Every terminal vendor outcome therefore maps to
`screening` — "their part is done, ours begins" — and a test asserts that no
vendor status can ever map to `decided`.

### The late-result problem: both guards, because they answer different questions

The brief offered transition rules **or** vendor timestamps. They solve
different problems, and picking one leaves a real hole.

**Transition rules alone.** They stop stage regression — `decided → checking`
is not an edge. But two results that map to the *same* stage are invisible to
them: a `Declined` and a corrected `Approved` arriving out of order are both a
legal move to `screening`, so the older would be written and nothing would
notice. The status did not regress; the content did, silently.

**Vendor timestamps alone.** They order results correctly, including that case
— but say nothing about legality. A timestamp-only system will happily take an
application from `started` straight to `decided` because the message was newer.
It also puts a third party's clock on the critical path, and skew between their
servers is exactly where such a guard stops working without telling you.

So:

> The **state machine** governs legality — which transitions the process
> permits. The **vendor timestamp** governs recency — which of two legal results
> is newer.

The state machine is the primary guarantee, because it is ours and trusts
nobody else's clock. The timestamp is a tiebreaker *within* a stage. Both were
demonstrated catching cases the other missed.

Two details that matter:

**The comparison is vendor clock against vendor clock, never ours against
theirs.** `vendor_result_at` stores the vendor's own timestamp for the last
result applied. Comparing our observation time to their event time mixes two
clocks and reintroduces the skew problem the guard exists to avoid.

**`vendor_result_at` is nullable, honestly.** Didit's webhook envelope carries
an event timestamp, but their decision endpoint
(`GET /v3/session/{id}/decision/`) publishes no result timestamp — only session
`created_at` and `expires_at`, which mean something else. Rather than pass off a
plausible wrong value, results we PULL leave it null and rely on the state
machine alone. That is sound: a pulled result is by construction the session's
current state, so it cannot be stale.

### Why the sweeper exists

A webhook is a delivery *attempt*, not a guarantee. Didit retries twice —
roughly one and four minutes — then drops the message permanently. A deploy, a
database blip that makes us answer 500, an expired tunnel, a firewall change,
or a bug in our own handler all end identically: the vendor has a verdict, we
never hear it, and the applicant waits forever.

Without a sweeper the recovery path is "a human notices". In a compliance
system an application silently stuck for days is a regulatory problem, not just
poor service.

The sweeper inverts the dependency: rather than trusting the vendor to tell us,
we periodically ask. Webhooks become an **optimisation** that makes the common
case fast, while the sweeper is what makes the system **correct**. A useful way
to think about push versus pull generally: *push for latency, poll for
correctness.*

Decisions inside it:

**A self-rescheduling job, not a timer in the worker loop.** A timer fires in
every worker, so the sweep would run once per worker rather than once. As a
queued job, `FOR UPDATE SKIP LOCKED` already guarantees exactly one claimant,
the next run is visible in the `jobs` table rather than buried in a process, and
it survives restarts. A partial unique index keeps at most one sweep queued —
scoped to `status = 'queued'` only, because the sweeper enqueues its successor
while still `running`, and a constraint covering `running` would make a
recurring job unable to schedule its own next run.

**The stuck threshold must exceed the vendor's retry schedule.** Set it below
and the sweeper races deliveries still in flight, doing work the webhook was
about to do. Harmless, since both paths are idempotent, but wasteful and
misleading.

**`started` is excluded.** An application with no verification session is
waiting on the *applicant*, not the vendor. Chasing those is a reminder email,
a different job.

### The handler re-fetches rather than trusting the webhook body

A verified signature proves a message is genuine and unaltered. It does not
make it the authority on the current state — by the time we process it, the
body may describe a state already superseded.

So the webhook says "something changed" and we then ask the vendor what it is.
This had a benefit I had not fully anticipated until it showed up in testing:
because both webhooks in an out-of-order pair re-fetch, both see the *current*
state, so the second becomes a clean no-change rather than a conflict. Fetching
largely immunises the system against notification ordering; the recency guard
then covers the narrower case of two fetches racing each other.

It also means the webhook path and the sweeper path converge on one function.
That is deliberate: the sweeper exists to be correct when the webhook path
fails, and two implementations would mean two sets of bugs, with the recovery
path exercised only during incidents.

### Decisions worth explaining

**Refusals are audited; no-changes are not.** A duplicate delivery that changes
nothing is the system working normally, and logging it would fill the audit log
with noise and make a genuinely idempotent handler look busy. But "the vendor
sent us a verdict and we refused it" is exactly what an auditor asks about
later, so `vendor_result.refused` records the outcome, the reason, and both
statuses.

**The web service makes exactly one lifecycle transition** — `started →
submitted` when a verification session is created — and guards it in the `WHERE`
clause rather than in TypeScript. Duplicating the state machine in a second
language would mean two copies of rules that must never disagree. Everything
vendor-driven goes through `worker/lifecycle.py`.

**The adapter is per-service, not shared.** Phase 3 built a TypeScript adapter
for creating sessions; this phase builds a Python one for reading results. They
cannot be shared without inventing the internal API that architecture rule 1
exists to avoid.

### Things found while building

**`make_interval(mins => …)` takes an integer**, so a fractional value is a type
error. Switched to `secs =>`, which is double precision and also keeps
sub-minute intervals usable in demos.

**Next.js answers a trailing slash with a 308.** Didit's documented URL ends in
one; Next serves the unslashed form. Following the redirect would have worked,
but the URL convention now lives in each vendor's client, which is what an
adapter is for.

**The `@/` path alias is a bundler feature.** A module imported by both Next and
a plain-Node script must use relative imports; `@/lib/db` resolves under Next
and fails under Node.

**TypeScript cannot check SQL.** A find-and-replace updated a result type to
`result_at` but missed the `select` clause, which still said `updated_at`. The
types agreed with themselves and the query returned undefined. Worth
remembering every time raw SQL looks "obviously fine".

### Evidence

- **Dropped webhook:** the simulator recorded `Approved` and suppressed
  delivery. The application sat at `submitted` with no `vendor_status`. The
  sweeper found it, asked the vendor directly, and moved it to `screening` with
  an audit row reading `"source":"sweeper"` and `"vendor_event_id":null`.
- **Out of order, recency:** after applying `Approved` at 15:40:40, a `Declined`
  the vendor reached at 15:40:20 was refused —
  `stale_result … is older than the last applied result`. The verdict did not
  regress.
- **Out of order, legality:** a *newer* `In Review` was refused with
  `illegal_transition (screening -> checking is not a permitted transition)`.
  Each guard caught a case the other could not.
- **Replay after decided:** status, `vendor_status`, `vendor_result_at` and
  `updated_at` all unchanged; only a refusal row was added.
- **Happy path:** `started → submitted → checking → screening`, driven entirely
  by the worker, each transition with its own audit event naming the vendor
  status and the source.
- 29 state-machine unit tests, no database or network.
