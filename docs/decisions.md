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
