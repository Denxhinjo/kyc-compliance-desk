# Decisions

A record of what was built in each phase, what was decided, what was rejected,
and — where it happened — what turned out to be wrong.

Written to be readable by someone who was not here. It is deliberately not a
changelog: a changelog says what changed, and the useful part is almost always
*why*, and what the alternative would have cost.

> **Synthetic data throughout.** Every applicant, match and decision described
> below is invented. See the [README](../README.md) for the full disclaimer and
> for an unsparing list of what this demo does not do.

## What to read if you only read one thing

Three sections carry most of the reasoning:

- **[Phase 2 — the job queue](#phase-2--the-job-queue)** for why a Postgres
  table is a legitimate queue, and when it stops being one.
- **[Phase 5 — sanctions screening and risk scoring](#phase-5--sanctions-screening-and-risk-scoring)**
  for the threshold argument, which is the one real judgement call in the
  project, and for the measurement that forced it to change.
- **[Phase 4 — the worker processing results](#phase-4--the-worker-processing-results)**
  for how out-of-order and duplicate delivery are made harmless.

## Contents

| Phase | What it covers |
| --- | --- |
| [0 — Foundations](#phase-0--foundations) | Repo layout, why `/db` belongs to neither service, the walking skeleton |
| [1 — Data model and audit log](#phase-1--data-model-and-audit-log) | Five tables, append-only enforcement, and a bug that would have passed silently |
| [1a — `referred` as an outcome](#addendum-referred-is-a-decision-after-all-migration-007) | Reversing an earlier decision without editing an applied migration |
| [2 — The job queue](#phase-2--the-job-queue) | `FOR UPDATE SKIP LOCKED`, measured against the broken alternative |
| [3 — Applicant flow and the webhook](#phase-3--applicant-flow-the-vendor-and-the-webhook) | Signature verification, idempotency, why the endpoint does almost nothing |
| [4 — The worker processing results](#phase-4--the-worker-processing-results) | The state machine, the recency guard, and the sweeper |
| [5 — Screening and risk scoring](#phase-5--sanctions-screening-and-risk-scoring) | Fuzzy matching, the threshold argument, and why auto-rejecting on a name is wrong |
| [6 — The review desk](#phase-6--the-compliance-review-desk) | What an officer is actually doing, and how two of them cannot decide one case |
| [7 — Seed data, stats and retention](#phase-7--seed-data-stats-retention-and-states) | Making synthetic data that does not look synthetic; publishing numbers honestly |
| [Correction — the lifecycle belongs in the schema](#correction-the-lifecycle-belongs-in-the-schema-migration-017) | Phase 4 broke the project's own stated principle; migration 017 fixes it |
| [Correction — version the sanctions list](#correction-version-the-sanctions-list-migration-018) | The rules were versioned and the list was not; what that fixes and what it pointedly does not |

## A note on the things that were wrong

Several sections below record mistakes rather than decisions. That is
deliberate, and they are the parts most worth reading:

- **Phase 1** — a test that printed a perfect result while writing nothing to
  the database, because of how psycopg nests transactions.
- **Phase 4** — a state machine that was correct, paired with a web service
  that transitioned status without consulting it.
- **Phase 5** — a matching rule that would have rejected a third of a real
  customer book, and the "obvious" fix for it that was also wrong.
- **Phase 7** — two successive models of a review backlog, both wrong in
  different directions.
- **Migration 017** — the lifecycle rule was put in Python in Phase 4, in
  breach of the principle this project set out in Phase 0. The correction, and
  why it took four phases to notice.

A document that recorded only the decisions that worked would be a marketing
page.

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

---

## Phase 5 — Sanctions screening and risk scoring

### The licensing decision, first

OpenSanctions is the better dataset and it is what a real firm would license.
It is also **CC-BY-NC**: commercial use requires a paid licence. A portfolio
project that anyone should be able to clone and run cannot ship it, and a repo
that quietly downloads non-commercial data on first run is worse, not better.

So three sources behind one interface:

| Source | Licence | Role |
| --- | --- | --- |
| `synthetic` | fabricated, committed | `git clone && pytest` works offline with no licence question at all |
| `ofac` | **public domain** — a US Government work under 17 U.S.C. 105 | the default for real screening; downloaded on demand, gitignored |
| `opensanctions` | CC-BY-NC | supported, documented, deliberately not shipped |

OFAC's SDN list is also the list with actual legal force behind it. The
downloader pulled **19,385 entries, 9,971 of them with at least one alias** —
and the aliases turn out to matter more than any threshold.

### What sanctions lists and PEPs are, legally

**A sanctions list is a government instrument, not a risk signal.** Providing
funds or services to a listed person is a **criminal offence** — UK Sanctions
and Anti-Money Laundering Act 2018, US OFAC regulations with strict liability
and penalties in the millions per violation. There is no "we assessed the risk
and proceeded".

**A PEP is the opposite kind of thing.** A Politically Exposed Person holds a
prominent public function, or is close to someone who does. It is **not illegal
and not grounds for refusal** — refusing PEPs wholesale ("de-risking") is
something regulators criticise. The law requires **enhanced due diligence**:
senior sign-off, source-of-wealth checks, ongoing monitoring. Which means a
human.

Getting these backwards is the classic error: auto-rejecting PEPs (bad, and a
regulator will ask why) and manually reviewing sanctions hits (worse, because
you transacted while deciding). The points table encodes the distinction, and a
test asserts that no PEP signal can ever cause an automatic rejection.

### Why fuzzy name matching is genuinely hard

Measured, not asserted. Normalised, scored as `max(token_set_ratio, ratio)`:

```
        score  applicant            list entry
MATCH   100.0  Xi Jinping           Jinping Xi
MATCH   100.0  Vladimir Putin       Vladimir Vladimirovich Putin
MATCH   100.0  Jose Munoz           José Muñoz
MATCH    94.7  John Smith           Jon Smith
not      91.7  Maria Garcia         Maria Garzia          <- false positive
MATCH    89.7  O'Brien Patrick      Patrick OBrien
MATCH    87.0  Ahmed Hassan         Ahmad Hasan
MATCH    81.5  Sergey Ivanov        Sergei Ivanoff
not      81.5  Ahmed Hassan         Ahmed Hasan Ali       <- false positive
MATCH    80.0  Mohammed Al-Sayed    Muhammad Al Sayyid
not      80.0  Ahmed Hassan         Ahmed Hussein         <- false positive
not      80.0  John Smith           Jane Smith            <- false positive
```

**Look at 80.0–81.5.** A true match and three false positives score
*identically*. There is no threshold that separates them, because **the
information needed to separate them is not in the strings.**

The specific difficulties:

- **Transliteration.** محمد has no canonical Latin form: Mohammed, Muhammad,
  Mohamed, Mohammad. `Sergey Ivanov` and `Sergei Ivanoff` are one person and
  score 81.5.
- **Name order.** Chinese and Hungarian put the family name first; Spanish uses
  two surnames; Arabic chains patronymics. `token_set_ratio` handles reordering,
  which is why Xi Jinping scores 100 — and the same blindness inflates
  `Ahmed Hasan Ali` to 81.5.
- **Common names.** `Ahmed Hassan` in Egypt is `John Smith` in England. A 90
  against a common name carries far less information than a 90 against
  `Ryszard Wojciechowski`. Real systems weight by name frequency; we do not, and
  that is a stated limitation.
- **Diacritics and punctuation** are the easy ones, and normalisation fixes them
  outright. `José Muñoz` went from 80 to **100**. Preprocessing bought more than
  any scorer choice did.

### The threshold, and what each direction costs

Measured across those cases:

| Threshold | True matches caught | False positives |
| --- | --- | --- |
| 80 | 8 of 8 | **4 of 7** |
| 85 | 6 of 8 | 1 of 7 |
| 88 | 5 of 8 | 1 of 7 |
| 92 | 4 of 8 | 0 of 7 |

At 80 you catch everything and drown. At 92 you are clean and you **miss
`Ahmed Hassan` against `Ahmad Hasan`** — a textbook transliteration pair.

**So there is no single threshold.** Bands instead:

| Band | Points (sanctions) | Effect |
| --- | --- | --- |
| ≥ 92 confirmed | 60 | guarantees review; cannot refuse alone |
| 85–91 probable | 45 | review |
| 80–84 weak | 20 | recorded, contributes, never decides |
| < 80 | — | not recorded |

A single line forces a binary decision on data that cannot support one. Bands
do not pretend to: the ambiguous range routes to a human, which is what humans
are for. Everything at or above 80 is written to `screening_results` including
the weak matches — an officer who cannot see the near-misses cannot judge
whether the strong one is a coincidence.

### Two bugs the measurements found, both serious

**1. `token_set_ratio` returns 100 for containment.** That is what makes
`Vladimir Putin` correctly match `Vladimir Vladimirovich Putin`. It also makes
`Ibrahim Osei` match a list entry reading `DR. IBRAHIM` at **100**, and
`Sarah Khan` match `KHAN`.

OFAC SDN contains **951 single-token entries**. Against a 400-applicant
population that one flaw produced a 31.5% confirmed-sanctions rate and a
**31.8% automatic rejection rate**. It would have refused a third of a real
customer book, and every rejection would have looked perfectly justified in the
logs.

**2. The first fix was also wrong.** Requiring one *exactly* shared token
seemed obvious — and `Ahmed Hassan` and `Ahmad Hasan` share no exact token at
all, so the headline transliteration case would have been silently discarded.

The rule that works: **at least two name PARTS must correspond**, where parts
are compared fuzzily at a loose per-token threshold of 70. Loose because
transliteration lives inside words (`ahmed`/`ahmad` is 80, `mohammed`/`muhammad`
75, `sayed`/`sayyid` 72) while genuinely different parts score far below
(`hassan`/`hussein` 46, `john`/`jane` 50). At the token level there is a wide
gap, precisely because the comparison is not diluted by the parts that do match.

It is also how a human compares two names: sharing a first name is not a match;
sharing a first name and a surname is. All 14 measured cases come out right,
including `Mohammed Al-Sayed` against `Muhammad Al Sayyid`, which nothing else
rescued.

### THE FINDING: auto-rejecting on a name match is wrong

The brief specified <20 approve / 20–79 review / 80+ reject, and asked whether
that produces a sensible split. The routing thresholds are fine. **The points
were not**, and the measurement is unambiguous.

With a confirmed sanctions match worth 80 — enough to refuse on its own —
scored against the real OFAC list over 2,000 synthetic applicants:

```
1,895 applicants were on no list at all.
  matched at confirmed    11    0.58%
  AUTO-REJECTED despite being on no list: 11   (0.58%)
    'Carlos Garcia'    matched 'Carlos Alberto GAXIOLA GARCIA'
    'Andrei Petrov'    matched 'Andrei Yuvenalyevich PETROV'
    'Carlos Fernandez' matched 'Carlos Ariel FERNANDEZ CONCEPCION'
```

**0.58% of legitimate customers refused by a string comparison.** On a book of
100,000 that is 580 real people, each with a genuine grievance and no idea why.

So `confirmed` was lowered from 80 to **60**: enough to guarantee review, not
enough to refuse anyone by itself. Reaching 80 now requires a second
independent signal — a failed document check, a call-for-action jurisdiction.

The effect, same population and seed:

| | confirmed = 80 | confirmed = 60 |
| --- | --- | --- |
| approve | 87.7% | 87.7% |
| review | 6.8% | **11.6%** |
| reject | 5.6% | **0.8%** |
| false auto-rejections | **0.58%** | **0.11%** |
| recall, exact plants | 100% | 100% |
| recall, transliterated | 97.0% | 97.0% |

**Recall did not move.** The match is still found, still recorded, still shown
to an officer. Only the automatic refusal was withdrawn. The cost is review
volume: 6.8% to 11.6%, roughly one extra case per twenty applicants.

That trade is the right way round. A false negative is a regulatory breach; a
false positive is an officer's hour. Moving 4.8% of cases from "refused
automatically" to "looked at by a human" buys a fivefold reduction in wrongly
refused customers, and the two false rejections that remain both have a second
independent signal behind them — which is the design working, not failing.

It also matches actual practice. Firms do not refuse customers on a fuzzy name
match; a potential match is escalated and a human confirms identity before the
firm acts. **Automated rejection on name similarity alone is not the cautious
option — it is an untested one.**

A property test now asserts the general rule: no single signal can auto-reject.

### The honest verdict on the split

87.7% / 11.6% / 0.8% is sensible for a consumer book, with these caveats
stated plainly:

- **The reject rate is inflated by the test population.** Genuine sanctions
  matches were planted at ~5%; in reality they are well under 0.1%. On a real
  book the automatic rejection rate would be a small fraction of 0.8%.
- **Review volume is driven by the document check, not by name matching.**
  `document_check_declined` and its siblings account for most of the 11.6%.
  Good matching produces surprisingly little review volume; failed identity
  verification produces most of it.
- **Recall on transliterations is 97%, not 100%.** Two of 67 were missed, both
  two-token names where the single mutated character broke token alignment:
  `OOO RADIOTEKHSNAB` became `OOu RADIOTEKHSNAB` and `ooo`/`oou` scores 66.7,
  below the 70 alignment floor, leaving only one aligned part. The two-part rule
  has no margin on two-part names. Stated rather than smoothed over: **each of
  those two is a breach.** The mitigation in a real deployment is screening
  against several lists and on more than the name.
- **Single-token list entries are not matched at all.** 951 OFAC entries are
  unmatched by construction. Real systems handle mononyms with passport numbers
  and dates of birth, which we do not have.

### Why pure functions, concretely

`scoring.py` touches no database, no socket and no clock. Everything arrives as
an argument, including the thresholds.

The payoff was not theoretical. The entire threshold investigation above —
2,000 applicants scored against a 19,385-entry list, twice, with different
points tables — ran in a second with no database, no worker and no queue. Had
scoring reached into Postgres, that analysis would have needed fixtures,
teardown and a lot more patience, and I would probably have done less of it and
found neither bug.

Also: determinism means an old decision can be re-derived rather than merely
believed, which is what "explain this decision" requires. A clock read inside
would have made a case score differently on re-run.

### Why points and not a model

Not because a model would score worse — it might well score better. Because of
what has to happen after the score exists.

- **An officer acts on the reason, not the number.** "Score 65" tells them
  nothing. "Probable sanctions match at 87, plus a high-risk jurisdiction" tells
  them what to check first.
- **A regulator asks why this person was refused.** "The model said so" is not
  an answer; UK and EU rules on automated decision-making give individuals a
  right to an explanation and to contest it.
- **The training data does not exist.** You refuse the risky applicants, so you
  never learn whether they would have been fine. The feedback loop supervised
  learning needs is structurally absent.
- **Proxy discrimination.** A model trained on past decisions learns past bias,
  and name and nationality are excellent proxies for ethnicity. A points table
  makes every such input visible and arguable.
- **Rules change by law, not by retraining.** When FATF adds a country you edit
  a list and can state exactly which decisions change.

The point is not accuracy. **The output has to be an argument, not a
prediction.**

### Other decisions

**Strongest hit, never the sum.** Five weak matches against `Ahmed Hassan` are
evidence that the name is common, not that the person is five times more likely
to be sanctioned. Summing them would refuse an ordinary applicant for having an
ordinary name. The count is still reported, worth zero points, because thirty
weak hits and one hit at 97 are very different situations and the score cannot
tell them apart.

**A conflicting date of birth downgrades one band** rather than scaling points,
because the result has to be sayable in a sentence: "the name matched at 93 but
the listed date of birth is 1944 and this applicant was born in 1983, so it is
treated as probable rather than confirmed". A multiplier produces a number
nobody can defend in those terms. The asymmetry is deliberate: a conflict is
strong evidence of two different people, while agreement is weak evidence of
one.

**Referral leaves the application in `screening`, not `decided`.** A referral is
a decision about *process*, not about the applicant, so the case is not decided
until a human decides it. Phase 6's queue is `screening` plus a `referred`
decision.

**Automatic decisions justify themselves in words.** `decisions.reason` is
constrained non-blank, and a row saying only "score 84" would satisfy the
constraint while defeating its purpose, so the reason is assembled from the
signal sentences.

**FATF lists live in code, not in a database**, so a change to them is a
reviewable commit. They carry an "as at" date and a warning to re-check against
fatf-gafi.org, because they are legal lists with publication dates, not
constants.

### Evidence

- 121 unit tests, no database and no network: every signal in isolation, both
  routing boundaries (19/20, 79/80), the band edges, and properties that must
  survive any change to the rules.
- The measured name-matching table above, pinned as regression tests — including
  one whose only job is to fail if someone adds a `decided` shortcut, and one
  asserting that the bands genuinely overlap.
- 19,385 real OFAC entries downloaded and screened against.
- End to end: `Ahmed Hassan` matched `Ahmed Hassan Mahmoud` (an alias of
  `Ahmad Hasan`) at 100, scored 60, referred to a human with the reason
  `Risk score 60. Sanctions match against "Ahmed Hassan Mahmoud" on
  SYNTHETIC-SDN at 100% name similarity (confirmed).`
- Idempotency: re-running screening left `risk_score`, `risk_scored_at`,
  `screening_results`, `decisions` and `audit_events` byte-identical —
  `score unchanged at 60 — nothing rewritten`, `already referred — no second
  referral`.
- The stored `risk_signals` blob carries the score, the routing, every signal
  with its points, its evidence and a human sentence, the thresholds in force
  and the ruleset version — so the routing is explainable after the bands move.

---

## Phase 6 — The compliance review desk

### What the officer is actually doing

Not "reviewing an application". Answering one question: **is this the person on
the list, or someone who shares their name?**

That is almost the whole job. Phase 5 found that `Carlos Garcia` matches
`Carlos Alberto GAXIOLA GARCIA` at 100% and cannot tell whether that is the same
human — the information needed is not in the strings. The officer resolves it
using things the matcher never sees: date of birth, nationality, the document
result, the address. Then they write down what they concluded.

Every layout decision below follows from that one sentence.

### Why the evidence is side by side and not behind tabs

**Because the task is comparison, not reading.**

The applicant's date of birth and the list entry's date of birth have to be
visible at the same moment, because the officer is holding one against the
other. Put them on separate tabs and the officer carries a date in their head
while clicking — and at case ninety of a shift, that is exactly where mistakes
come from.

Tabs are fine for material read in sequence. They are actively harmful for
material read against each other. So the case view is a grid: applicant
details, document check and screening matches all on screen together, with the
decision panel beside them rather than below.

The same reasoning puts the score and the flag reasons above the fold. The
officer's first judgement is *how much attention does this deserve*, and they
should be able to make it before scrolling.

In the working demo this shows up as, for example, applicant date of birth
`1975-11-02` sitting directly opposite listed date of birth `1975-04-12` —
which is the entire decision, visible in one glance.

### Why the reason is mandatory, and what an auditor does with it

An auditor — internal, external, or the regulator — samples decisions and asks
one question per case: **was this decision reasonable on the evidence available
at the time?**

They are not checking whether the outcome was right. They are checking whether
a competent person, seeing what your officer saw, could have reached that
conclusion. That is only answerable if the officer wrote down what they
concluded and why.

Concretely, they use the field to:

- **Test overrides.** An approval on a case scoring 65 is fine if the reason
  reads "PEP match is a different individual: listed DOB 1962, applicant 1988,
  different nationality". The same approval reading "looks ok" is a finding
  against the firm.
- **Detect pattern behaviour.** Thirty cases approved with an identical sentence
  means somebody found a shortcut, not thirty individual judgements.
- **Reconstruct intent after the rules changed.** Phase 5 stores the thresholds
  in force; the reason supplies what the human was thinking.
- **Establish that the control exists at all** — that a person is exercising
  judgement rather than software producing numbers.

So the constraint lives in the **database** (`decisions.reason` is `NOT NULL`
with a non-blank `CHECK`, from migration 004), with a minimum length in the
action on top. Neither can force anyone to think, and pretending otherwise
would be silly. What they do is make the *absence* of a reason impossible, so a
sampling auditor always has something to read — and a one-word reason is itself
a finding they can act on.

### Why the timeline is read-only, and how that reaches back to Phase 1

Because it is **physically read-only**. Migration 006 put `BEFORE UPDATE`,
`BEFORE DELETE` and `BEFORE TRUNCATE` triggers on `audit_events` that raise an
exception. There is no edit control to build, because the write would be
refused.

That is the whole append-only design finally surfacing in a user interface. The
timeline is not a log the firm curates — it is **evidence**, and its value comes
entirely from nobody being able to tidy it. A correction appears as a new event
saying something was corrected, never as a changed row. The page says so in
those terms, because a reader who does not know the triggers exist would
otherwise assume "read-only" meant "we chose not to add a button".

### Two officers, one case: how the guard works

Three layers, doing three different jobs. Only the last one is a guarantee.

| Layer | Mechanism | What it gives |
| --- | --- | --- |
| UI | a decided case renders with no buttons and no reason box | stops the honest mistake |
| Transaction | `SELECT … FOR UPDATE`, then re-read | determinism, and a good error message |
| Database | partial unique index `decisions_one_terminal_per_application` | **impossibility** |

**Plain `FOR UPDATE`, not `FOR UPDATE SKIP LOCKED`.** Same lock as the job
queue, opposite modifier, and the reason is a human one rather than a technical
one.

The queue *skips* contended rows because another worker will take them and
nobody needs telling. The desk must do the opposite: the loser of a race is a
**person waiting for an answer**, and silently doing nothing is the worst
possible response. So the second transaction **blocks** until the first commits,
then re-reads, finds the case decided, and can therefore report *who* decided
it. `SKIP LOCKED` would have made the second officer's submission vanish.

The re-read has to happen **after** the lock is acquired. Anything read before
it is a guess about a row someone else may have been changing.

**What was deliberately not built:** a soft claim ("Alice is looking at this").
Real desks have it and it is genuinely useful, but it is advisory — it reduces
collisions and cannot prevent them. Building it without the index underneath
would be worse than not building it, because people would trust it.

### The security trap worth knowing about

`/desk/*` is guarded in the **layout**, not in Next.js middleware, because
middleware runs on the Edge runtime where `node:crypto` does not exist. The
workarounds end either in a broken build or in a hand-rolled signature on the
wrong primitive. A layout is a Server Component on the Node runtime, it wraps
every page beneath it, and there is one of it.

**But a layout guard does not protect Server Actions.** An action is a POST
endpoint that exists independently of any page: anyone who knows its id can call
it without ever rendering the layout that was supposed to be guarding it. So
`requireStaff()` is the first line of the decision action, and the layout check
is only a convenience for navigation. Verified by calling the action with no
session at all: `303 → /login`, nothing written.

### The login, and its honest limits

One account from `.env`, an HMAC-signed cookie, no framework.

- Signed, not encrypted: signing stops forgery, and the payload contains only an
  email and two timestamps, none of it secret.
- `httpOnly` so a cross-site scripting bug elsewhere cannot steal it;
  `sameSite=lax` as the basic CSRF defence for the decision action.
- Both email and password compared with `timingSafeEqual`, and **both
  comparisons always run** — short-circuiting on `&&` would make a wrong email
  answer measurably faster than a wrong password, which tells an attacker
  whether an address exists. One error message for both failures, for the same
  reason.
- Expiry is checked **after** the signature. Reading an unverified payload to
  decide anything, even whether to bother verifying, is how signature checks get
  quietly bypassed.
- A fixed-window rate limit on failures, in memory — and therefore honestly
  limited: it resets on restart and is per-process. For one demo account that is
  fine; for real users it belongs in the database. Having none at all would
  leave a login endpoint anyone can brute-force at line rate.

**The password sits in `.env` in plaintext, and hashing it there would be
theatre** — the hash and the signing secret would live in the same file, so an
attacker who can read one can read the other. What actually matters at this
scale is constant-time comparison, a signed cookie, and never logging
credentials. With real users the gaps are specific rather than vague: per-user
rows with scrypt or argon2, rate limiting that survives a restart, session
revocation (a signed cookie is valid until it expires and there is no way to
cancel one), password reset, and a second factor. Naming which pieces are
missing is more useful than implying none are.

### Design decisions for a tool used six hours a day

- **Denser than the applicant-facing pages** — 13px, tight rows, monospace for
  ids and scores. Many cases visible at once.
- **Colour carries meaning only**: score bands, decision outcomes, waiting time.
  Anything coloured decoratively steals attention from a signal that needs it.
- **Oldest first, always, with no sort control.** The longest-waiting case has
  the most regulatory exposure, and a queue that lets you sort it away will
  eventually hide it for a fortnight. Waiting time turns amber after a day and
  red after three.
- **Keyboard**: `j`/`k` and arrows move, `Enter` opens. Officers arriving from
  other compliance software try these before reading anything.
- **`a` and `r` focus the reason box rather than deciding.** Deliberate: a
  single keystroke must never record a decision. The shortcut saves the reach
  for the mouse; it does not skip the part where the officer says why.
- **Weak screening matches are shown, not hidden.** Whether the strong match is
  a coincidence is far easier to judge when you can see how many others came
  close — and an auditor asking "what did you consider?" needs an answer that is
  not "whatever the threshold let through".

### A bug this phase created and fixed

`npm run build:check` (added in Phase 3 to stop production builds clobbering a
running dev server) writes to `.next-check`, and Next.js adds
`.next-check/types` to `tsconfig`'s `include` when it builds there. A leftover
directory meant `tsc --noEmit` typechecked the route validator from an **old**
build alongside the current one, producing a baffling error about a route not
satisfying the constraint `"/"` in a generated file nobody wrote.

Fixed by having the script remove its output directory before and after, so
there is never a stale one for `tsc` to find. Worth recording because the error
message points nowhere near the cause.

### Evidence

- Signing in: wrong password returns "Those details do not match an account";
  the correct one sets an `HttpOnly; SameSite=lax` signed cookie and redirects
  to `/desk`. `/desk` without a session is `307 → /login`.
- The queue renders six cases oldest first with waiting time, score, country and
  the top flag reason on one line.
- A case view shows applicant DOB `1975-11-02` beside listed DOB `1975-04-12`,
  the match at 100% attributed to its alias (`Ahmed Hassan Mahmoud`, alias of
  `Ahmad Hasan`), the reasons list, the decision panel and the full timeline.
- **The race:** two clients sharing one session, both POSTing a decision at the
  same instant. One recorded an approval; the other was told
  `alreadyDecidedBy: "staff:officer@example.com"`. The database holds exactly
  **one** terminal decision and exactly **one** human `decision.recorded` audit
  event, and the application is `decided`.
- Reloading the decided case: **zero** approve buttons, **zero** reject buttons,
  **zero** reason boxes — the panel shows the outcome, who, when, the score at
  the time, and the written reason.
- A stale page POSTing again, bypassing the UI: refused, same message, nothing
  written.
- A direct `INSERT` with no application code involved:
  `ERROR: duplicate key value violates unique constraint
  "decisions_one_terminal_per_application"`.
- The action called with no session at all: `303 → /login`.
- A one-word reason: refused, and **0** terminal decisions created.

---

## Phase 7 — Seed data, stats, retention and states

### Making synthetic data that does not look synthetic

The tells first, because every choice in the seeder is aimed at one of them:

1. **Flat timestamps.** `random.uniform(start, end)` produces a histogram with
   no shape. Real arrivals have a daily rhythm and a weekly one, and a
   histogram is the first thing anyone plots.
2. **Uniform everything.** Ages evenly spread 18–80; every name used exactly
   twelve times out of five hundred.
3. **No correlation between fields.** Random name crossed with random country
   gives "Wolfgang Schmidt, Vietnam".
4. **Every journey complete.** Real funnels are full of people who started and
   wandered off, and a dataset where everyone finishes quietly flatters every
   percentage computed from it.
5. **Tidy outcome ratios.** Exactly 33/33/33 is the fingerprint of someone
   generating to a specification rather than observing a process.
6. **Machine-even durations.** Everything taking two to four hours, no tail.
7. **Insertion order not matching time order.** Generate in random order and
   the sequential `id` stops being monotonic with `created_at` — which is
   precisely what a backfill looks like.

The fixes: rejection sampling on an hour-of-day curve times a weekday weight;
Zipf-biased name frequency; a lognormal age distribution; lognormal durations
with a long right tail; 14% of applicants who never finish; and rows inserted
**chronologically**, so ids and timestamps agree.

The single most valuable detail is that **officers work office hours**. It is
what makes the queue build overnight and across weekends and drain on the next
working morning, and it is what makes median time-to-decision a real number
rather than a constant. A Friday evening referral genuinely waits until Monday.

### The outcomes are computed, not assigned

The seeder does not decide who gets approved. It generates applicants and
vendor results, then runs the **real** Phase 5 code — the actual
`SanctionsIndex` against 19,385 OFAC entries, the actual pure
`score_application()` — and routes on the real thresholds. Only the human
decisions on referred cases are simulated.

So the published figures measure the pipeline rather than assumptions about it.
The consequence is that the outcome mix is lopsided, because that is what the
scoring produces. An even split would itself have been the tell.

### Two wrong models of a backlog

Recorded because the second one was wrong in a more interesting way than the
first.

**Model one:** officers always clear the queue within the working day. Result:
one case pending out of five hundred. Optimistic rather than realistic — real
desks carry a backlog.

**Model two:** every referral has a flat 18% chance of still being open,
independent of age. Result: a queue whose oldest case had been waiting 32 days.

That number is not a backlog, it is a control failure — but more importantly
the *model* was wrong, not the roll. A flat probability implies a case from six
weeks ago is exactly as likely to be outstanding as one from yesterday, and
real queues do not behave that way: old items get chased, escalated and
cleared.

**Model three**, the one in the code: the probability decays with age, plus a
small floor for the genuinely stuck — the case waiting on a document the
applicant will never send. Most of the queue is recent; one or two stragglers
are old, which is both true to life and exactly what the red waiting-time
styling on the desk exists to surface.

The distinction matters: changing model two to model three was fixing a
modelling error, not tuning the data until the number looked better. The
resulting oldest-case figure is still unflattering, and it is still published.

### The honesty problem

The seeder writes **backdated `audit_events`** — the one table in this system
meant to be unvarnished truth, fabricated six weeks deep.

So every seeded row carries `"seeded": true` in its details. Anyone can filter
them out, and nobody reading a timeline can mistake invented history for real
history.

It also cannot be undone. `audit_events` rejects `DELETE`, and that rule binds
the script that wrote it as much as anyone else — so the seeder refuses to run
twice without `--force`, and a genuine reset means dropping the schema and
re-migrating. Discovering that the constraint applied to us was a good sign
that it was built properly.

### Publishing numbers honestly

Three rules, all of them enforced on the page rather than in a style guide:

**Median, never mean.** One case stuck for three weeks moves a mean enormously
and a median not at all — and the mean is the one that would get quoted. The
90th percentile is shown alongside, because a median on its own hides a bad
tail, and the tail is where the operational problem lives.

**Every percentage carries its denominator.** "87.6% auto-decided" means
nothing without "of 421 decided". A percentage with no base is a number nobody
can check, and the whole point of publishing them is that they can be.

**Below twenty decisions, the page says so rather than showing a figure.** A
median computed from nine cases is not a median, it is an anecdote with a
decimal point.

And one that only appeared once there were real numbers to look at: **the
median had to be split by who decided.** The combined figure came out at nine
minutes, which is true and useless — with ~88% of decisions automatic and
taking seconds, the machine drowns the number anyone actually wants. Reviewed
cases take **13.9 hours**, and a single combined median hides that completely.
That is the kind of figure that is misleading precisely because it is accurate.

### Queue retention, the loose end from Phase 2

`jobs.cleanup` deletes `done` jobs by age and **never touches `parked`**.
Parked is the dead letter queue — the only record that a piece of work failed
and was given up on — so deleting it deletes the evidence that something went
wrong. Parked jobs leave when a human deals with them, not when a timer fires.
The job warns on every run while any remain.

The contrast worth drawing: **the job queue is operational plumbing and has a
retention policy; `audit_events` is evidence and has none.** One is machinery,
the other is the record of what the machinery did, and only one of them is
allowed to forget.

Honest cost: `DELETE` leaves dead tuples for vacuum, and a queue that churns
hard enough will spend real effort on that. At very high throughput the answer
is a partitioned table and `DROP PARTITION`, which is a metadata operation
rather than a row-by-row delete. At this scale `DELETE` is right, and saying
when it would stop being right is more useful than implying it always is.

### Empty and loading states

The empty queue is treated as **success, not an error** — it is a state a
visitor may well land on, and it should read as "all clear" while explaining
what would put a case there.

Loading skeletons mirror the real layout rather than showing a spinner, so
nothing jumps when the data arrives. That matters most on the case view, which
an officer opens hundreds of times a day: a layout that settles differently
each time is a layout you cannot build muscle memory against.

The not-found page deliberately does not distinguish "no such application" from
"not yours". An application id is a bearer token in this system — anyone
holding it can see the status page — so telling the difference would let
someone probe for which ids exist.

### Evidence

- 500 applicants seeded over six weeks in 13 seconds, scored against 19,385
  real OFAC entries.
- The arrival histogram has genuine diurnal shape: 1 arrival at 02:00, 34 at
  10:00, 54 at 20:00, tailing to 14 at 23:00.
- 421 of 500 reached a decision; 87.6% of those without a human; 9 minutes
  median automatic, 13.9 hours median reviewed.
- Retention: 40 `done` jobs older than seven days deleted, 5 recent ones kept,
  the single `parked` job untouched and warned about on every run.

---

## Correction: the lifecycle belongs in the schema (migration 017)

### What was wrong

Phase 0 set out this project's central structural principle:

> `/db` belongs to neither service. Two languages share one database, so the
> schema cannot be owned by either one's ORM — or by either one's code.

Phase 4 then built the application lifecycle as a state machine in
`worker/lifecycle.py` and **did not follow that principle**. The rules were
correct, carefully explained and well tested. They were also in Python, in one
of the two services, governing a table both services write to.

The TypeScript side made two status transitions without ever consulting them:

```
web/src/app/verify/[id]/actions.ts:46   set status = 'submitted' ... where status = 'started'
web/src/app/desk/[id]/actions.ts:110    set status = 'decided'   ... where status = 'screening'
```

Both were guarded by a `WHERE` clause, and both were correct. That is not the
problem. The problem is what kind of thing was holding the line: **a convention,
duplicated across two languages, with no mechanism to keep the copies honest.**
The next transition added in TypeScript would not have had the guard, and
nothing anywhere would have complained.

I flagged this in Phase 4's own notes and shipped it anyway, which is worth
recording plainly. It is easy to state a principle in Phase 0 and much harder to
notice, four phases later, that the convenient thing you are about to do
violates it. The rule was in Python because the worker needed it first, and
"the worker needed it first" is not an architectural reason.

### Why a schema rule rather than a shared library

The obvious alternative is to extract the transition table into something both
services import. That fails for the same reason an ORM would: there is no shared
runtime. One service is Node, the other CPython. Sharing it means either
duplicating it — which is what we already had — or inventing a service call
between the two, which architecture rule 1 exists to forbid.

The database is the only thing both services genuinely share. A rule expressed
there is a rule neither can ignore, in any language, including `psql`.

That is the same argument that put migrations in `/db` in the first place. This
migration is not a new idea; it is the original idea, applied somewhere it
should have been applied already.

### What is a schema rule and what is not

Not every rule belongs in the database, and it is worth being precise about why
this one does.

**The lifecycle is an invariant of the data.** "An application that has not been
screened cannot be decided" is true of every row at every moment, regardless of
which service is writing, what feature is being built, or whether anyone
remembered. It also carries legal weight: it is *you may not decide on a
customer you have not screened*.

**Risk scoring is not.** It is a policy that changes, is versioned, needs
explaining to an officer, and belongs in `scoring.py` as pure functions with a
ruleset version stored alongside every result. Putting it in the database would
make it harder to test, harder to read and harder to change — all the opposite
of what it needs.

The distinction: **invariants that must hold for all data go in the schema;
policy that is expected to change goes in code.**

### How it is enforced

A `BEFORE UPDATE ... FOR EACH ROW` trigger on `applications`, deliberately built
to the same pattern as the append-only triggers on `audit_events` from migration
006 — a plpgsql function that raises with a real SQLSTATE
(`restrict_violation`), attached by a trigger. Following the existing precedent
rather than inventing a second style of guard.

One difference from that precedent, which is deliberate. The `audit_events`
triggers are `FOR EACH STATEMENT`, because the operation is banned outright:
there is nothing to inspect, so a statement-level trigger is both cheaper and
also fires on statements matching zero rows. Here the operation is permitted and
it is the *values* that decide, which can only be examined a row at a time.

The transitions live in a table, `application_transitions`, rather than being
hardcoded inside the function. That is the one design choice beyond the obvious,
and the reason is that a table can be **read**: `lifecycle.py` keeps a copy so
the worker can decide without a round trip, and a test now compares that copy
against these rows. Hardcoding the list in plpgsql would have made the Python
mirror something we trust. A table makes it something we check.

`lifecycle.py` now says so at the top of the declaration, in as many words:
*this is a mirror, the database is authoritative*. If the two ever disagree the
database wins by construction, because it refuses the write.

### Two things found while building it

**`BEFORE ROW` triggers run before `CHECK` constraints.** The first version of
the test asserted that setting a status to `'aproved'` would raise a
`CheckViolation` from migration 001's constraint. It does not: the lifecycle
trigger sees the value first, finds no edge for `started -> aproved`, and
raises `RestrictViolation`. The CHECK never gets a look on that path.

Both are still needed, and the division is now explicit in the tests: the
trigger governs which **moves** are legal and only fires on `UPDATE`; the CHECK
governs which **values** may exist at all, and is what guards `INSERT`, where
there is no previous row to transition from.

**Most writes to `applications` are not transitions.** Writing a risk score,
linking a vendor session, bumping `updated_at` — the lifecycle has nothing to
say about any of them. The trigger short-circuits when `NEW.status IS NOT
DISTINCT FROM OLD.status`, which also makes a self-transition a no-op rather
than an error. That matters for idempotency: handlers re-issue the same write
by design, and refusing it would turn a harmless duplicate into a failure.

### What this deliberately does not do

It does not constrain `INSERT`. An insert creates an application; it does not
transition one, and there is no `OLD` row to reason from. In practice every
caller inserts at `'started'`, and the CHECK limits the column to five values —
so the remaining gap is that a direct `INSERT` could create a row already at
`'decided'`. Closing it would mean deciding whether backfills and restores are
allowed to bypass the lifecycle, which is a larger question than this migration
should answer quietly.

### Evidence

**From `psql`, with no application code involved:**

```
update applications set status = 'decided' where id = '<a started case>';
ERROR:  illegal application lifecycle transition: started -> decided
HINT:   Permitted transitions are rows in application_transitions.
        Nothing reaches 'decided' except from 'screening'.

update applications set status = 'checking' where id = '<a decided case>';
ERROR:  illegal application lifecycle transition: decided -> checking
```

**From TypeScript, through `pg`, with the `WHERE` guard deliberately removed** —
every one of these would have succeeded before this migration:

```
started -> decided    ✓ refused by the database
decided -> checking   ✓ refused by the database
decided -> submitted  ✓ refused by the database
started -> submitted  ✓ accepted   (the trigger is not simply blocking everything)
```

**Tests:** 146 passing, up from 121. The 25 new ones are the first
database-backed tests in the project — necessarily so, because the thing under
test *is* the database, and asserting it from Python alone would only be testing
a copy of the rule. They skip cleanly when no database is reachable, so the pure
suite still runs on a fresh clone. They cover every legal edge, ten
representative illegal ones, the compliance rule as its own named test, and the
mirror-versus-schema comparison.

**Nothing broke:** the seeder still drives 150 applicants through the full
lifecycle, and a live application still runs
`started → submitted → screening → referred` through the real webhook, worker
and scoring path.

---

## Correction: version the sanctions list (migration 018)

### The inconsistency

`screening_results.source` said `'ofac'`. It did not say *which* OFAC.

So the question an auditor actually asks — **"was this person screened against
the list as it stood on the day you approved them?"** — was unanswerable. Not
hard to answer: unanswerable. The data did not contain it.

What makes that worth fixing rather than merely noting is the inconsistency
with everything else this project does. A decision has two inputs: the rules
and the list. Phase 5 versioned the rules with some care — every scored
application stores `risk_ruleset_version` and the thresholds in force,
precisely so a decision can be reconstructed after the rules have moved. The
other half of the input carried no version at all.

### What was built

`sanctions_snapshots` — one row per distinct list version, holding the source,
the publisher's own publication date, a sha256 of the file, the entity count and
when it was first loaded. `screening_results.snapshot_id` points at it.

Four decisions inside that are worth recording.

**The hash is of the file, not the parsed entries.** Hashing in-memory entries
would have made the digest a statement about our parser's output at runtime,
which is harder for anyone to check than a file on disk.

But the claim I first wrote around this was overstated, and the correction is
worth recording rather than quietly editing. I said "anyone holding the same
file can recompute it and confirm", which implies independent verification. It
does not provide that. The file is gitignored and never archived, so in three
months the bytes are gone and OFAC's publication has moved on. And the digest
covers our *derived* JSON, not OFAC's XML — so even a reader holding the
original source could not reproduce it without running this exact parser.

What the digest actually provides is **tamper-evidence within this system**:
the file loaded is the file recorded, and nothing has been altered since. That
is genuinely useful and it is a smaller claim. Hashing the source XML as well
would restore external verifiability for OFAC specifically; archiving the
source artifact to WORM storage would make it durable. Neither is built, and
the README now says so.

**`published_at` is nullable, and the synthetic fixture has none.** It is
fabricated and has no publisher. Inventing a date would make a made-up list look
like a dated authority, which is precisely the opposite of what the table is
for.

**Existing rows were not backfilled.** Screenings written before this migration
ran against a list whose version we genuinely do not know. Filling in a
plausible value would fabricate exactly the provenance the table exists to
establish. `NULL` reads as "unknown", which is true, and the case view says so
in those words rather than leaving a blank space.

**The snapshot is registered by the worker at load time, not by the
downloader.** The brief suggested the downloader, which is the obvious place —
it is what fetches the file. But the downloader only ever sees OFAC. The
synthetic fixture is never downloaded at all, and a file copied in by hand is
downloaded by nothing. Registering when the worker *loads* a list means every
list actually screened against has a row, whatever its provenance, and it keeps
`DATABASE_URL` out of a standalone script. The downloader's job became
preserving OFAC's `Publish_Date` and `Record_Count` — which it had been parsing
and discarding — into the file it writes.

`ON CONFLICT DO NOTHING` against a unique `(source, content_hash)`: identical
content is the same snapshot however many times it is loaded. Restarting a
worker must not create a new row and make it look as though the list changed.

### What this does NOT fix, stated plainly

**Staleness.** The worker still loads the list once per process and never
refreshes it. A worker up for three weeks is screening against a three-week-old
list.

This migration makes that **visible** — a snapshot row with an old publication
date next to a recent decision is readable evidence — and visible is a genuine
improvement over invisible. It is not the same as fixed. Nothing reloads the
list, and nothing warns when the one in memory has aged.

The two halves are worth keeping apart in the mind, because it is easy to let
the fixed one make the unfixed one sound solved:

| | Before | After |
| --- | --- | --- |
| Can you prove which list screened a case? | No | **Yes** |
| Is the list current? | Not necessarily | Still not necessarily |

The README's limitations section keeps staleness, reworded to say exactly that.
A TTL and a background reload are the fix, and they are deliberately not in this
change.

### The chain, end to end

```
DECISION       ELLISSA HOLDING — rejected by staff:officer@example.com, score 60
RULES          ruleset 2026-09-1, thresholds {approve <20, reject >=80}
MATCH          OFAC SDN, "ELLISSA HOLDING", 100%, snapshot 1
LIST VERSION   ofac, published 2026-09-18, 19,393 entries,
               sha256 3344c6ea837c2d74ba19688be26a49b06d839031856aa6733b4413553996a950
               loaded 2026-09-19 13:24
```

The hash was verified against the file on disk independently of the database:
`sha256(data/ofac_sdn.json)` produces the same digest. That is what makes it an
attestation rather than a label — a reader can check it without trusting us.

On the case view the officer sees one line beside the matches:

> Screened against **OFAC** published **2026-09-18** · 19,393 entries · loaded
> 2026-09-19 13:24 · sha256 3344c6ea837c2d74ba19688b…

and, where the snapshot is unknown, a differently-coloured line saying so and
saying why no value was invented.

### Note on the gap between the two dates

`published_at` is the vendor's version. `downloaded_at` is when we first loaded
it. The gap between them *is* the staleness, and having both recorded means it
can be measured rather than guessed — which is what the eventual fix will need
in order to alarm on anything.

---

## Correction: integration tests and CI

### What was actually wrong

Every interesting claim this project makes had been **demonstrated once, by
hand, on one machine, and never checked again**.

Two workers and no double-processing: run once in Phase 2. Three identical
webhooks producing one job: run once in Phase 3. A late result not overwriting a
newer one, the sweeper rescuing a dropped delivery, two officers racing one case
— each one a terminal session that scrolled away.

The suite was 121 tests and all of them were green, which is the part that made
this dangerous rather than merely incomplete. The scoring rules were tested
thoroughly because they are pure functions and pure functions are easy to test.
Everything that could actually break in production — the queue, the lock, the
constraints, the trigger — was tested not at all, because testing it needs a
database and a database is inconvenient.

That is the usual shape of this mistake: the tests cluster where testing is
cheap, not where the risk is.

### The fixture problem, and why it forced a different design

The existing database tests (migration 017's) wrap each test in a transaction
and roll it back. Clean, fast, leaves nothing behind.

It cannot work here, for two independent reasons:

1. **Two workers racing for a job have to see each other's writes.** Seeing
   requires committing. Committing means the rollback trick is gone.
2. **`audit_events` refuses DELETE and TRUNCATE.** The append-only rule binds
   the test suite exactly as it binds everything else, so "clean up afterwards"
   is not available either.

The second one is worth sitting with. A guarantee that is real is inconvenient
somewhere, and this is where. A test suite that could empty the audit log would
be evidence the log was not really append-only.

So: a **throwaway database**, `<name>_test`, whose schema is dropped and rebuilt
once per session. Pollution stops mattering when the whole thing is disposable.

Rebuilding runs the real `db/migrate.py` in a subprocess rather than replaying
SQL files directly. That was a deliberate choice for a second benefit: every CI
run now also exercises the migration runner against an empty database — the
exact path a deploy takes, and the one thing nothing else covered.

### Two tests that exist to catch a specific edit

Most tests assert that something works. These two assert that a specific
plausible change would be noticed.

**`test_the_naive_claim_really_is_broken`.** `claim_job_naively` exists to
demonstrate the race that SKIP LOCKED prevents. If someone ever "fixes" it —
and it looks exactly like a bug — the comparison the README draws becomes a
claim about nothing, silently. So the suite asserts it still produces
duplicates.

**`test_the_desk_lock_has_not_quietly_become_skip_locked`.** This one is
textual, and worth explaining because it looks like cheating.

Swapping `for update` for `for update skip locked` in the desk action breaks
nothing the database can see. Only one decision would still be recorded — the
partial unique index guarantees that regardless. What breaks is the *human*
behaviour: the second officer's locking query returns no row, so they are told
"No such case" about a case they are looking at.

No Python test can catch that. The code is TypeScript, and the database is
unharmed. Reading the file and asserting the query has not changed is a blunt
instrument, but it is an honest one, and it beats pretending the behaviour is
covered.

### Something the tests discovered about the schema

Backdating an application to make it look stuck turned out to be impossible.
`applications_set_updated_at` is a BEFORE UPDATE trigger that overwrites
`updated_at` with `now()` on every write, so an UPDATE setting it to two hours
ago is simply undone.

That is correct behaviour, and it means something stronger than it appears:
**no application's `updated_at` can be forged through the ordinary write path**,
by our code or by anything else holding a normal connection. The test disables
the trigger for one statement and restores it, and says why in a comment rather
than working around it quietly.

### What is still not covered, precisely

CI runs the worker suite against a real `postgres:16`, and typechecks the web
service. It does not drive a browser. A bug in a React component, or in the
TypeScript sitting above the SQL, passes.

That is a smaller gap than it sounds for this particular codebase — nearly every
guarantee here is a database guarantee, and those are now asserted — but it is a
real one, and the README's limitations section says so in those words rather
than claiming the suite covers the system.

### The numbers

| | Before | After |
| --- | --- | --- |
| Tests | 121 | 171 |
| Needing a database | 25 | 48 |
| Run automatically | none | all, on every push |

With no database reachable the 48 skip and the 123 pure ones still run, which is
why a fresh clone can `pytest` before starting Docker. The `schema` fixture is
deliberately **not** autouse: as autouse it attached to all 171, so an
unreachable database skipped the pure tests too — a suite that looks green
having tested nothing. That was caught by running it against a dead port and
counting, which is a habit worth keeping.

### What CI caught on its very first run

The migration-017 tests had been connecting to `DATABASE_URL` directly — the
development database — and passing because that database happened to be
migrated. On the CI runner Postgres was reachable and empty, so all 25 errored
instead of skipping.

Two things wrong there, and only the second is obvious:

1. They cannot run on a fresh clone or a clean database, which is the case the
   skip logic was written for.
2. **They were running against the developer's working database.** Rolled back
   per test, so nothing was harmed — but a suite that opens a connection to
   whatever you happen to be using is one editing mistake away from being a
   problem, and nobody had noticed because it always worked.

Both fixed by having that fixture depend on the throwaway `_test` database like
everything else. Worth recording because it is the argument for CI in miniature:
the failure had existed since migration 017 shipped, was invisible on the
machine that wrote it, and was found within ninety seconds of a machine that had
never seen the project running the tests.
