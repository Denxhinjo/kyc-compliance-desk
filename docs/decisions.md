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
