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

## Status

Phase 0 complete — foundations. Both services connect to Postgres.
