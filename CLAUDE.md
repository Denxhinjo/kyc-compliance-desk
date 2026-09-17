~~~markdown

# Project: KYC Onboarding & Compliance Review Desk

## What this is

A portfolio demo by a freelance software developer moving into fintech. It shows

a customer identity-verification flow (KYC/AML) plus the back-office review desk

where a compliance officer handles cases that can't be decided automatically.

It is NOT a product and NOT client work. Synthetic data only.

## How to work with me

I am an experienced developer but NEW TO FINTECH. I'm building this to learn the

domain and to show prospective clients. Teaching me is part of the job, not a

bonus. A working feature I don't understand is a failure.

### Explanation rules - not optional

1. **Before writing code in a phase**: explain in plain language what we're
    
    building, why it's built that way, and list the files you'll create or
    
    change. Then wait for me to say go.
    
2. **After writing code**: walk me through each file - what it does, why it's
    
    shaped that way, and any syntax or pattern I may not have seen.
    
3. **Define every fintech term the first time it appears** (KYC, AML, PEP,
    
    sanctions screening, idempotency, webhook signature) in one or two plain
    
    sentences. Don't assume I know it.
    
4. **When there was a real choice**: say what you picked, what you rejected,
    
    and why. I want the reasoning, not just the result.
    
5. **Flag the parts that are the point of the demo** - the pieces a fintech
    
    engineer would look for - so I know where to spend care.
    
6. If I ask "why", I want the underlying concept, not just the syntax.

## Stack - do not change without asking me

- **Web**: Next.js (App Router) + TypeScript. Sign-up flow, review desk,
    
    webhook endpoint.
    
- **Worker**: Python. Sanctions screening and risk scoring only.
- **Database**: PostgreSQL. Also carries the job queue.
- **ID verification**: Didit. Sumsub was the original choice but requires a

    business account, so sandbox keys were unobtainable. Didit publishes its

    contract and offers self-serve sandbox access. A local simulator speaking

    the same contract is the default, so the repo runs with no vendor account.
- **Sanctions data**: OpenSanctions (free, open data).
- **Hosting**: Railway. Local dev via docker-compose.

Use **raw SQL** in both services (`pg` in TypeScript, `psycopg` in Python).

No ORM. Reason: the schema is shared by two languages, so it must not be owned

by either one's ORM - and raw SQL keeps what's happening visible to me.

## Repo layout

```
/web      Next.js + TypeScript
/worker   Python worker
/db       Numbered SQL migrations (shared, owned by neither service)
/docs     decisions.md, concepts.md
docker-compose.yml
```

## Architecture rules

1. **The two services never call each other.** No HTTP between them. They
    
    communicate only through a `jobs` table in Postgres. This is deliberate -
    
    it avoids an internal API, CORS and shared auth.
    
2. **The job queue is a plain Postgres table** claimed with
    
    `FOR UPDATE SKIP LOCKED`. No Redis, no Celery, no pg-boss.
    
3. **Webhooks do the minimum**: verify signature, store the raw message,
    
    enqueue a job, return 200 fast. Real work happens in the worker.
    
4. **Duplicate vendor messages must be harmless.** Unique constraint on the
    
    vendor's event id.
    
5. **`audit_events` is append-only.** Never UPDATE or DELETE it. Every state
    
    change writes a row.
    
6. **Don't store ID document images.** Keep the vendor's reference and fetch
    
    on demand. Less personal data held = less risk.
    
7. **Risk scoring is pure functions with unit tests.** No machine learning -
    
    a points system is explainable, which is what regulators require.
    

## Scope guardrails

- Two services, one database. That is the ceiling. Nothing else gets added.
- Build only what the current phase needs. No speculative abstraction.
- No auth framework beyond one simple staff login.
- Ask before adding any dependency, and say what it's for.
- Synthetic applicants only. A "DEMO - SYNTHETIC DATA" banner on every page.

## Process

- **One phase at a time.** Stop at the end of each phase and wait for me.
- **Run the code, don't just write it.** Show me it working before moving on.
- **At the end of each phase**, append to `docs/decisions.md`: what we built,
    
    the decisions made and why, and anything a reader would find non-obvious.
    
    Write it so it could become a public blog post later - I will use it for my
    
    case study.
    
- **Also keep `docs/concepts.md`**: a short plain-English glossary of every
    
    fintech term that came up.
    
- **Commit at the end of each phase** with a clear message.
- Secrets go in `.env`, never in git. Add `.env` to `.gitignore` in Phase 0.

## The phases

1. Foundations - repo, both services, local Postgres, everything talking
2. Data model + append-only audit log
3. The job queue (SKIP LOCKED) - the architectural centrepiece
4. Applicant flow + Didit + webhook with idempotency
5. Python worker consumes jobs, fetches vendor results
6. Sanctions screening + risk scoring + tests
7. The compliance review desk
8. Stats page, seed data, demo polish
9. Deploy to Railway + README

~~~

---

# Part 2 — The phase prompts

Paste one at a time. Each assumes `CLAUDE.md` is in place.

## Phase 0 — Foundations

~~~

Start Phase 0: Foundations.

Goal: an empty but complete skeleton where every piece can talk to every other

piece. No features yet.

Set up:

- The repo layout from CLAUDE.md
- docker-compose.yml running PostgreSQL locally
- /web: a Next.js + TypeScript app that connects to Postgres and has one page
    
    showing "database connected" or the error
    
- /worker: a Python service that connects to the same database and logs a
    
    heartbeat every 10 seconds
    
- .env.example with every variable, and .env in .gitignore
- A README stub

Before you write anything, explain the plan and why the layout is shaped this

way — especially why /db migrations belong to neither service.

Then build it, run both services, and show me the evidence they're both talking

to the database.

~~~

## Phase 1 — Data model + audit log

~~~

Start Phase 1: the data model and the audit log.

Create numbered SQL migrations in /db for the five tables in CLAUDE.md:

applications, vendor_events, screening_results, decisions, audit_events.

Plus a small migration runner I can run from the command line.

Then write the logEvent() helper in TypeScript and its Python equivalent.

Every state change in this system will go through these.

Explain as you go:

- Why each table exists and what each column is for
- What "append-only" means in practice and how we'll keep audit_events honest
- Why audit_events is the most important table here, and how it becomes a
    
    separate demo later
    

Show me the schema working with a few manually inserted rows.

~~~

## Phase 2 — The job queue

~~~

Start Phase 2: the job queue. This is the architectural centrepiece — take

your time explaining it.

Build:

- A `jobs` table (migration)
- TypeScript: enqueue a job
- Python: claim a job with FOR UPDATE SKIP LOCKED, run it, mark done or failed
- Retries with exponential backoff, and a max attempt count after which a job
    
    is parked rather than retried forever
    

Explain properly:

- What FOR UPDATE SKIP LOCKED actually does, and what would go wrong without it
- Why a database table is a legitimate queue here, and when it would stop being
    
    one (be honest about the limits)
    
- Why we chose this over Redis/Celery/pg-boss

Prove it: run two Python workers at once against the same queue and show me

that no job is ever processed twice.

~~~

## Phase 3 — Applicant flow + Didit + webhook

~~~

Start Phase 3: the applicant flow and the vendor integration.

Didit requires a business account, so there are no vendor keys. A local

simulator speaks Didit's published contract instead.

Build:

- The applicant form (name, date of birth, address) creating an `application`
    
    with status "started"
    
- A verification screen, so I can complete a test check
- A webhook endpoint that verifies Didit's signature, stores the raw message
    
    in vendor_events, enqueues a job, and returns 200 immediately
    
- A unique constraint on the vendor event id so duplicates are harmless
- A status page the applicant sees while waiting

Explain:

- Why the webhook must do almost nothing before returning 200
- What a webhook signature is and what attack it prevents
- What idempotency means here, and why vendors resend messages at all

Then test it: replay the exact same webhook three times and show me that

nothing breaks and only one job is created.

~~~

## Phase 4 — Worker consumes the results

~~~

Start Phase 4: the Python worker processing verification results.

Build:

- A job handler that fetches the full result from Didit's API and updates the
    
    application
    
- The status lifecycle: started → submitted → checking → screening → decided,
    
    with every transition writing an audit event
    
- A sweeper job every 15 minutes that finds applications stuck in "checking"
    
    and asks Didit directly what happened
    

Explain:

- Why the sweeper exists — what happens when a webhook simply never arrives
- Why we model status as a lifecycle rather than storing a result

Show me an application moving through the states on its own after I complete

a test verification.

~~~

## Phase 5 — Screening and risk scoring

~~~

Start Phase 5: sanctions screening and risk scoring, in Python.

Build:

- An OpenSanctions lookup, results into screening_results
- Fuzzy name matching with rapidfuzz, so "Ahmed Hassan" matches "Ahmad Hasan"
    
    — store a match strength, not just yes/no
    
- The points-based risk score from the plan, as pure functions
- Routing: under 20 auto-approve, 20–79 send to review, 80+ auto-reject —
    
    each writing a decision and an audit event
    
- Unit tests for the scoring functions, including edge cases

Explain:

- What sanctions lists and PEPs are, and why a near-match is genuinely hard
- Why a points system beats machine learning here (regulators need to see why)
- Why scoring is pure functions — and why this is the code a client would most
    
    want to read
    

This is the part I'll be judged on. Make the tests good.

~~~

## Phase 6 — The review desk

~~~

Start Phase 6: the compliance review desk. This is the most important screen

in the whole demo.

Build:

- A simple staff login (one demo account)
- A queue: pending cases, oldest first, showing risk score and time waiting
- A case view: applicant details, document check results, screening matches,
    
    and a clear list of why it was flagged
    
- Approve / Reject, with a written reason REQUIRED, writing a decision and an
    
    audit event
    
- A read-only timeline on each case showing every audit event

Explain:

- Why the reason field is mandatory, and what an auditor does with it
- What a compliance officer is actually looking at, and why the evidence has
    
    to be side by side
    

Design it as an operations tool people use all day, not a marketing page.

~~~

## Phase 7 — Stats, seed data, polish

~~~

Start Phase 7: make it look alive.

Build:

- A seed script generating 500 synthetic applicants spread across all three
    
    outcomes, with realistic timestamps over the past few weeks
    
- A stats page: total processed, % auto-decided, median time to decision,
    
    current queue size, decisions over time
    
- A "DEMO — SYNTHETIC DATA" banner on every page
- Empty and loading states everywhere

Explain how you generated data that looks plausible rather than uniform.

Then give me the three real numbers from the seeded data, with the method

stated, so I can publish them honestly.

~~~

## Phase 8 — Deploy and document

~~~

Start Phase 8: deploy and document.

- Deploy web app, worker and database to Railway
- Production env vars and the Didit webhook URL pointed at the live app
- A README explaining what this is, the architecture, how to run it locally,
    
    and — most importantly — the async/idempotency problem and how it's solved
    
- A clear DEMO disclaimer: synthetic data, not client work
- Tidy docs/decisions.md into something I could publish

Then walk me through a full demo script: what to click, in what order, to show

this to a fintech CTO in 90 seconds.

~~~