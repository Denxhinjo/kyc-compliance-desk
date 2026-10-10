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

`.github/workflows/drain.yml`, hourly. Not GitHub's floor — the floor is five
minutes, and the schedule was deliberately lowered on 10 October 2026 because
the web app's doorbell already covers the path an applicant is watching, so the
cron only has to drive the two recurring jobs. See `decisions.md` under
*"Hourly instead of every five minutes"* for the compute reasoning and the
three costs. Repository secrets:

- `DRAIN_URL` — `https://<drain-project>.vercel.app/api/drain`
- `DRAIN_SECRET` — matching the function

The workflow calls the endpoint repeatedly while it reports `"more": true`,
capped at twelve calls so a backlog cannot turn one cron run into an unbounded
loop.

**Two things about scheduled workflows that will bite eventually.** GitHub
disables them after 60 days of repository inactivity, so a demo nobody touches
for two months stops draining until someone presses the button. And scheduled
runs are best-effort: the hour is the request, not a promise, and delays of
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

**In practice most first visits now meet this.** The drain cron runs hourly and
Neon's autosuspend is about five minutes, so the database is suspended for
roughly fifty-five minutes in sixty, and an idle demo is the normal state of a
demo.

This was the other way round until 10 October 2026: the cron ran every five
minutes, which defeated autosuspend and kept the compute awake around the
clock, and this paragraph said a cold start almost never happened. The schedule
was lowered deliberately, trading this two-and-a-half-second first load for
twelve times less compute. The reasoning and the other two costs are in
`decisions.md` under *"Hourly instead of every five minutes"*.

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

---

## Rolling migrations 020–025 out to Neon

Neon is at **019**. Six migrations are pending, and **file order is not
deployment order** — applying them 020 first breaks the live site.

Three of them change what the database will *accept*, and the deployed code has
to be ready before each of those lands. One changes what the system *does*
rather than what it accepts. The order below is the only one where no step
breaks the step before it.

Nothing here has been run. Run it yourself, one step at a time, checking after
each.

### The order

| # | Step | What it does to the live database |
| --- | --- | --- |
| 0 | **Pre-check** (read-only) | Nothing. Tells you what the later steps will do. |
| 1 | `021` | Adds a nullable column to `decisions` and backfills it. |
| 2 | `024` | Creates `rulesets`, seeds two versions, adds two foreign keys. |
| 3 | `025` | Inserts one more `rulesets` row. |
| 4 | **Deploy worker + web code** | New code starts writing the column and stamping `2026-10-1`. |
| 5 | `022` | Starts rejecting decisions with no ruleset version. |
| 6 | `023` | Starts rejecting officer decisions while screening evidence is parked. |
| 7 | `020` | Starts rejecting re-scores of decided applications — and so starts parking jobs. |

---

## STOP — "deploy the code" has already happened, by itself

Checked 2026-10-08. The order above assumes the code deploy is a step you take
between migrations. On this project it is not: **half of it has already
happened, automatically, and the other half has not.**

| Project | Git-connected? | Code currently live |
| --- | --- | --- |
| `kyc-drain` — runs the **worker** against production | **Yes. Auto-deploys to Production on every push to master.** | `4e57a32`, deployed 2026-10-08 12:11 |
| `kyc-compliance-desk` — the **web app** | No, disconnected after the root-directory incident | Predates `9aba3d2` (2026-09-24) |

Evidence: `gh api repos/.../deployments` lists a Production deployment per push
since 2026-10-03, every one with a `target_url` under `kyc-drain`. The live
stats page does not contain the `chart-scroll` marker introduced on 2026-09-24,
so the web app has not been rebuilt since before then.

### What that means right now

The deployed worker writes `risk_ruleset_version_at_decision` when it records a
decision. **Production's `decisions` table does not have that column** — 021 is
unapplied. So every automatic decision on production fails with "column does not
exist", and has done since `f0f3965` deployed on 2026-10-03.

It has not been noticed because the drain's HTTP endpoint returns success
whether or not the jobs inside it succeeded. The GitHub Actions cron has
reported green throughout. The failure is real, latent, and armed: it fires the
next time an application reaches screening.

**This makes 021 a repair, not merely the first step.** Applying it fixes a
live system that is currently broken.

### What changes in the order

| Step | Changed? | Why |
| --- | --- | --- |
| 0 pre-check | **Extra gate added** — see below | It now has to answer a question it did not before |
| 1 `021` | **Urgent** | Repairs the live failure, not just preparation |
| 2 `024` | **Gated on the pre-check** | May be unsafe; see below |
| 3 `025` | Unchanged | |
| 4 deploy code | **Split in two** | The worker half is done. The **web half still must be deployed by hand**, and it is not optional — step 5 depends on it |
| 5 `022` | Unchanged, and the reason now matters more | The deployed **web** code does not write the column. Applying 022 before deploying the web app breaks every officer decision |
| 6 `023` | Unchanged | Still needs the new case view, i.e. the web deploy |
| 7 `020` | Unchanged | Still last |

The web deploy is the step most likely to be skipped now, precisely because
"deploy the code" sounds done. It is not:

```bash
cd web && npx vercel deploy --prod
```

### The extra gate: what the pre-check must show before 024 is safe

Migration 024 adds a foreign key requiring **every** version on an application
to exist in `rulesets`, and it seeds only `2026-09-1` and `2026-09-2`. The
deployed worker stamps **`2026-10-1`**, which 024 does not seed and only 025
adds.

So before running 024, `versions_in_use` from the pre-check must contain
**nothing outside `{2026-09-1, 2026-09-2}`**.

```sql
select array_agg(distinct risk_ruleset_version)
  from applications where risk_ruleset_version is not null;
```

**If it contains `2026-10-1`, do not run 024.** It will fail — cleanly and
atomically, because the migration is one transaction, so nothing is left
half-applied — but it will not go in, and the remedy is a migration that records
`2026-10-1` before the keys are added, which does not exist yet. Report it
rather than improvising.

Is `2026-10-1` likely to be there? Probably not, and the reasoning is worth
following. Scoring and deciding happen in **one transaction**: the worker writes
the version onto the application and then inserts the decision. The decision
insert is the statement that fails, so the whole transaction rolls back and the
version is never committed. The exception is a re-screen of an
already-referred case, where the worker returns early without inserting a second
decision — there, the version write commits on its own. That path is reachable,
which is why this is a gate and not an assumption.

### Before you start: stop the code changing under you

`kyc-drain` redeploys on every push to master. A push during the rollout swaps
the worker mid-sequence. Either finish the rollout without pushing, or pause the
integration first:

**Vercel dashboard → the `kyc-drain` project → Settings → Git → Disconnect**, or
**Settings → Git → Ignored Build Step** set to always skip. Reconnect afterwards.

The GitHub Actions cron is separate and can be left alone — it only calls the
endpoint. If you want it quiet during the window:
**GitHub → Actions → the `drain` workflow → ⋯ → Disable workflow.**

### Where to look, if you would rather confirm this yourself

- **Vercel dashboard → `kyc-drain` → Settings → Git.** If a repository is shown
  as connected, pushes deploy. → **Deployments** tab shows the commit for each.
- **Vercel dashboard → `kyc-compliance-desk` → Settings → Git.** Expect no
  connected repository. → **Deployments** shows its last build and its date.
- **GitHub → the repository → Environments / Deployments** (right-hand sidebar
  on the Code tab) lists what Vercel has deployed and from which commit.
- **GitHub → Actions → `drain`** for the cron's history. Note that a green run
  there means the HTTP call succeeded, **not** that the jobs it triggered did.

The last point is worth keeping: a cron that reports success while the work it
triggers fails is the same defect class recorded in `decisions.md` — something
that looks like a check and is not one.

---

## Applying one migration at a time — read this before step 1

`python db/migrate.py up` applies **every pending migration in numeric order**.
The order above is not numeric order, so plain `up` would apply 020 first and
break the live site at the first step.

Use `--only`:

```bash
python db/migrate.py up --only 021
```

It applies exactly that migration, records it with its checksum like any other,
and refuses if the version is unknown or already applied. It is deliberately not
"apply up to version N", because that still imposes numeric order — the thing
that does not hold here.

This was found by rehearsing against a branch rather than by reading the
runbook. Up to that point the checklist was unexecutable with the tooling the
project had, and the alternatives would have been running the SQL by hand
(bypassing the checksum record, so the database would no longer agree with the
repository) or shuffling migration files between steps.

---

---

### Step 0 — the pre-check

Read-only. It tells you what steps 1–3 will actually do on Neon, where the
numbers may differ from the development database.

```sql
select
  -- What 021's backfill will fill, and what it will leave NULL.
  (select count(*) from decisions)                                            as decisions_total,
  (select count(*) from decisions d join applications a on a.id = d.application_id
    where a.risk_scored_at is not null and a.risk_scored_at <= d.decided_at)  as will_backfill,
  (select count(*) from decisions d join applications a on a.id = d.application_id
    where a.risk_scored_at > d.decided_at)                                    as null_scored_after,
  (select count(*) from decisions d join applications a on a.id = d.application_id
    where a.risk_scored_at is null)                                           as null_never_scored,
  -- What 024's foreign keys must be able to resolve.
  (select array_agg(distinct risk_ruleset_version)
     from applications where risk_ruleset_version is not null)                as versions_in_use,
  -- What 020 will start parking.
  (select count(*) from applications a join decisions d on d.application_id = a.id
    where a.status = 'screening' and d.outcome = 'referred')                  as referred_pending,
  -- What 023 will start blocking.
  (select count(*) from jobs
    where job_type = 'screening.run' and status = 'parked')                   as parked_screening_jobs;
```

**Read `versions_in_use` before step 2.** Migration 024 seeds `2026-09-1` and
`2026-09-2`, then adds a foreign key requiring every version on an application
to exist in `rulesets`. If that array holds anything else, **stop** — 024 will
fail, and the fix is a new migration recording the missing version, not an edit
to 024.

`referred_pending` is the number that becomes parkable at step 7.

---

### Step 1 — `021`, the column and the backfill

**What it does.** Adds `decisions.risk_ruleset_version_at_decision`, nullable
with no default, then copies each application's version onto its decisions — but
only where `risk_scored_at <= decided_at`, meaning the score was written before
the decision was taken. A score written *after* its decision cannot be trusted
to be the one the decision used, so those rows stay NULL.

**Why first.** A nullable column with no default is a catalogue change rather
than a table rewrite: it does not touch existing rows and takes no meaningful
lock. Nothing deployed knows the column exists, so nothing behaves differently.
It is the only one of the six invisible to running code, which is what makes it
safe to go first.

**Why it cannot be later.** Step 2 adds a foreign key *on this column*. Step 4's
code writes to it. Step 5 requires it.

**The slow part.** The backfill is one UPDATE per qualifying decision row — the
only step here with meaningful write volume.

**Verify after.** The migration prints its own counts and `db/migrate.py`
surfaces them. They should match `will_backfill`, `null_scored_after` and
`null_never_scored` from the pre-check.

---

### Step 2 — `024`, the rulesets table

**What it does.** Creates `rulesets`, makes it append-only with triggers, seeds
`2026-09-1` and `2026-09-2`, and adds foreign keys from
`applications.risk_ruleset_version` and
`decisions.risk_ruleset_version_at_decision` to `rulesets.version`.

**Why after 021.** One of those foreign keys is on the column 021 adds. Applied
first it refuses: the column does not exist.

**Why before the code deploy.** The currently deployed code stamps `2026-09-2`,
which this seeds. The *new* code stamps `2026-10-1`, which this does not. Deploy
the code first and either the new rows cannot be written, or 024 later fails on
data it cannot resolve.

**What breaks if skipped.** Step 3 inserts into a table that does not exist.

**Verify after.**

```sql
select version, effective_from::date from rulesets order by effective_from;
-- expect 2026-09-1 and 2026-09-2

select count(*) from decisions d
  join rulesets r on r.version = d.risk_ruleset_version_at_decision;
-- expect this to equal will_backfill from the pre-check
```

---

### Step 3 — `025`, the current version

**What it does.** Inserts one row: `2026-10-1`, the version whose
call-for-action list is read from the committed FATF statement.

**Why here, and not after the deploy.** This is the row the new code's version
string points at. The foreign key from step 2 is already live, so the moment the
new code scores an applicant it writes `2026-10-1` into
`applications.risk_ruleset_version`. If this row is absent that write is
rejected and the application cannot be scored at all.

**What breaks out of order.** Deploy the code before this and scoring fails on
the live system with a foreign key violation. Loudly rather than silently, which
is the right direction — but it fails.

**Verify after.**

```sql
select version, reference_data->'fatf_call_for_action'->>'sourcing'
  from rulesets order by effective_from;
-- 2026-10-1 present, reporting 'sourced'
```

---

### Step 4 — deploy the worker and web code

**What it does.** From here both decision paths write
`risk_ruleset_version_at_decision`, new applications are stamped `2026-10-1`,
the case view shows the stuck-evidence and retry notices, and `/desk/rulesets`
exists.

**Why after steps 1–3.** It writes a column that must exist (1), naming a
version that must resolve (2 and 3).

**Why before steps 5–7.** Each of those rejects something the *old* code does.
Applying them first turns working behaviour into errors.

**Verify after.** Put one applicant through end to end on the live site, then:

```sql
select risk_ruleset_version from applications order by created_at desc limit 1;
-- expect 2026-10-1
```

Open `/desk/rulesets` and confirm three versions are listed.

---

### Step 5 — `022`, requiring the version

**What it does.** Adds a `CHECK` constraint, `NOT VALID`, so every *new* decision
must carry a ruleset version while existing rows are left unexamined.

**Why after the code deploy.** This is the step that breaks most visibly out of
order. The currently deployed web code inserts decisions without the column.
Apply this before step 4 and **every officer decision on the live desk fails**
with a constraint violation until the code is deployed.

**Why `NOT VALID` matters here.** 021 deliberately leaves the column NULL where
the data could not establish a version. A validating constraint would refuse to
be added while those rows exist, and the only way to satisfy it would be to fill
them with a guess.

**Verify after.** Decide one case on the live desk — it should succeed. Then:

```sql
select count(*) from decisions where risk_ruleset_version_at_decision is null;
-- should not increase from here on
```

---

### Step 6 — `023`, the stuck-evidence guard

**What it does.** Refuses an officer's decision while a `screening.run` job for
that application is parked. Automatic decisions are unaffected.

**Why after the code deploy.** The case view is what explains the refusal.
Without the new code an officer meets a bare database error with nothing to
read.

**Why before 020.** 020 is what *causes* jobs to park. The other way round and
jobs start parking while nothing in the interface accounts for them — an officer
could decide a case whose new evidence is sitting unrecorded in a failed job,
which is the exact situation this prevents.

**What it may block immediately.** Whatever `parked_screening_jobs` reported in
the pre-check. If that is non-zero, those applications become undecidable until
someone looks at the jobs — which is the intent, but know it before rather than
after.

**Verify after.** With no parked jobs, deciding a case still works.

---

### Step 7 — `020`, the freeze

**Last.** It refuses any change to an application's scoring columns once a
decision row exists for it.

**What it does to live behaviour** — and this is the only step that changes what
the running system *does* rather than what it accepts: a redelivered screening
job for a referred case whose score would change now raises instead of writing.
It retries, exhausts its attempts and parks. Nothing is half-written, because
the whole job is one transaction, but the job stops and the evidence it carried
is not recorded.

**Why last.** Because of that sentence. Step 6 and the step 4 code are what make
a parked job visible to an officer and stop a decision being taken without it.
Applying 020 first means jobs can park into silence.

**What to expect.** `referred_pending` from the pre-check is the set of
applications that can now park a job. Most will not — a job only fails if the
score would actually change.

**Verify after.**

```sql
select count(*) from jobs where job_type = 'screening.run' and status = 'parked';
-- watch over the next day; a slow rise is the freeze working as intended
```

---

### If something is already wrong

**Decisions failing with `decisions_ruleset_version_present`** — 022 was applied
before the code deploy. Deploy the code; nothing needs undoing.

**Scoring failing with a foreign key violation on `risk_ruleset_version`** — the
code was deployed before 025. Apply 025; nothing needs undoing.

**024 refusing to apply** — an application carries a version `rulesets` does not
contain. Do not edit 024. Record the missing version in a new migration, built
from the repository the way 024's own rows were.

### One caveat about `effective_from`

Each `rulesets` row records when its version took effect *in the repository*,
which is its commit date. Applied to Neon later, those dates precede the moment
the version actually governed anything live. Left as-is rather than rewritten
per environment: the date answers "when did these rules come into existence",
and a version existing before it was deployed is an ordinary state of affairs.

---

## What the rehearsal did, and what it proved

Run on 2026-10-08 against a Neon branch copied from the live database — 736
applications, 770 decisions, migrations at 019, PostgreSQL 18.6. Every step
below was executed in order with its verification. Nothing failed after the
`--only` gap was closed.

| Step | Result |
| --- | --- |
| 0 — pre-check | 770 decisions, 770 backfillable, 0 excluded. `versions_in_use` = `{2026-09-1}` only. 13 referred-pending, 0 parked jobs. |
| 1 — `021` | Applied. Backfill reported **770 of 770**, 0 left NULL — matching the pre-check exactly. |
| 2 — `024` | Applied. Two rows seeded, both foreign keys present, 770 decisions joinable to a definition. |
| 3 — `025` | Applied. `2026-10-1` present and reporting `sourced`. |
| 4 — deploy code | Worker and web run against the branch. One applicant end to end in 105s: scored 60, referred, OFAC provenance shown. Stamped **`2026-10-1`**, and so was its referral row. `/desk/rulesets` listed three versions. |
| 5 — `022` | Applied, `convalidated = false` as intended. An officer decision through the real desk UI **succeeded** and carried `2026-10-1`. |
| 6 — `023` | Applied. 0 parked jobs, so nothing became undecidable. A second officer decision succeeded. |
| 7 — `020` | Applied. A `risk_score` update on a decided application was **refused**; a non-risk update on the same row still worked. 0 jobs parked. |

**What this proves for the real rollout.** The order is right and each step's
verification is checkable. Specifically:

- Step 3 before step 4 is not theoretical. The new code stamps `2026-10-1`, the
  foreign key from step 2 is live by then, and the rehearsal confirmed the write
  succeeds — which it could not have done if `025` had been skipped.
- Step 5 after step 4 is the one that would have taken the desk down. The
  rehearsal decided a real case through the UI with the constraint active; the
  old code would have failed that insert.
- Step 7's freeze works on a database with real data in it, and does not
  interfere with ordinary writes to a decided row.

**What the rehearsal could not prove.** It ran against a branch with no live
traffic, so it says nothing about contention, nor about what happens if a user
is mid-decision while a migration applies. Each step is short and takes no
heavy lock, but "no lock contention observed with nobody using it" is a weak
claim and should be read as one. The backfill in step 1 is the only step with
meaningful write volume.

### One thing to expect that is not caused by the migrations

The worker logged repeated `vendor.sweep_stuck` failures — *"sweep: 25
application(s) waiting on the vendor"*, timing out and retrying — before
eventually succeeding. Those 25 applications are pre-existing live data stuck
part-way through verification, and the sweeper asking the vendor about all of
them at once is slow. It predates this rollout and is unrelated to it. Expect
the same noise on the real run and do not read it as a migration problem.
