# The 90-second demo

For showing this to a fintech engineer or CTO. It doubles as a script for a
recorded walkthrough, which is the version worth having — a video does not
cost money every month and still works when someone opens your email three
weeks late.

**Before you start:** have the queue populated (`seed.py`), the worker running,
and three tabs open — `/stats`, `/desk`, and a terminal.

Assume you will be interrupted. The interruptions are the good part; the
answers below are the point of the whole project.

---

## The 90 seconds

### 0:00 — Lead with the problem, not the product (10s)

> "This is KYC onboarding — identity checks and sanctions screening. The
> interesting part isn't the forms, it's that you depend on a vendor you don't
> control, over a network that loses messages, to produce a result you're
> legally required to act on. Everything hard follows from that."

Do not open the applicant form. Nobody wants to watch you type an address.

### 0:10 — Open `/desk`. Start where the value is (20s)

> "This is the compliance officer's queue. Oldest first, no sort control — the
> longest-waiting case carries the most regulatory exposure, so it's not
> something you get to hide. Score, top flag reason, and how long it's been
> waiting, on one line."

Point at the oldest case, the one in red.

> "That one's been waiting [read the number off the screen]. In a real firm
> that's an audit finding, not a statistic. The screen is built to make that
> uncomfortable to look at."

### 0:30 — Open a case. This is the screen nobody else shows (30s)

> "The officer's job is narrower than it sounds: is this the person on the
> list, or someone who shares their name? That's it, that's the job."

Point at the applicant's date of birth, then at the listed date of birth
beside it.

> "Which is why everything is side by side rather than behind tabs — they're
> comparing two dates of birth, and if one's on another tab they're carrying it
> in their head. At case ninety of a shift, that's where mistakes come from."

Scroll to the reasons list.

> "Every score carries its reasons. A regulator asks why this person was
> refused, and 'the model said so' isn't an answer — which is why this is a
> points system and not machine learning."

### 1:00 — The thing that makes it real (20s)

Switch to the terminal. Run the replay:

```bash
cd web && npm run webhook -- --application <uuid> --times 3
```

> "Same webhook three times — vendors resend constantly, they can't tell a lost
> request from a lost response. Three 200s, one row stored, one job created.
> Idempotency enforced by a unique constraint, not by application logic."

### 1:20 — Land it (10s)

> "Two services, one Postgres, no HTTP between them — they talk through a jobs
> table claimed with `FOR UPDATE SKIP LOCKED`. The audit log physically rejects
> UPDATE and DELETE. And it's all in the repo with a limitations section that
> tells you exactly what it can't do."

Stop talking.

---

## When they interrupt

They will. These are the questions that actually come, with the short answer
and where to point.

### "Why not just use a queue? Why a database table?"

> "Transactional enqueue. If the queue were Redis, storing the vendor's message
> and enqueuing the job that processes it are two writes to two systems with no
> shared transaction — and there's no ordering of those two lines that's
> correct. Crash between them and you either have an event nobody will process
> or a job pointing at nothing. In Postgres it's one transaction."

Then volunteer the limit, because they are about to ask:

> "It stops being a good queue at a few hundred jobs a second — every claim is
> a write, and you start spending database capacity on queue mechanics instead
> of on users. `LISTEN`/`NOTIFY` is the next step, then move it out."

### "What happens if the webhook never arrives?"

> "It eventually won't — the vendor retries twice and then drops it. So there's
> a sweeper: every 15 minutes it finds applications waiting longer than they
> should and asks the vendor directly. Webhooks are the optimisation; the
> sweeper is what makes it correct. Push for latency, poll for correctness."

If they look interested, offer the demo — completing a verification with
delivery suppressed, then watching the sweeper recover it — but only if you
have time.

### "How do you know two workers don't process the same job?"

> "`FOR UPDATE SKIP LOCKED` — the exclusion is enforced by Postgres' lock
> manager, not by application logic, so there's no check-then-set window."

Then the part that lands:

> "I measured it rather than asserting it. Two thousand jobs, two workers: zero
> duplicates. Then I ran the same test with the locking removed — 380
> executions of 200 jobs, 180 of them run twice. There's a `--naive` flag in the
> repo that exists purely to demonstrate the bug."

### "Is that real sanctions data?"

> "The default list is fabricated so a fresh clone works offline with no
> licensing question. There's a downloader for the real OFAC SDN list —
> 19,385 entries, public domain because it's a US Government work. The numbers
> I'm quoting come from that. OpenSanctions is better data but it's CC-BY-NC,
> so commercial use needs a licence and it's deliberately not shipped."

### "How good is the name matching?"

The honest answer is the impressive one:

> "Good enough to be interesting and nowhere near good enough for production.
> True matches and false positives overlap at identical scores —
> 'Mohammed Al-Sayed' against 'Muhammad Al Sayyid' is the same person and scores
> 80; 'John Smith' against 'Jane Smith' is two people and also scores 80. No
> threshold separates them, because the information isn't in the strings."

> "So there's no single threshold — there are bands, and the ambiguous band
> routes to a human instead of pretending to decide. That's what the review
> desk is for."

### "Does it auto-reject sanctions matches?"

> "No, and that's the most interesting thing I found. When I let a confirmed
> name match refuse someone on its own, 0.58% of applicants who were on no list
> at all got auto-rejected — 'Carlos Garcia' matching 'Carlos Alberto GAXIOLA
> GARCIA'. On a hundred thousand customers that's 580 real people refused by a
> string comparison."

> "So a name match now guarantees review and never refusal; refusal needs a
> second independent signal. Recall didn't change. It's also what firms actually
> do — a potential match gets escalated and a human confirms identity first."

### "Can two officers decide the same case?"

> "No, and there are three layers. The UI hides the buttons on a decided case —
> that's convenience. The transaction takes a row lock and re-reads — that's a
> good error message, it can tell the second officer who beat them. And a
> partial unique index on terminal decisions makes it impossible even if both of
> those have bugs."

> "Plain `FOR UPDATE`, not `SKIP LOCKED` — deliberately the opposite of the job
> queue. The queue skips contended rows because another worker will take them.
> Here the loser is a person waiting for an answer, so it blocks and tells them
> what happened."

### "Is the data real? It looks real."

> "All invented. The interesting part is what gives synthetic data away —
> uniform timestamps are the big one, so arrivals are shaped by hour-of-day and
> weekday. Officers work office hours, which is why the queue builds overnight
> and drains on Monday. And 14% of applicants never finish, because a funnel
> where everyone completes is fiction and it flatters every percentage."

> "Every seeded audit row is tagged `seeded: true`, because that table is meant
> to be the honest record and I'm fabricating six weeks of it."

### "What would you do next?" / "What's missing?"

Do not improvise. The README has a limitations section; lead with the three
that matter:

> "No integration tests — the async behaviour is proved by demonstration, not
> asserted continuously. The sanctions list is loaded once per process and never
> refreshed, which for a screening system is a real problem. And there's no
> ongoing monitoring: customers are screened once at onboarding, when the
> obligation is to rescreen the book whenever lists change."

Naming these unprompted is worth more than anything else in the demo.

---

## If you have five minutes instead of ninety seconds

Add, in this order:

1. **The audit log refusing to be edited.** Run
   `update audit_events set actor = 'x'` in psql and let them read the error.
   Then `delete … where false` — matches nothing, still refused. It lands.
2. **The dropped-webhook recovery.** Complete a verification with delivery
   suppressed, show the application stuck, run the sweeper, show it recover
   with `"source":"sweeper"` in the audit trail.
3. **`worker/scoring.py`.** Just scroll it. Pure functions, every rule
   justified where it is defined, 121 tests that need no database.

## What not to do

- **Do not start with the applicant form.** It is the least interesting screen
  and it costs you twenty seconds of typing.
- **Do not claim the vendor integration is tested.** It is written against
  published documentation and has never run against the live API. Say so before
  they ask.
- **Do not quote a percentage without its denominator.** "80% auto-decided"
  invites a challenge you cannot answer; reading the tile aloud — the figure
  *and* the "N of M decided" underneath it — does not. Read what is on the
  screen rather than a number from this script: the share is a property of the
  applicant mix and changes every time the data is reseeded.
- **Do not hide the oldest case in the queue.** Whatever it is on the day —
  it has been anywhere from three to six weeks — pointing at it first is what
  makes the rest credible.
