# Deploying to Heroku

One app, two process types, one database. Run these in order.

Everything here has been verified against the same container images Heroku will
build — locally, against a local Postgres — except the four steps marked
**NOT YET RUN**, which need an active Heroku account. Nothing below is guessed
from documentation; where something is untested it says so.

---

## 0. Prerequisites

- Heroku account with active credits (student credits count)
- Heroku CLI: `winget install --id Heroku.HerokuCLI`
- Docker running locally (the CLI builds images on your machine and pushes them)

```bash
heroku login
heroku container:login
```

---

## 1. Create the app  — **NOT YET RUN**

```bash
heroku create kyc-compliance-desk --stack container
heroku git:remote -a kyc-compliance-desk
```

`--stack container` is what makes Heroku read `heroku.yml` instead of guessing a
buildpack. Setting it afterwards is `heroku stack:set container`, which takes
effect on the next deploy rather than immediately.

## 2. Add Postgres  — **NOT YET RUN**

```bash
heroku addons:create heroku-postgresql:essential-0 -a kyc-compliance-desk
```

`essential-0` is the cheapest paid tier (~$5/month, 1GB, 20 connections). The
free tier no longer exists. It sets `DATABASE_URL` automatically — never set
that by hand, because Heroku rotates the credentials behind it and a hardcoded
copy will silently go stale.

**Connection budget.** 20 connections, and the web pool is `max: 10`. One web
dyno plus one worker (which holds exactly one connection) uses 11. A second web
dyno would use 21 and start refusing connections. If you ever scale the web
process, lower `max` in [db.ts](../web/src/lib/db.ts) first.

## 3. Config vars  — **NOT YET RUN**

`DATABASE_URL` comes from the add-on. Set the rest:

```bash
heroku config:set -a kyc-compliance-desk \
  SESSION_SECRET="$(openssl rand -hex 32)" \
  STAFF_EMAIL="officer@example.com" \
  STAFF_PASSWORD="<pick something>" \
  PUBLIC_BASE_URL="https://kyc-compliance-desk-<suffix>.herokuapp.com" \
  DIDIT_MODE="simulator" \
  DIDIT_WEBHOOK_SECRET="$(openssl rand -hex 32)" \
  SANCTIONS_SOURCE="ofac" \
  SANCTIONS_FALLBACK="synthetic" \
  JOB_STALE_SECONDS="600" \
  SWEEP_BATCH_SIZE="25" \
  NODE_ENV="production"
```

`PUBLIC_BASE_URL` has to be the real dyno URL — `heroku apps:info` shows it
after step 1. It is what the vendor simulator points its callbacks at, so a
wrong value produces verifications that start and never come back.

The four that are not obvious:

| Variable | Why this value |
| --- | --- |
| `SANCTIONS_SOURCE=ofac` | The real US Treasury SDN list. Not in git (5.7MB, gitignored), so the worker downloads it at boot — see step 6. |
| `SANCTIONS_FALLBACK=synthetic` | If treasury.gov is unreachable the worker starts degraded instead of crash-looping. It logs `DEGRADED` at ERROR and every match recorded meanwhile names `synthetic` as its snapshot source, so a case decided during an outage says so on its own page. |
| `JOB_STALE_SECONDS=600` | Must exceed the longest legitimate job. Worst case is a sweep against an unresponsive vendor: `SWEEP_BATCH_SIZE (25) × VENDOR_TIMEOUT_SECONDS (10) = 250s`. Below that product the reaper reclaims jobs that are still running. The worker checks this relationship at boot and logs `MISCONFIGURED` if it stops holding. |
| `SWEEP_BATCH_SIZE=25` | The other half of the same arithmetic. |

## 4. Deploy  — **NOT YET RUN**

```bash
git push heroku master
```

Heroku reads [heroku.yml](../heroku.yml), builds both images, runs the release
phase (`python ../db/migrate.py up` from the worker image), and only then starts
the dynos. A failed migration aborts the release and leaves the previous version
running against the previous schema — which is the correct outcome, and the
reason migrations are a release phase rather than a boot step.

## 5. Scale the worker  — **NOT YET RUN**

```bash
heroku ps:scale web=1 worker=1 -a kyc-compliance-desk
```

**The worker does not start on its own.** Heroku starts `web` automatically and
leaves every other process type at zero. Skipping this gives you a site that
loads, accepts applications, and never decides any of them — the queue fills and
nothing drains it. This is the single most likely way this deploy goes wrong.

## 6. Confirm the worker is actually processing

Deployed is not the same as working. Check all three:

```bash
# 1. The dyno is up
heroku ps -a kyc-compliance-desk

# 2. It booted correctly — look for these four lines
heroku logs --tail --dyno worker -a kyc-compliance-desk
```

A healthy boot looks exactly like this (verified in the container locally):

```
worker <id> starting (poll 1.0s)
timeouts: worst-case job 250s, stale after 600s (2.4x margin), reaped every 60s
no local OFAC list; downloading it before claiming any work
sanctions list ready: 19393 entries from 'ofac' in 14.86s, RSS 86MB
```

If line 2 says `MISCONFIGURED`, fix the config vars before doing anything else.
If line 4 says `'synthetic'` rather than `'ofac'`, the download failed and you
are running degraded — the log above it will say why.

```bash
# 3. Jobs are actually being drained, not just queued
heroku pg:psql -a kyc-compliance-desk -c \
  "select status, count(*) from jobs group by status"
```

`done` should be climbing and `queued` should not be. A `queued` count that only
grows means the worker is at zero dynos (step 5) or crash-looping.

## 7. Seed the demo data  — **NOT YET RUN**

```bash
heroku run -a kyc-compliance-desk --size=standard-1x \
  "cd worker && python seed.py --count 500 --weeks 6"
```

`--size=standard-1x` because the default one-off dyno shares the Eco pool and
seeding 500 applicants with screening is heavier than a request handler.

---

## Memory: measured, not estimated

Taken from the worker container on Linux, the same image Heroku builds:

| Stage | RSS |
| --- | --- |
| Interpreter + imports | ~21 MB |
| After loading the synthetic fixture (25 entries) | ~21 MB |
| After loading OFAC from disk (19,393 entries) | **73 MB** |
| After downloading *and* parsing OFAC at boot | **86 MB** |

An Eco dyno is 512MB and R14 (swapping) begins there. **86MB is 17% of it, so
this is not tight and needs no tuning.** The 13MB gap between the last two rows
is the 27.7MB XML document held during parsing; it is transient, and the peak
during the parse is somewhat higher than the 86MB measured after it.

The number that would change this picture is a much larger list — the EU and UK
consolidated lists together, or OpenSanctions' full export — not this one.

## Boot time

14.86s for the worker, nearly all of it the OFAC download and parse. Worker
dynos have no boot timeout (R10 applies to web dynos that fail to bind `$PORT`
within 60s), so this is comfortable. The web dyno boots in about 10s.

---

## Two things to know before the first restart

**Every worker boot may register a new sanctions snapshot.** OFAC's preview
endpoint can return subtly different bytes for the same publication date — two
downloads six hours apart produced the same 19,393 records and the same
`published_at`, but different sha256 digests. `sanctions_snapshots` correctly
treats those as different versions, which means the table gains a row per dyno
restart rather than per publication. That is honest (the content really did
differ) but it is not what the table name suggests, and with two workers running
concurrently they can be screening against different snapshots.

**A dyno restart costs a re-download.** Heroku restarts dynos at least daily.
Each restart re-fetches 27.7MB from treasury.gov. That is fine at this scale and
would not be at a larger one; the fix is a cached copy with a TTL, which is the
same missing piece as the staleness limitation in the README.

---

## Rollback

```bash
heroku releases -a kyc-compliance-desk
heroku releases:rollback vNN -a kyc-compliance-desk
```

Rolling back reverts the **images**, not the **database**. A release whose
migration changed the schema cannot be undone this way: `db/migrate.py` has only
an `up` command and the migrations carry no down-steps. So if a migration is
wrong, the fix is a new migration, not a rollback — and a rollback after a
schema change puts the previous code in front of the new schema, which is the
one combination the release phase otherwise prevents.
