# Design rationale — for a compliance reader

**KYC onboarding and compliance review desk**
Denis Xhabrahimi · October 2026

> A rewrite of [design-rationale.tex](design-rationale.tex) for a
> non-engineering reader. That document is unchanged and remains the more
> precise engineering account.

---

## Read this first

**The short answer.** If a regulator asks why a particular customer was
approved, rejected or escalated, this system can show: the outcome and a written
reason, who decided, the exact version of the scoring rules in force at that
moment with its points and thresholds written out in full, the risk score with
every signal that contributed to it, and the precise version of the sanctions
list the name was screened against, with that list's publication date. All of it
is stored data rather than something reconstructed from today's code, and the
scoring inputs are frozen once a decision exists, so the record cannot drift
after the fact. On the live demo, as at 10 October 2026, all 771 decision records carry the
rulebook version they were decided under; none is missing one.

**What it cannot show.** The audit trail and the rulebook are append-only. The
decision records are too **in the code, but that change is not yet applied to
the live demo** — see §3.3, because the distinction matters and is exactly the
kind of thing this document must not blur. And all of it is only
tamper-**evident**: a database administrator can disable the rules that enforce
it, and there is no write-once archive outside the database. The list
fingerprint attests to this
system's parsed copy, not the publisher's original file, and nothing verifies
it. One list only (OFAC), screened once at onboarding, with no ongoing
re-screening and nothing that refreshes the lists. There is no second-reviewer
requirement, no role separation, no suspicious-activity-reporting function, and
no retention or erasure policy. **None of this has been reviewed by a compliance
function.**

**If you have ten minutes,** read §1, §2 and §6 — what it is, the question it
answers, and what it deliberately does not do. §3 and §4 are the evidence
behind the claims, with each one tied to a named automated test you can run.
§5 is what went wrong while building it. §7 is for engineers.

---

## 1. What this is

A working demonstration of a customer onboarding and compliance review system:
it collects an applicant's details, sends them to an identity verification
provider, screens their name against a sanctions list, scores the result, and
then either decides automatically or puts the case in front of a reviewer.

**All data in it is synthetic.** Every applicant, name and address is invented
by a seeding script, and the identity provider is a simulator built into the
demo, not a real vendor — no real person's data has passed through it. The one
genuinely external artefact is the published OFAC sanctions list, used only to
screen invented names.

It is a portfolio piece, not a product, and has never processed a real customer.

### Words used here

| Term | Meaning in this document |
| --- | --- |
| **Decision record** | The row stored when a case is approved, rejected or escalated. |
| **Audit trail** | The dated record of everything that happened to a case, stored so entries can be added but not altered. |
| **Rulebook** | A numbered, dated version of the scoring rules: points, thresholds and country lists in force. |
| **Background task** | A unit of work — "screen this applicant" — held on a list the system works through. |
| **All-or-nothing write** | A group of changes that either all take effect or none do, leaving no half-finished state. |
| **Database rule** | A rule enforced by the data store itself, so it holds no matter which part of the software tries to break it. |

Compliance terms are used in their ordinary senses: **KYC** (the identity and
risk checks done at onboarding), **PEP** (politically exposed person — someone
in or close to public office, warranting extra scrutiny rather than refusal),
and **EDD** (enhanced due diligence — the additional checks applied to
higher-risk customers, which here means a human decides rather than the
system).

---

## 2. The question this answers

> **Why was this customer approved?**

The honest form is harder, because it is always asked later — after the rules
have changed, after the list has been revised, after the reviewer has left:

> **Why was this customer approved, under the rules as they stood that day,
> against the list as it stood that day — and can you show me that now?**

The system answers by storing the answer at the moment of decision, rather than
reconstructing it later from whatever the code says today. The chain an auditor
follows is:

**decision record → rulebook version → the points, thresholds and country lists
in force → the sanctions list version → that list's publication date and
fingerprint**

Every link is stored data, not code. Nobody has to read the software, or trust
that it has not changed since.

Each decision record carries the outcome, a written reason, who decided, the
rulebook version, the risk score with every contributing signal, and a pointer
to the list version screened against. The reason and the author cannot be left
blank (database rules: `decisions_reason_not_blank`,
`decisions_decided_by_not_blank`).

**Measured on the live demo, 10 October 2026:** 771 decision records across 737
applications — 555 approved, 84 rejected, 132 escalated. **Zero lack a rulebook
version.** 3,515 audit entries, 3 rulebook versions, one list version covering
19,393 entries.

---

## 3. What the system guarantees

What is promised, how it works, and how we know — each citing a named automated
test or a numbered change to the data store, so claims can be checked rather
than taken on trust.

### 3.1 Every decision names the rulebook it was taken under

No decision can be recorded without naming the rulebook version that produced
it, and that version must be one the system holds a complete written definition
of. The rulebook table rejects edits and deletions, and a newer rulebook leaves
older decisions pinned to their own.

*How we know:* a decision with no version is refused
(`test_a_decision_without_a_ruleset_version_is_refused`); one naming a version
that does not exist is refused
(`test_a_decision_cannot_name_a_ruleset_that_does_not_exist`); a stored rulebook
cannot be rewritten (`test_a_recorded_ruleset_cannot_be_rewritten`); and the
stored definition is checked against what the scoring code actually applies
(`test_the_recorded_definition_matches_what_scoring_actually_applies`) — the test
that stops the written rulebook drifting from the real one. Migrations
`021_decision_ruleset_version`, `024_rulesets`.

### 3.2 The evidence behind a decision is frozen

Once a case is decided or escalated, its risk score, the signals behind it, the
rulebook version and the time of scoring cannot change. The rule keys on *a
decision record existing* rather than on the case's status — because an escalated
case keeps the status "screening" while already having a decision, and that was
the case that could genuinely have been re-scored.

*How we know:* a decided case refuses every risk field
(`test_a_decided_application_refuses_every_risk_column`); an escalated one is
frozen although still screening
(`test_a_referred_case_is_frozen_although_it_is_still_screening`); other fields
stay editable, so the freeze is targeted
(`test_a_decided_application_can_still_be_updated_in_other_ways`). Migration
`020_freeze_risk_after_decision`.

### 3.3 The audit trail and the decisions can be added to, not altered

Every state change writes a dated entry, and the data store rejects edits,
deletions and bulk clearing of that table (migration
`006_audit_events_append_only`) — enforced there rather than by coding
convention, because the case an auditor worries about is precisely the one where
someone decided the convention did not apply.

**The decision records are protected the same way by migration
`026_decisions_append_only`** — which exists and is tested, but **has not yet
been applied to the live demo.** As at 10 October 2026 the live database has 25
migrations applied and its decision table still carries no such rule, so on the
live system a decision record remains editable by anyone with direct database
access. Stating this precisely rather than claiming the guarantee is the whole
point: a control that exists in a repository and not in the running system is
not a control.

Until migration 026 this was not protected at all, and it was the most serious
gap in this document's first draft: every other rule on that table governed what
a decision could *say*, and none stopped it being rewritten afterwards. A decision could have been edited to a different outcome
while the audit trail's own immutable record of the original survived alongside
it, contradicting it, with nothing to say which came first.

*How we know:* no column of a recorded decision can be edited
(`test_a_recorded_decision_cannot_be_rewritten`, covering outcome, reason,
author, score and rulebook version); a decision cannot be deleted
(`test_a_recorded_decision_cannot_be_deleted`) or the table cleared
(`test_truncate_is_refused_by_our_own_trigger`); a statement matching no rows is
still refused, which is what proves the rule is not one `where` clause away from
being bypassed (`test_a_statement_matching_no_rows_is_still_refused`); and the
original row survives a refused rewrite rather than the statement failing after
writing (`test_the_decision_survives_a_refused_rewrite`). Because every one of
those would also pass on a table nobody could write to at all, they are paired
with `test_inserting_decisions_still_works` and
`test_the_one_terminal_decision_rule_still_applies`.

**What it forecloses.** There is now no way to correct a decision, and there
cannot be one without a further change to the data store. That is the right
default — a system that can quietly edit its verdicts is worse than one that
cannot correct them — but it is a real constraint. A correction has to be
designed as a *new* decision referencing the one it supersedes, and that design
is not built.

**What it does not give you.** This is tamper-**evident**, not tamper-proof.
Anyone with administrator access can disable the rules. Real immutability needs
write-once storage, or a chain of fingerprints linking each entry to its
predecessor so a mid-history edit could not pass unnoticed. **Neither is built.**

### 3.4 No case is decided without being screened

A case cannot reach a decided state except from screening. Permitted moves are
rows in a table and the data store refuses anything not listed — enforced there
because two separate programs write these records, so a rule held in one is a
rule the other can break.

*How we know:* decided is reachable only from screening
(`test_decided_is_reachable_only_from_screening`), with exactly one route in
(`test_the_compliance_rule_has_exactly_one_route_in`); the identity provider can
never drive a case to decided
(`test_vendor_can_never_drive_an_application_to_decided`); and the code's own copy
of the rules is checked against the data store's
(`test_python_mirror_matches_the_database_exactly`). Migration
`017_lifecycle_enforcement`.

### 3.5 One final decision per case — and not on incomplete evidence

A case can be approved or rejected once. Escalations are deliberately unlimited,
because a case can be escalated, re-screened and escalated again and each is a
real event. Separately, if screening work for a case has failed and been set
aside, a reviewer is blocked from deciding it — though work merely waiting to be
retried only raises a warning, and the automatic path is unaffected.

*How we know:* two reviewers deciding one case produce one decision
(`test_two_officers_deciding_one_case_produce_one_decision`), and the data store
refuses the second **even with the application's lock removed**
(`test_the_constraint_refuses_a_second_decision_even_without_the_lock`) — the
guarantee does not depend on the software behaving well. A reviewer cannot decide
on set-aside evidence
(`test_an_officer_cannot_decide_while_screening_evidence_is_parked`) but decides
normally otherwise
(`test_an_officer_can_decide_normally_when_nothing_is_parked`). Migrations
`007_decisions_allow_referred`, `023_no_decision_while_evidence_is_stuck`.

### 3.6 The applicant is never shown what screening found

The applicant's status page shows their progress and their own details, never the
risk score, the routing, or anything a sanctions screen found. It works from a
list of approved event types, each mapped to a sentence written for that
audience; an unrecognised type renders **nothing at all**, so a new kind of event
stays invisible to applicants until somebody decides what they may be told.

Three reasons, in order of weight. Financial-crime regimes generally restrict
telling a customer what screening has happened about them, so the operating rule
is that screening detail never reaches its subject and the question does not
arise. It teaches evasion — "matched at 86%, downgraded because the date of birth
conflicts" names the field to change. And **most such matches are wrong**: a name
similarity is a finding about a name, not a person, and showing it attaches a
sanctioned individual's identity to someone who is ordinarily somebody else.

*How we know:* the page reveals no screening detail
(`test_the_status_page_reveals_no_screening_detail`) while still showing the
applicant's own name (`test_the_applicants_own_name_is_still_shown`) — the
counterweight, since a disclosure test is trivially passed by a blank page. An
unknown reference discloses nothing, including whether it exists
(`test_a_status_page_for_an_unknown_application_discloses_nothing`). This runs
against a real built copy of the web application, with a setting that turns every
reason the check *could not run* into a failure rather than a skip — because a
guard that silently does not run is worse than no guard.

### 3.7 Which sanctions list, as it stood that day

Every screening result points at the list version it was run against, which
records its source, the publisher's publication date, its record count and a
fingerprint of its contents. A version is usable only once loaded completely, and
the screening code refuses an unmarked one — so a half-finished load is unusable
rather than quietly returning fewer matches, which would look like a clean
result.

*How we know:* a half-loaded version is never searched
(`test_a_half_loaded_snapshot_is_never_searched`); a match points at the version
it was found in (`test_a_match_points_at_the_list_it_was_found_in`); identical
content loaded three times registers one version
(`test_loading_the_same_list_three_times_registers_one_snapshot`). Migrations
`018_sanctions_snapshots`, `019_sanctions_entries`.

**What the fingerprint proves, and does not.** It proves two runs recording the
same fingerprint used byte-identical content. It does **not** attest to the
publisher's document: it is taken of the file this system writes *after parsing*,
and that file is excluded from the repository, so a reader cannot recompute it.
And it is recorded, not verified — nothing checks it on loading, so a corrupted
file would be faithfully recorded as corrupt. A cross-check against the
publisher's declared record count would close that gap and **is not built**.

### 3.8 Repeated, delayed or out-of-order messages do not corrupt the record

Arriving results are recorded and the work they imply scheduled in a single
all-or-nothing write, so there is no window where one happened and the other did
not. Duplicates collapse, an older result cannot overwrite a newer one, and a
periodic task — running hourly — asks the provider about cases waiting too long,
so a result that never arrives is eventually fetched rather than awaited
forever. An hour is the fallback interval, not the normal one: the ordinary path
is driven the moment a result arrives, and on the production run of 9 October
2026 a case went from submission to decision in twenty seconds.

*How we know:* three identical messages store one event and one task
(`test_three_identical_webhooks_store_one_event_and_one_job`); re-processing
changes nothing (`test_reprocessing_a_result_changes_nothing`); an older result
cannot overwrite a newer (`test_an_older_result_cannot_overwrite_a_newer_one`); a
late result cannot reverse a decided case
(`test_late_result_cannot_regress_a_decided_application`); the periodic task
recovers a result never delivered
(`test_the_sweeper_recovers_a_result_no_webhook_ever_delivered`); and an
unrecognised provider status is refused rather than guessed at
(`test_unknown_vendor_status_is_refused_rather_than_guessed`).

### 3.9 Decisions are explainable by construction

Every score is a sum of named signals, each with a reason in plain words — no
model, no unexplainable number. A confirmed sanctions match is 60 points, a PEP
match 30, residence in a jurisdiction subject to an international call for action
40, a failed document check 40. Under 20 approves automatically, 20–79 goes to a
human, 80 and above rejects automatically.

Two consequences chosen deliberately. **A PEP match alone can never cause an
automatic rejection** — being politically exposed is not unlawful, wholesale
refusal is something regulators criticise, and the expectation is enhanced due
diligence, which here means a human looks. **A sanctions match alone routes to
review, not rejection**, because screening finds *names*, and whether a name is a
person is a reviewer's judgement.

*How we know:* every signal carries a readable reason
(`test_every_signal_carries_a_readable_reason`); the score is always the sum of
its signals (`test_the_score_is_always_the_sum_of_its_signals`); scoring is
repeatable (`test_scoring_is_deterministic`); and no single signal can
auto-reject (`test_no_single_signal_can_auto_reject`), a PEP match least of all
(`test_a_pep_match_can_never_cause_an_automatic_rejection`).

**The cost, stated plainly.** The weights are one person's judgement, never
back-tested against real outcomes because no real outcomes exist. A points system
is **explainable and unvalidated** — a genuinely different thing from being right.

---

## 4. What happens when something fails

**A crash mid-review.** Each background task runs as one all-or-nothing write.
If the program is killed partway through, everything it had done is undone: no
half-written decisions, no cases with a score but no outcome
(`test_marking_done_and_the_handlers_writes_commit_together`). The work is not
lost — and because the attempt count rises when work is *claimed* rather than
when it fails, work that crashes the program hard enough to skip any failure
handling still counts against its retries. Otherwise it would retry forever,
killing the program each time. After a set number of attempts it is set aside
for a human (`test_a_job_abandoned_by_a_dead_worker_is_reclaimed`,
`test_a_reclaimed_job_with_no_attempts_left_is_parked_not_requeued`). This is
not theoretical: over five days in October 2026 every automatic decision failed
and **nothing was corrupted**, precisely because each failure undid itself (§5).

**A duplicate submission.** Three identical arrivals store one event and
schedule one task; re-processing an already-handled result changes nothing
(§3.8).

**Two reviewers acting at once.** Both press approve; one decision is recorded
and the other reviewer is told the case is already decided rather than silently
overwriting it. Two mechanisms sit behind that deliberately — the desk locks the
case so the second reviewer waits, and independently the data store permits only
one final outcome, which is tested *with the lock removed* (§3.5). A further
guard reads the source code and fails if the desk's lock is ever changed to the
skip-if-busy variety, because that would leave every data-store rule intact while
telling the losing reviewer the case **does not exist**
(`test_the_desk_lock_has_not_quietly_become_skip_locked`).

**Two background programs claiming the same work.** Work is claimed one item at
a time under a lock that makes a second program skip to the next item rather
than wait. Measured, not asserted. **Re-measured 10 October 2026**, two
programs against 200 items, five runs each way: with the lock removed, 381 to
396 executions of 200 distinct items — **181 to 196 of them run twice**. With
the lock, 200 executions and **zero duplicates, five runs out of five**
(`test_two_workers_never_process_a_job_twice`). The spread is the point: how
often two programs land on the same item is scheduling luck, which is what a
race means. A deliberately broken version is
kept so the comparison stays reproducible, and a test asserts it is *still
broken* (`test_the_naive_claim_really_is_broken`) — otherwise someone fixes it
and the comparison quietly becomes a claim about nothing.

---

## 5. What broke while building this

Every entry is drawn from the project's decision log, its recorded incident, or
its commit history. None is invented.

**The applicant page showed what screening found.** It originally rendered audit
entries directly — including *"Sanctions match against X on OFAC SDN at 86% name
similarity"* — on a page reachable by reference with no login. The project's own
engineering account calls this "the most serious mistake in the project." Fixed
by the approved-list approach and tests in §3.6.

**The same applicant could score 60 or 75 for no reason.** An applicant matched
three sanctions entries at exactly equal strength, two without a date-of-birth
conflict and one with. The scoring took "the strongest match", which among equals
meant *whichever happened to be listed first*. Found by running two
implementations side by side over 400 applicants and comparing every result
(`test_the_two_matchers_agree_on_every_applicant`) — not by testing either alone,
because neither was wrong on its own. Fixed by sorting on strength then entry
identifier, making the tie-break a decision rather than an accident.

**A performance shortcut silently lost real matches.** Screening was sped up by
first narrowing to a shortlist, but the first version applied the rule "two name
parts must correspond" using a *different* similarity measure from the scoring's.
**Thirteen applicants in 400 lost a real match** — every one a miss, never a
false positive, which is the dangerous direction. (Measured 24 September 2026 by
the matcher-equivalence comparison; not re-measured for this document, because
reproducing it means re-running that comparison at three different thresholds.) Fixed by having the shortlist
ask only the weakest useful question and leaving every judgement to the scoring.
The principle recorded: **a cheap pre-filter must only ever widen**; the moment
it reimplements the expensive check with a cheaper measure it can disagree, and
every disagreement is a match nobody sees.

**A country list never checked against a source.** The jurisdictions subject to
an international call for action were a hand-typed constant which, checked
against the published source, proved to be a set **FATF never published** —
Monaco, listed June 2024, beside Türkiye and the UAE, both removed by then. Such
lists are published complete at each plenary, so an absence is as much a
statement as a presence: a set assembled from two meetings screens against
countries already cleared *and* misses countries added since. Fixed with one
statement's complete set, carrying its publication date, the file it was read
from and that file's fingerprint, plus a test that fails if the codes change
without the date
(`test_the_monitoring_list_and_its_plenary_date_cannot_drift_apart`). The
superseded rulebook records in its own change note that its list was unsourced —
written from memory and never checked
(`test_an_unsourced_revision_still_says_so_in_the_data`).

**Five days of silently failing decisions, 3–8 October 2026.** The background
program was updated to record a new field and deployed automatically on each code
change; the matching change to the data store had not been applied. Every
automatic decision failed. Nothing was corrupted — each task is one
all-or-nothing write — but nothing was decided, and **nothing said so**: the
scheduled call driving the work reported success whether or not the work inside
it succeeded. It was measuring whether the request arrived, not whether the work
happened. Found not by monitoring but by preparing a deployment and asking
whether the code had already shipped on its own — half of it had. Fixed three
ways: the program no longer deploys itself; the scheduled call now reports
failure with counts of what failed and what was given up on; and the deployment
order is written down and was rehearsed against a copy of live data first. Proven
both ways on the live system on 9 October. Full account:
[incident-2026-10-03.md](incident-2026-10-03.md).

**The alarm that worked and could not be read.** When a failure was forced on the
live system to prove that fix, the alarm *did* sound — but the explanation never
reached the log. The counts of what had failed were sent and then discarded by
the thing receiving them, leaving a log saying only "error", with no way to tell
one transient blip from work the system had given up on. Found on 9 October 2026
by forcing a failure rather than by reading the configuration — and it had
survived being written, reviewed and shipped *as the fix for an incident about
unreadable signals*. Fixed, and recorded in the incident note.

**A test that had been running nowhere for two days.** The side-by-side
comparison above needs a loaded list and a seeded population; the automated
runner provided neither, so the job failed on a missing table and stayed red for
seven code changes while the project's own badge advertised it. It passed on the
author's machine throughout, which is why nobody saw it. Fixed by having the
runner provide what the test reads. What had failed was not the test, but the
noticing.

**The pattern underneath four of these** is recorded in the decision log as a
recurring class of defect: **something that looks like a check and is not one.** A
screen against half a list looks clean. A scheduled call that cannot fail reports
health it never measured. A figures page that returns "success" while showing
only a placeholder (§6) says nothing about whether its data loaded. A test that
never runs is indistinguishable from one that passes.

---

## 6. What this demo deliberately does not do

The most important section. The guarantees above are real; these gaps are
equally real.

### Controls a regulated firm would expect, and that are absent

- **No four-eyes principle** — meaning a second person must approve certain
  decisions before they take effect. One reviewer can approve any case alone.
- **No role separation.** Every signed-in user can decide anything.
- **One shared staff account**, password held in plain text in a configuration
  file. No per-user accounts, no password reset, no second factor, and sessions
  cannot be revoked once issued.
- **No suspicious activity reporting.** There is no way to raise, record or
  track a SAR — the disclosure a firm makes to its financial intelligence unit.
  The disclosure boundary in §3.6 is written in the *spirit* of tipping-off
  restrictions, but there is no reporting function to protect.
- **No ongoing or periodic re-screening.** A customer is screened once, at
  onboarding. Nothing re-screens an existing customer when a list changes.
- **No MLRO function modelled.** The MLRO — the individual personally
  accountable for a firm's anti-money-laundering controls — has no role, view or
  sign-off here.
- **No risk rating beyond onboarding**, and no segmentation by product, channel
  or transaction behaviour, all of which a real risk model uses.

### Limits of the evidence itself

- **Decision records are still editable on the live demo.** Migration
  `026_decisions_append_only` closes this — it refuses edits, deletions and
  clearing of the decision table, as migration 006 has always done for the audit
  trail — and it is written and tested (§3.3). **It has not been applied to the
  live database**, which as at 10 October 2026 has 25 migrations applied. Until
  it is, someone with direct database access could alter a decision's text.
  Separately, and permanently: **every decision taken before that migration was
  editable while it was being taken**, which no later control can retroactively
  fix. That is a statement about this evidence's history, not about its future
  protection.
- **The audit trail is tamper-evident, not tamper-proof** (§3.3).
- **The list fingerprint attests to this system's parsed output, not the
  publisher's file**, and nothing verifies it on loading (§3.7).
- **Only the OFAC list is used** — no EU, UN or UK consolidated lists, and a
  test enforces that nothing else is downloaded
  (`test_only_ofac_is_ever_downloaded`). A real firm screens several.
- **The lists go stale and nothing refreshes them.** They load at deployment.
  Recording the publication date makes staleness *visible* — an old date beside
  a recent decision is readable evidence — which is better than invisible, but
  not the same as solved.
- **The risk weights are unvalidated** (§3.9).

### Operational and data-protection gaps

- **No retention or erasure policy for personal data.** Records are kept
  indefinitely. Real KYC records carry both statutory retention *and* deletion
  obligations, which conflict in genuinely difficult ways with an append-only
  audit trail. This system does not attempt that problem.
- **No subject access or erasure process.** Both are legal requirements.
- **No encryption beyond the host's**, and no field-level encryption.
- **No alerting.** Nothing says that set-aside work is accumulating. A
  deployment runbook exists; monitoring does not. **No backups or disaster
  recovery.**
- **The review queue shows 200 cases** and silently omits the rest.
- **The browser is largely untested** — automated checks cover the data store's
  rules and the web application's type correctness, but nothing drives a page,
  except the disclosure boundary in §3.6. A fault in a user-interface component
  could pass unnoticed.
- **The figures page reports success even when it cannot reach the data.** It
  returns a normal success response and a loading placeholder when the data store
  is unreachable, because the response begins before the queries run. A reader,
  or an automated check, sees "success" and an empty page. A known instance of
  the §5 pattern, and **not fixed**.

### On what this document is not claiming

**Nothing here should be read as saying this system complies with any
regulation, meets any standard, or has been reviewed or signed off by anyone.**
It has not. What is described is what the system *records* and what it
*enforces*. Whether that suffices for any particular obligation is a question
for a compliance function, and none has examined it.

Before real customers, at minimum: a real provider contract and integration
testing against it; ongoing screening against several lists; a validated,
back-tested risk model with documented governance; per-user authentication with
multi-factor and role separation; four-eyes on higher-risk decisions; a
retention and erasure policy reconciled with the audit requirements; write-once
storage for audit records; tests that drive a browser; monitoring with an
on-call rota; and a compliance review of all of it.

---

## 7. Technical appendix

For engineers; the main text deliberately avoids this vocabulary.
[design-rationale.tex](design-rationale.tex) has the full argument, the rejected
alternatives and the measurements behind them.

**Architecture.** A Next.js web application and a Python worker share one
PostgreSQL database and never call each other directly, with one tested
exception below. Schema changes are numbered SQL migrations belonging to neither
service, so neither language's tooling owns the shape of shared tables.

**The job queue is a Postgres table**, so enqueueing is in the same transaction
as the write that caused it — eliminating the window where a webhook is recorded
but its work never scheduled, or vice versa. Redis, Celery and `pg-boss` were
all rejected; the reasoning is in the original document. This stops being the
right choice somewhere in the low thousands of jobs per second.

**Claiming** uses `FOR UPDATE SKIP LOCKED` in one CTE-plus-UPDATE statement;
without `SKIP LOCKED` a second worker blocks on a locked row rather than moving
on, serialising the pool. `attempts` increments at claim time, not on failure,
so a process-killing job still exhausts its retries.

**Lifecycle.** `started → submitted → checking → screening → decided`, enforced
by a `BEFORE UPDATE` trigger raising `restrict_violation`, with permitted
transitions as rows in `application_transitions`. A Python mirror exists for
pre-flight checks; the database is authoritative and a test asserts they agree.

**Append-only tables.** `audit_events` (006), `rulesets` (024) and `decisions`
(026) all carry statement-level `BEFORE UPDATE / DELETE / TRUNCATE` triggers
raising `restrict_violation`. Statement-level rather than row-level so that a
`where false` clause is refused too. `decisions` is additionally protected by a
partial unique index (`decisions_one_terminal_per_application`, covering only
`approved` and `rejected`), a `NOT VALID` check on
`risk_ruleset_version_at_decision`, and a foreign key to `rulesets`. Note the
ordering: migration 021 backfills `decisions` with an UPDATE and runs before
026, so a database rebuilt from scratch backfills and only then locks. One
difference from `rulesets`: nothing holds a foreign key *to* `decisions`, so its
TRUNCATE refusal comes from the trigger rather than from a referential error,
which is why its test asserts the message and the `rulesets` test does not.

**The risk freeze** (020) keys on the existence of a decision row rather than
`status = 'decided'`, because a referred case retains `status = 'screening'`
while already having a decision — the reachable re-scoring path. It uses
`IS NOT DISTINCT FROM`, so rewriting a column to its current value is not an
error.

**Two-stage matching.** A trigram GIN index over name *tokens* narrows the
list to a shortlist, then the original in-memory comparison runs over it. Tokens
rather than whole strings because transliteration drift lives inside words and
an extra middle name wrecks a whole-string comparison.

*Re-measured 10 October 2026.* The OFAC snapshot in production holds **19,393
entries**, which is also the publisher's own declared record count, and
**44,017 name-plus-alias spellings** (43,555 distinct once normalised) counted
from the source file as committed. Running the real shortlist query over 400
applicants gives a **median shortlist of 228 and a mean of 425** — about **1.2%
of the list**, not the "roughly 2%" the original document stated. Whole-string
trigram similarity for `ahmed hassan` against `ahmad hasan` is **0.4706**,
confirming the 0.471 originally published.

*Measured 24 September 2026 and not re-measured here.* The best-matching token
across 131 accepted matches never fell below 0.357, with a median of 1.000; and
the threshold was chosen by sweep — 0.35 lost 13 applicants' matches, 0.30 lost
one, 0.25 lost none. Reproducing either means re-running the matcher-equivalence
comparison, at three thresholds for the second.

*Latency, measured 24 September 2026 and not re-measured here*, because the
harness that produced it was never committed: the in-memory scan was flat at
190–228ms, being a full comparison whatever the name, while two-stage spanned
13–623ms, its cost set by how many entries the pre-filter admits. Faster for
almost everyone, occasionally worse, and less predictable. A latency budget here
has to be written against the tail, and the tail is the number that moves.

**The one permitted cross-service call.** After enqueueing, the web application
sends a fire-and-forget POST to the worker's HTTP entry point so an applicant
sees a decision in seconds. Permitted because the work is already committed
before the call; it carries no body and expects no response; it is not awaited
and cannot fail the triggering request; and a scheduled path does the same work
if it never arrives. Reduces to: delete the call and the system is still
correct, only slower — confirmed unintentionally when it silently did nothing
for a period after deployment and nothing broke.

**The worker in production** is an on-demand function whose second entry point
claims a bounded batch of five jobs and returns, rather than the local
long-lived loop. It is driven two ways: the web app's fire-and-forget call the
moment work is enqueued, which is what makes the applicant-facing path fast,
and a scheduled job **hourly** as the fallback. The cadence was lowered from
every five minutes on 10 October 2026 — see `decisions.md` under *"Hourly
instead of every five minutes"*. The schedule is the only thing that drives the
two recurring jobs, so its interval is also the worst-case delay before
abandoned work is reclaimed (roughly seventy minutes) and before a stuck
application is chased with the vendor. Both entry points import the same job-execution function, so
transaction boundaries and retry accounting cannot diverge, and the
no-double-processing proof runs against both. The endpoint returns HTTP 500 when
any job in the batch failed, with `failed` and `parked` counts in the body.

**Further reading.** [decisions.md](decisions.md) — full decision log, including
what was rejected and what proved wrong. [concepts.md](concepts.md) —
plain-English glossary of every term. [fragilities.md](fragilities.md) — known
brittleness carried deliberately.
[incident-2026-10-03.md](incident-2026-10-03.md) — the five-day failure.
[deploy.md](deploy.md) — the deployment runbook.

---

*Synthetic data throughout. Every figure was measured or cited from a named
test, migration or decision-log entry; none is estimated.*
