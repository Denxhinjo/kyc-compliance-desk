# Deploying

Free tiers throughout: Vercel for the web app, Neon for Postgres, GitHub
Actions for the periodic drain. Nothing here is billed.

This replaces an earlier Heroku runbook. Heroku was the plan until its worker
dynos turned out to need a paid plan to stay up, which is the whole reason the
worker became an on-demand function — see [decisions.md](decisions.md).

**Two Vercel projects, one repository.** The Next.js app lives in `web/` and
the drain function needs `worker/`, which is outside it; Vercel's root
directory can only be one of the two. So they deploy separately, which is also
the honest description: they are two services sharing one database, and now
they deploy like it.

---

## 1. Neon — DONE

```bash
npx neonctl projects create --name kyc-compliance-desk --region-id aws-us-east-1
```

`us-east-1` deliberately: it is Vercel's default function region, and a
screening run makes several round trips per applicant. Putting the database
next to the function matters far more than putting it near the browser.

Verified before anything else was provisioned, because the whole of Phase 1
depends on it:

| | |
| --- | --- |
| Server | PostgreSQL **18.6** (local is 16) |
| `pg_trgm` | **1.6** — same as local |
| `gin_trgm_ops` | present |
| `similarity('ahmed','ahmad')` | 0.333, identical to local |

## 2. Schema and data — DONE

```bash
export DATABASE_URL="postgresql://…@ep-….us-east-1.aws.neon.tech/neondb?sslmode=require"

python db/migrate.py up                      # 19 migrations, clean on PG18
cd worker
SANCTIONS_SOURCE=ofac python load_sanctions.py
SANCTIONS_SOURCE=ofac python seed.py --count 400 --weeks 5 --seed 20260923
```

The list load took **32.5 s** over the network for 19,393 entries, 44,017
spellings and 155,788 tokens. It is worth knowing why that is tolerable: the
first version of the loader inserted row by row and took 229 s *locally*, so
the same load across the Atlantic would have been twenty minutes or more. It
uses `COPY` now.

Seeding is still row-by-row and correspondingly slow over a network — several
minutes per few hundred applicants. It is a one-off, so it has been left
alone; if it ever stops being a one-off it wants the same treatment.

Storage after loading and seeding: **38 MB** of Neon's 512 MB free limit.

## 3. Vercel — the web app

Root directory `web`. Environment variables:

| Variable | Notes |
| --- | --- |
| `DATABASE_URL` | the Neon **pooled** connection string |
| `SESSION_SECRET` | `openssl rand -hex 32` |
| `STAFF_EMAIL`, `STAFF_PASSWORD` | the one demo login |
| `DIDIT_MODE` | `simulator` |
| `DIDIT_WEBHOOK_SECRET` | `openssl rand -hex 32` |
| `PUBLIC_BASE_URL` | the live URL, once it exists |
| `DRAIN_URL` | the drain project's `/api/drain` |
| `DRAIN_SECRET` | must match the drain project's value exactly |

Use the **pooled** host (`…-pooler.…`) for the web app. It serves many
concurrent requests from a `pg.Pool`, and Neon's pooler is what keeps that from
exhausting the connection limit on a free project.

## 4. Vercel — the drain function

Root directory `/` (the repository root). It picks up `vercel.json`,
`requirements.txt` and `api/drain.py`, and `.vercelignore` keeps `web/`,
`docs/` and the test suite out of the bundle so the cold start stays small.

| Variable | Notes |
| --- | --- |
| `DATABASE_URL` | the Neon **direct** connection string, not the pooler |
| `SANCTIONS_SOURCE` | `ofac` |
| `DRAIN_SECRET` | the same value the web app and the cron use |

Direct rather than pooled here, on purpose: one invocation holds exactly one
connection for its lifetime and then exits. A pooler in front of that adds a
hop and buys nothing.

## 5. The cron

`.github/workflows/drain.yml`, every five minutes — GitHub's floor, and about
right. Repository secrets:

- `DRAIN_URL` — `https://<drain-project>.vercel.app/api/drain`
- `DRAIN_SECRET` — matching the function

The workflow calls the endpoint repeatedly while it reports `"more": true`,
capped at twelve calls so a backlog cannot turn one cron run into an unbounded
loop.

**Two things about scheduled workflows that will bite eventually.** GitHub
disables them after 60 days of repository inactivity, so a demo nobody touches
for two months stops draining until someone presses the button. And scheduled
runs are best-effort: five minutes is the request, not a promise, and delays of
several minutes happen under load. Neither matters here, because nothing
depends on punctuality — only on eventually.

## 6. The secret, in three places

`DRAIN_SECRET` must be identical in the drain function, the web app, and the
repository secrets. A mismatch is a 401 and a silent queue: work accumulates,
nothing errors visibly, and the only symptom is applicants waiting. If the
demo ever looks frozen, check this first.

It travels as a **header**, never a query parameter. A secret in a URL is
written to access logs, referrer headers and the platform's own request
logging — several places neither you nor I control.

---

## What a visitor actually experiences

To be measured against the live deployment, not estimated:

- cold start after the database has been idle for days
- warm request latency
- time from submitting an application to seeing a decision

Filled in once the deployment exists.
