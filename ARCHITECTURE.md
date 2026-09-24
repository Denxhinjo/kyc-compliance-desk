# KYC Onboarding & Compliance Review Desk — architecture

## What this is

A portfolio demo: a customer identity-verification flow (KYC/AML) plus the
back-office review desk where a compliance officer handles the cases that
cannot be decided automatically.

It is not a product and not client work. Synthetic applicants throughout — no
real person's data has been in this system.

## Stack

- **Web**: Next.js (App Router) + TypeScript. Applicant flow, review desk,
  webhook endpoint.
- **Worker**: Python. Sanctions screening and risk scoring only.
- **Database**: PostgreSQL. Also carries the job queue.
- **ID verification**: Didit. Sumsub was the first choice but requires a
  business account, so sandbox keys were unobtainable. Didit publishes its
  contract and offers self-serve sandbox access. A local simulator speaking the
  same contract is the default, so the repo runs with no vendor account at all.
- **Sanctions data**: OpenSanctions and the OFAC SDN list — free, open data.
- **Hosting**: Vercel for the web app, Neon for Postgres, the worker as an
  on-demand function, GitHub Actions cron for the periodic drain. All free
  tiers. Railway was the original plan and its trial expired; Heroku replaced it
  and then turned out to need a paid plan to keep a worker dyno up, which is why
  the worker became a function. Local development is docker-compose.

**Raw SQL in both services** (`pg` in TypeScript, `psycopg` in Python). No ORM:
the schema is shared by two languages, so it must not be owned by either one's
idea of a model — and raw SQL keeps what is happening visible.

## Repo layout

```
/web      Next.js + TypeScript
/worker   Python worker — screening, scoring, job handlers
/api      the drain function (the worker's HTTP entry point in production)
/db       numbered SQL migrations, shared, owned by neither service
/docs     decisions.md, concepts.md, design-rationale.tex, deploy.md
/scripts  demo and verification scripts
/data     sanctions fixtures
```

## Architecture rules

1. **The two services never call each other.** No HTTP between them. They
   communicate only through a `jobs` table in Postgres. This avoids an internal
   API, CORS and shared auth. The deployed version bends this once, deliberately
   — see the design rationale for the test it has to pass.
2. **The job queue is a plain Postgres table**, claimed with
   `FOR UPDATE SKIP LOCKED`. No Redis, no Celery, no pg-boss.
3. **Webhooks do the minimum**: verify the signature, store the raw message,
   enqueue a job, return 200 fast. Real work happens in the worker.
4. **Duplicate vendor messages must be harmless.** Unique constraint on the
   vendor's event id.
5. **`audit_events` is append-only** — enforced by database triggers, not by
   convention. Every state change writes a row.
6. **Don't store ID document images.** Keep the vendor's reference and fetch on
   demand. Less personal data held is less risk carried.
7. **Risk scoring is pure functions with unit tests.** No machine learning: a
   points system is explainable, which is what a regulator needs.

## Scope guardrails

- Two services, one database. That is the ceiling.
- Build only what the current stage needs. No speculative abstraction.
- No auth framework beyond one staff login.
- Every dependency has to justify itself before it goes in.
- Synthetic applicants only, with a "DEMO — SYNTHETIC DATA" banner on every page.

## Conventions

- Run the code, don't just write it.
- `docs/decisions.md` records what was built at each stage, the decisions made
  and why, and anything a reader would find non-obvious.
- `docs/concepts.md` is a plain-English glossary of the fintech terms involved.
- Secrets live in `.env`, never in git.
- Commit at the end of each stage, with a message that explains the reasoning
  rather than restating the diff.

## How it was built

1. Foundations — repo, both services, local Postgres, everything talking
2. Data model + append-only audit log
3. The job queue (`SKIP LOCKED`) — the architectural centrepiece
4. Applicant flow + Didit + webhook with idempotency
5. Python worker consumes jobs, fetches vendor results
6. Sanctions screening + risk scoring + tests
7. The compliance review desk
8. Stats page, seed data, demo polish
9. Deploy — originally Heroku, now Vercel + Neon — and the README

The reasoning behind each of these, including the options rejected, is in
[docs/design-rationale.tex](docs/design-rationale.tex) and its compiled PDF.
