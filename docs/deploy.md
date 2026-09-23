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

Measured against the live deployment, not estimated.

### After the database has gone to sleep

Neon suspends a free-tier compute after about five minutes of inactivity. To
measure a real resume rather than guess at one, the cron was disabled, the
database left alone for seven minutes, and the first request timed:

| | cold | warm |
| --- | --- | --- |
| `/stats` — the first page that queries | **2437 ms** | 308 ms |
| `/` | 309 ms | 323 ms |
| `/desk` | 589 ms | 556 ms |

So: **one page load of about two and a half seconds, then everything is
normal.** The resume costs roughly 2.1 s of that, and it is paid once by
whoever arrives first — every page after it is served in around 300 ms.

Measured separately at the connection level, Neon's resume is **669 ms**; the
rest of the 2.4 s is the page's own queries and a transatlantic round trip from
the machine doing the measuring.

**In practice this almost never happens.** The drain cron runs every five
minutes and Neon's autosuspend is five minutes, so the database is kept awake
by the heartbeat. A visitor only meets a cold start if the cron has stopped —
which GitHub does after 60 days of repository inactivity. A demo nobody has
touched for two months therefore costs its next visitor two and a half seconds,
and nothing after that.

### The pipeline, end to end

From `scripts/verify-live.mjs`, which drives a real browser through the live
site:

```
1. Submit an application through the live form        ✓ created
2. Complete identity verification (simulator)         ✓ outcome submitted
3. Wait for the pipeline to reach a decision          ✓ DECIDED after 12.1s
4. Sign in to the review desk                         ✓
5. Open the case and read the evidence                ✓ score 60, OFAC shown,
                                                        5 matches at 100%
6. Stats page recomputes                              ✓ 638 processed,
                                                        13 awaiting review
```

**Twelve seconds from submitting to a decision**, most of which is the vendor
simulator and the polling interval of the check itself rather than the
pipeline.

### The drain function

| | |
| --- | --- |
| Cold start | 1.64 s |
| Warm | ~0.45 s |
| Work, once running | 5–80 ms for a batch of five |

---

## A trap worth knowing about

**The web project must not be connected to the Git repository.**

Both Vercel projects build from this one repo. The drain builds from the
repository root and reads the `vercel.json` there, which is its own config and
says there is nothing to build. That is correct — for the drain.

While the web project was also Git-connected, every push triggered an automatic
production build of the app *from the repository root*, where it read the same
`vercel.json`, built nothing, succeeded, and took the production alias. The
result was a green, `Ready` deployment serving `NOT_FOUND` for every route —
which happened twice before the pattern was visible.

It is disconnected for now and deploys by CLI:

```bash
cd web && npx vercel deploy --prod
```

### The permanent fix — do this in the dashboard

Setting the Root Directory tells Vercel where the app actually lives, so a
Git-triggered build runs `next build` inside `web/` and never sees the drain's
`vercel.json` at all.

1. **Vercel → the `kyc-compliance-desk` project → Settings → Build and
   Deployment.**
2. Under **Root Directory**, enter `web` and save. Leave "Include files outside
   the root directory" **off** — the app needs nothing above `web/`, and
   turning it on is what would let the root `vercel.json` back in.
3. Confirm the Framework Preset now reads **Next.js**. If it still says "Other",
   the root directory has not taken effect and step 2 did not save.
4. **Settings → Git → Connect Git Repository**, and reconnect
   `Denxhinjo/kyc-compliance-desk`. Production branch `master`.

**What this restores:** every push to `master` deploys the app automatically,
and `cd web && npx vercel deploy --prod` becomes a convenience rather than the
only way to ship.

### How to know it actually worked

Do not trust a green deployment — a green deployment serving nothing is exactly
the failure this fixes. Push a trivial change and check all three:

```bash
git commit --allow-empty -m "check vercel root directory" && git push
```

1. **The build log mentions Next.js.** Vercel → Deployments → the new one →
   Building. Look for `Traced Next.js server files` or `Creating an optimized
   production build`. A build that finishes in under ten seconds and says
   `nothing to build` is the broken shape.

2. **The site answers.** The single most useful check, because it tests the
   alias rather than the build:

   ```bash
   curl -s -o /dev/null -w "%{http_code}
" https://kyc-compliance-desk.vercel.app/stats
   ```

   `200` is correct. **`404` means the alias is pointing at an empty build
   again** — the same symptom as before, and the thing to watch for.

3. **The deployment you are looking at is the one serving.** `npx vercel ls
   kyc-compliance-desk` from `web/`; the newest Ready deployment should be the
   one holding `kyc-compliance-desk.vercel.app`.

If step 2 returns 404 after a push, the root directory did not stick.
Disconnect Git again and fall back to the CLI — the site works either way, and
a manual deploy is a smaller problem than a silently empty one.
