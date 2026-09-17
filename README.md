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

Phase 3 complete. Both services connect to Postgres; the schema and the
append-only audit log are in place; the job queue is proved safe under two
concurrent workers; and an applicant can fill in the form, complete identity
verification, and watch their status change as the webhook is processed
asynchronously.

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
