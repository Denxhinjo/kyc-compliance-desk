# Walkthrough: making a past decision defensible

**What this document is.** A plain-language account of every change made from
migration 020 onwards, in the order it happened, with the commit for each. It is
written so that the reasoning can be repeated to someone who knows the domain
better than the person repeating it.

Each item follows the same shape: what changed and what problem it solves, the
mechanism (how it works, not just that it works), why this approach rather than
the obvious alternative, and what it does **not** guarantee. That last part is
the one worth keeping — a claim with its limits stated survives questioning; a
claim without them does not.

Built from `git log`, the migration files, the test suite and
[decisions.md](decisions.md).

---

## Terms, as they come up

A few words are used throughout. The rest are defined where they first appear.

**Migration.** A numbered SQL file that changes the database's structure. They
run in order, once each, and the runner records which have been applied. The
database's shape is the sum of its migrations, which is why they are never
edited after being applied — see the last section.

**Trigger.** A rule the database itself enforces, attached to a table, which
runs automatically whenever someone tries to change a row. The important
property is that it applies to *everyone*: the Python worker, the TypeScript web
app, and a person typing SQL by hand. Code can forget a rule; a trigger cannot
be bypassed by forgetting.

**Ruleset.** The scoring rules in force at a moment: how many points each risk
signal is worth, where the approve/reject lines are drawn, and which reference
lists the scoring reads. Identified by a version string like `2026-10-1`.

**The problem the whole round addresses.** A compliance officer who approved
someone in March must still be able to explain, in October, why. That requires
knowing the rules as they stood in March — not the rules as they stand now.
Everything below exists to make that possible.

---

## 1. Freezing the scoring columns once a decision exists

**Commit `b6f3732`, migration `020_freeze_risk_after_decision.sql`**

**What changed.** An application's scoring columns — the score, the reasons
behind it, and the ruleset version used — can no longer be changed once a
decision has been recorded for that application.

**The problem.** They could be. Migration 017 added a trigger guarding the
application's *status* (started → submitted → checking → screening → decided),
and that trigger deliberately steps aside when the status is not changing,
because writing a score is not a status change. Nothing else covered the score.
Measured against the real database, inside a transaction that was then undone:

```
risk_ruleset_version   -> SUCCEEDED
risk_score             -> SUCCEEDED
risk_signals           -> SUCCEEDED
status (control)       -> REJECTED: decided -> screening
```

The control line matters. The status trigger was working the whole time; it
simply had nothing to say about those three columns. So a decision could end up
pointing at a ruleset version it was never taken under — the exact failure the
version was stored to prevent.

**And it was reachable, which was the surprise.** A case the system cannot
decide automatically is *referred* to a human. A referral is recorded as a
decision row, but the application deliberately stays at status `screening`,
because the case is not actually decided until a person decides it. Meanwhile,
the screening job that produces scores only refuses to run when the status is
*not* `screening` — so referred cases pass that check. Jobs are delivered
*at-least-once* (a queue that guarantees delivery will sometimes deliver twice),
so a repeat delivery would re-score a referred case and overwrite the evidence
the referral was based on. Nineteen applications were in that state. One, a UAE
applicant, would have re-scored from 60 to 45, because the UAE had left the FATF
monitoring list in the meantime.

**The mechanism.** A trigger on the applications table that runs before any
update. It first asks whether any of the four scoring columns is actually
changing; if not, it steps aside, so ordinary updates (linking a vendor session,
touching a timestamp) are unaffected. If one *is* changing, it checks whether a
decision row exists for that application, and refuses if so.

**Why it keys on the decision row and not on `status = 'decided'`.** Those are
different sets, and the difference is the entire point. A referred case has a
decision row while still sitting at `screening`. Keying on status would have
closed the case no code can reach and left open the one the code reaches by
design — a guard that looks right and protects nothing.

**Why a trigger and not a condition in the query.** Adding `and status =
'screening'` to the one UPDATE statement would have worked. It would also have
been a rule living in one function in one language, which the next person
writing the other language does not inherit. The database is shared by Python
and TypeScript and owned by neither, so the rule is stated once where both are
bound by it. The Python file was deliberately left unchanged.

**What it does not guarantee.** It does not protect against someone deleting the
decision row first, which would unfreeze the application. Nothing in the code
deletes decisions, but nothing forbids it either. It also does not constrain
`INSERT` — a row could in principle be created already decided.

**Tests.** `test_a_decided_application_refuses_every_risk_column` (each of the
four columns, as raw SQL rather than through the Python, because the whole claim
is that it binds writers who never consulted it);
`test_a_referred_case_is_frozen_although_it_is_still_screening` (the reachable
case); and two counterweights — `test_scoring_is_allowed_before_any_decision_exists`
and `test_a_decided_application_can_still_be_updated_in_other_ways` — because a
guard that refused everything would pass the first test while breaking scoring
entirely.

---

## 2. Copying the ruleset version onto the decision itself

**Commit `b6f3732`, migration `021_decision_ruleset_version.sql`**

**What changed.** Each decision row now stores the ruleset version it was taken
under, in its own column, rather than relying on the application row to still
say.

**The problem.** The decision already stored the *score* it was made on, for the
reason migration 004 gives: an auditor asking why someone was approved in March
needs March's number, not today's recalculation. The version was not stored
there — it lived on the application, which is a row that goes on changing.
Freezing it (item 1) stops the drift; storing it on the decision means
reconstructing a decision never depends on another table still saying what it
said.

**The mechanism.** A nullable text column, `risk_ruleset_version_at_decision`,
mirroring the existing `risk_score_at_decision` — same table, same naming, same
"as it stood" meaning, rather than inventing a second idiom for the same idea.
It is written at every point in the code that creates a decision. There turned
out to be four: the automatic worker path, the compliance officer's action in
the web app, the data seeder, and a diagnostic script. Had only the first been
updated — the one named in the original brief — every human decision would have
been recorded with no version at all.

**How the backfill was verified.** A *backfill* fills in a new column for rows
that already exist. The obvious approach is to copy the application's current
version onto every decision. That would be asserting that no application was
ever re-scored after being decided — and before item 1, that was possible. So
the migration only copies the value where the evidence supports it: where
`risk_scored_at` (when the score was written) is at or before `decided_at` (when
the decision was taken). Where the score was written *after* the decision, the
application's version cannot be trusted to be the one the decision used, so the
column is left `NULL` — the database's "no value", which here means "not
established from the data". That is a true statement; a filled-in value that
might be wrong is not, and would defeat the column's purpose.

On this database the gate excluded nothing: **915 of 915 rows filled, 0 left
NULL.** The gate stays in the migration regardless, because it also has to run
against the deployed database, where the counts may differ. The migration prints
its own counts as it runs.

**What it does not guarantee.** `risk_scored_at` only moves when the scoring
code writes it. Someone editing the version directly in a SQL console would
change it and leave that timestamp untouched, and the backfill would copy the
new value believing it to be the old one. The audit log corroborates — it is
append-only, no application has more than one screening event, and none is dated
after its decision — but corroboration is not proof. The honest claim is
"consistent with every record the system kept", not "certain".

---

## 3. Requiring the version on every new decision

**Commit `2125b83`, migration `022_decisions_require_ruleset_version.sql`**

**What changed.** The database now refuses to record a decision that does not
name a ruleset version.

**The problem.** Four places in two languages had each been taught to fill the
column in. That arrangement holds until someone adds a fifth. It matters most
for the officer's path, which has **no automated test** — the web project has a
type-checker and no test runner at all — so for that path this constraint *is*
the test.

**The mechanism.** A *CHECK constraint*: a rule the database applies to each row,
here requiring the column to be non-empty. Added with the `NOT VALID` option.

**What `NOT VALID` means, and why it is load-bearing.** Normally, adding a
constraint makes the database scan every existing row to confirm they all satisfy
it, and refuse the change if any do not. `NOT VALID` says: enforce this on every
new and updated row from now on, but do not go back and check the old ones.

Both halves are needed here. New decisions get checked. Old decisions are left
alone — because item 2 deliberately left the column `NULL` wherever the data
could not establish a version, and a validating constraint would have forced
those rows to be filled with something, and the only available something is a
guess. The whole point of the `NULL` was to not guess. The migration documents
the command to validate it later, and says to run it when the claim is true
rather than to make the constraint look tidy.

**Why not just fix the four code paths and trust them.** That is what had already
been done. The constraint is the answer to "and what about the fifth".

**What it does not guarantee.** It does not say the version is *correct*, only
that one is present. And because it is `NOT VALID`, it makes no claim about rows
that predate it.

**Tests.** `test_a_decision_without_a_ruleset_version_is_refused` (raw SQL,
asserting the constraint name) and `test_the_constraint_does_not_disturb_rows_that_predate_it`.

---

## 4. Two guards around evidence that could not be recorded

**Commit `364e279`, migration `023_no_decision_while_evidence_is_stuck.sql`**,
and the warning added in **commit `f0f3965`**

**The problem the freeze created.** Freezing the scoring columns stops a
referral's evidence being overwritten. It does not stop *new* evidence arriving,
and a repeat screening job can genuinely carry some, by two routes. First, the
sanctions list: the screening code uses the newest fully-loaded list, not the one
the referral was based on, so a reload can surface entries that did not exist
before. Second, the identity vendor: several of its outcomes all map to the same
internal stage, so a corrected result ("declined" revised to "approved") changes
the inputs without changing the status — and that single change moves the
document component of the score by 40 points.

With the freeze in place, such a job raises an error, retries, and is eventually
*parked* — the queue's term for a job that has exhausted its attempts and will
not be retried again. Because the whole job runs in one transaction, nothing is
half-written and nothing is corrupted. But nothing is **recorded** either: the
new evidence exists only inside a failed job, where no officer will ever see it.

**What changed — the hard block.** The database refuses to record *an officer's*
decision on an application that has a parked screening job. The case view
explains why, so the officer meets a sentence rather than an error.

**The mechanism.** A trigger on the decisions table, before insert. It only acts
when the decision is being made by a person (the `decided_by` field distinguishes
`staff:…` from `system`), then looks for a parked screening job for that
application and refuses if one exists.

**Why only the officer's decisions.** The automatic path is deliberately exempt.
A parked job on an application with no decision yet would otherwise stop the
worker from recording the screening that had just succeeded. The system writing
down what it found is never the thing this rule should prevent; a human signing
off while the system is holding unrecorded evidence is.

**What changed — the soft warning.** A screening job that is *queued with an
error recorded* — meaning an attempt failed and another is scheduled — produces a
visible warning on the case view, and does **not** block the decision.

**Why one blocks and the other does not.** They are different claims. A parked
job is terminal: the system has given up, and whatever it found is definitively
unrecorded. A retrying job may still succeed, and blocking on it would freeze the
desk for every transient hiccup, including first-time deliveries that are about
to work. So the database refuses only on the unambiguous signal, while the page
warns on the broader one. The officer can still decide; they simply know that
evidence may still be arriving, which is the difference between a judgement call
and an accident.

**What it does not guarantee.** There is a window. A job carrying new evidence
that has not yet exhausted its retries has not parked, so the hard block does
not apply during the backoff period. For a frozen referred case, parking is
certain rather than likely — the error will recur on every attempt — but the
window is real and is documented in the migration. Nor does any of this
*record* the new evidence; it only prevents deciding in ignorance of it. The
real fix is written up as a proposed task ("superseding assessments") and
deliberately not built.

**Tests.** Ten, covering both branches in both directions: parked blocks and
does not warn, retrying warns and does not block, the warning does not prevent
the insert, the automatic path is exempt, a clean case and a completed job
trigger neither, and a parked job belonging to a *different* application is
irrelevant.

---

## 5. Recording what each ruleset version actually contained

**Commit `258e1bb`, migration `024_rulesets.sql`**

**What changed.** A new table, `rulesets`, holding one row per version: when it
took effect, its points, its thresholds, the reference lists it read, and a
change note saying why it exists. Plus *foreign keys* — a database rule that a
value in one table must exist in another — so an application or a decision cannot
name a version nobody wrote down.

**The problem.** A decision recorded a version string. What the string *meant*
lived only in the Python source and in the project's git history. "Decided under
2026-09-1" is only an answer if `2026-09-1` is something you can look up, and
until this table the honest reply was "clone the repository and check out the
right commit". An auditor cannot read git.

**Why it is append-only.** The table's rows cannot be updated or deleted, enforced
by triggers in the same style the audit log already uses. A ruleset definition
that can be edited later defends nothing: the entire value of the table is that
"2026-09-1 contained these points" is a statement nobody can quietly revise after
a decision has been defended with it. Corrections are made by recording a *new*
version — which is what `2026-09-2` already was.

**How `2026-09-1`'s contents were established.** Not from memory, and not
retyped. A script read the current source for the newer version and
`git show ea139d3:worker/scoring.py` for the older one, and generated the SQL, so
the values in the migration are the values the code held.

One judgement was involved: whether the older version's *points* could be
established at all, given the file had changed since. They could. A comparison of
every upper-case constant across the version-bump commit shows that only the
country lists and the version string differ — every points table, threshold and
match band is identical. The comment above the version constant claims the same
thing, and the history corroborates it rather than the other way round. The codes
were also byte-identical between the two commits within that version's life, so
it had one set of codes throughout; had they moved mid-version, what the version
contained would have been ambiguous and stored as `NULL`.

**What is deliberately not in it.** The sanctions list. The scoring code never
reads it — screening runs first and passes its findings in — and which list
version informed a particular decision is already recorded per screening run. A
decision depends on both the rules and the list, and the two are versioned in the
two places they belong.

**What it does not guarantee.** The table records what a version contained; it
does not prove the version was *applied* correctly. And the foreign keys constrain
values that are present — `NULL` is still allowed, deliberately, because item 2
needs it to be.

---

## 6. The country list that was never sourced

**Commit `f0f3965`**

**What was found.** The scoring adds points for an applicant's country, using the
FATF lists. **FATF** — the Financial Action Task Force — is the
intergovernmental body that publishes, roughly three times a year, two lists of
jurisdictions with weak anti-money-laundering controls: one calling for
*counter-measures*, one under *increased monitoring*.

When the scoring was first built, it shipped those lists under this comment:

```
#: FATF listings, as at 2026-09. MUST be re-checked against fatf-gafi.org
#: before any real use — this is a legal list with a publication date, not a
#: constant.
```

The comment is scrupulous about the general risk and silent about the specific
fact: **the codes were written from memory.** Never fetched, never cited to a
published statement, never checked against anything. "As at 2026-09" implied a
currency the list never had, and the warning read as caution about a list going
stale rather than what it was — a list with no source to go stale from.

It could not have been real. The monitoring set held Monaco, added to that list
in June 2024, alongside Türkiye and the United Arab Emirates, both removed by
then. FATF has never published that combination. It stood for seven days before
being compared against the source and replaced.

**How it is recorded.** Every decision in this database carries that version. The
decision rows were **not changed**, and should not be — they record what they were
actually scored against, which is what a decision record is for. What changed is
that the `rulesets` table now says so: the change note opens with "DECISIONS
UNDER THIS VERSION WERE SCORED AGAINST AN UNSOURCED COUNTRY LIST", and the
reference data carries a `sourcing: "unsourced"` flag — against `"sourced"` for
the newer version, so the field means something by contrast rather than being
decoration.

**Why not simply correct it quietly.** Because the correction is the newer
version, and the older version's statement about itself has to stay true. A
compliance officer defending a decision taken under the old rules needs to know
what those rules actually were, including that part.

**What it does not guarantee.** Nothing about the past decisions changes. They
were scored against an unsourced list and still are. The record now says so;
that is the whole remedy available.

**A note on scope.** The synthetic-data disclaimer does not cover this. The
applicants are invented on purpose and say so everywhere. The country list was
invented by accident and presented as a legal reference.

---

## 7. The reference-data audit

**Commit `fab2018`** (the stale labels and the recipes), **`3ef5220`** (the
coverage report), **`031f9e5`** (the address data and the threshold)

**What changed.** Having found one piece of invented reference data, the obvious
question is whether there is more. A search covered every constant and data file
presenting itself as real-world reference data. Three items needed fixing.

Two files labelled hardcoded country codes `# FATF increased monitoring` in
comments. Those were claims about the real world and they were wrong: Nigeria,
Türkiye and the UAE had all left that list. Both files now derive their
higher-risk countries from the sourced constant, so no comment asserts
membership and a plenary that moves the list moves the code with it.

A third comment read `# ~93% clean, matching a real document-check pass rate` —
an industry statistic that was never sourced. It is relabelled as what it is: an
invented demo assumption, chosen so the document component of the score varies
across its bands.

**A worse instance the audit had not reached.** The seeder's "borderline" recipes
hardcoded three countries and documented them in their notes as monitoring
jurisdictions worth 15 and 20 points. By the newer version those numbers were
wrong. Nothing failed, because the `expected` value is a comment and the real
score is computed separately — which is precisely how a wrong comment survives.
The recipes now take their countries from the constants, and a test asserts every
documented value matches what the rules actually produce.

**A second-order problem the fix created, and its answer.** Deriving the
countries from the sourced list exposed that the seeder can only place an
applicant where it has address data — and it had data for exactly *one* of the
22 monitored jurisdictions. So the country-risk dimension of the demo was being
exercised by one country, silently. Address data was added for all three
call-for-action jurisdictions and eight monitored ones, chosen for spread of
region and postcode shape; coverage went from 1-of-22 to 9-of-22 and 1-of-3 to
3-of-3. The seeder now *reports* its coverage every run and warns when a list
falls below a third represented, with the reasoning stated: a dimension that
barely fires is a dimension nobody is testing.

**What they do not guarantee.** Thirteen monitored jurisdictions still cannot
appear in the demo. That is now visible rather than silent; it is not solved.

---

## 8. A check that could not tell "no leak" from "could not look"

**Commit `3ef5220`**

**What changed.** The test that proves the public applicant page never reveals
screening findings can no longer fail because the page was unreadable.

**The problem.** Twice, a stale development server held its port while its
rendering processes were dead, answering every page with an HTTP 500 error. The
disclosure test failed — and failed looking exactly like a leak, because an
assertion about a page's contents cannot distinguish "I read the page and found
something that should not be there" from "there was no page to read".

The danger runs in the unhelpful direction. Having been fooled twice, the cheap
explanation the third time is "probably the stale server again", and that is how
a real leak ships.

**The mechanism.** The fetch helper now classifies what came back. A connection
failure, any server error, or a status the caller did not declare it could
reason about raises a distinct "unreachable" condition, which is reported as
*the check could not run* — a skip locally, a loud failure in continuous
integration. A disclosure failure can now only come from an assertion against
bytes the test actually received.

**Why not just remember.** Because the memory is the defect. The distinction was
made mechanical rather than left to whoever reads the failure next.

**Proved three ways**, against stub servers: no server at all reports
unreachable; a server answering 500 to everything reports unreachable and
names the stale-server cause; and a server returning a normal page *with a
screening match in the body* still fails properly, naming the leaked terms. The
third is the one that matters — a guard that stops failing is not an
improvement.

**What it does not guarantee.** It does not test the page's rendering, only its
response. And a page that returns a normal, empty response would be read as
"nothing disclosed" — which is why the suite also asserts the applicant's own
history still appears, so a blank page cannot satisfy it.

**The pattern, written down.** The same commit records a defect class this
project has now hit three times — a loader whose side effect decided whether 64
tests ran at all, a mutation-testing run that counted failures and ignored
errors, and documented score values never compared against computed ones. Common
shape: *something that looks like a check and is not one*, producing a
reassuring output without measuring anything. The remedy has two halves — make
the documentation executable, and make a check that could not run say so in
terms that cannot be mistaken for a pass or for a finding.

---

## 9. The rules-versions page

**Commit `62bfc60`**

**What changed.** A read-only staff page at `/desk/rulesets`, behind the same
login as the review desk, listing each version with its effective date, its
change note **in full**, its sourcing status, and how many decisions were made
under it. Selecting a version lists those decisions — applicant, outcome, who
decided, when, and the sanctions list version behind the case — each linking to
the existing case view.

**The mechanism.** Two database queries and some markup. No new tables and
nothing that writes, because a ruleset is corrected by recording a new version,
not by editing one.

**Two deliberate presentation choices.** Sourcing is a label on the face of each
row, not a tooltip: whether the reference data was ever sourced is the first
thing someone defending a decision needs, and a fact you have to hover to
discover is a fact the page is hiding. And decisions taken under a version with
unsourced reference data are marked on **each row**, not only in the banner
above them, because rows get filtered and screenshotted away from their heading.

**What it does not guarantee.** Rendering is untested — the web project has no
test runner — so a change to the page could break it without failing anything.
That is why the rules that matter are enforced in the database instead.

---

## 10. The FATF statement, and version `2026-10-1`

**Commit `6eafdbf`, migration `025_ruleset_2026_10_1.sql`**

**What changed.** The call-for-action list stopped being unsourced. A copy of the
FATF statement was placed in the repository and read from disk — nothing fetched,
nothing reconstructed — and a new ruleset version records it with full
provenance: source URL, publication date (19 June 2026), the plenary it came from
(17–19 June 2026), the date it was read, a SHA-256 hash of the file, and the
file's format.

**The comparison, which was the point.** The statement lists the DPRK, Iran and
Myanmar. The constant held exactly those three. **They matched.** The constant
being right was treated as a hypothesis to test, not a premise — which is the
only reason that sentence is worth anything.

**Why a new version when nothing scored differently.** The codes did not change,
so no applicant's score moves. What changed is what can be *proven*. The older
version's definition records that its call-for-action list was never fetched and
never cited, and that must stay true of the decisions taken under it. Editing
that row to say otherwise would be rewriting history to look better — the one
thing the append-only table exists to prevent. So: a new version, identical in
effect, different in what it can show about itself.

**What the hash proves, and what it does not.** A **SHA-256 hash** is a short
fingerprint of a file's exact bytes; two files with the same hash are the same
file. The file here is a *browser capture* of the statement printed to PDF — its
own footer carries the retrieval timestamp and its header the source URL. So the
hash proves "this is the artifact that was read". It does **not** prove "these
are FATF's canonical bytes": a capture carries navigation, a timestamp and
whatever markup the site served that minute, so two honest captures of the same
statement hash differently. The data records the format as
`pdf-of-saved-webpage` precisely so the field cannot be read as the stronger
claim. The publisher's own PDF would be stronger; this is what was available.

**The Myanmar flattening — a limitation recorded, not fixed.** The statement
draws two tiers. Counter-measures are called for on the DPRK and Iran. For
Myanmar it says, in terms, enhanced due diligence **"and not countermeasures"**.
Our scoring has one set and one number, so a Myanmar applicant attracts exactly
what a DPRK applicant attracts — 40 points.

This is recorded in the version's reference data (both tiers, plus a note stating
the limitation) and deliberately **not** fixed. Changing who scores what is a
policy decision; this was a provenance exercise, and mixing the two would make a
version mean two things at once. The limitation now travels with every decision,
where an auditor can find it, rather than living in a developer's head.

**A review date the publisher itself gave.** The statement says that if Myanmar
makes no further progress by October 2026, FATF will consider counter-measures.
That is recorded as `next_review_expected`, and it is the clearest available
argument for pinning a version onto each decision: this list may move within
weeks of being written down.

**Pinning, now actually exercised.** Until this migration, three versions existed
but every decision carried the oldest — so "a decision stays pinned when a newer
version arrives" had never been tested. A test now writes a decision under the
previous version, resolves it through the join an auditor would use, and asserts
it answers with the *previous* version and still reports its call-for-action list
as unsourced, which the newer version contradicts. A counterweight asserts the
newer version tells the newer story, so the first test cannot pass vacuously.

---

## 11. The one-time checksum exception

**Recorded in [decisions.md](decisions.md)**

**What happened.** The migration runner records a checksum — a fingerprint — of
every migration it applies, and refuses to run if a file on disk no longer
matches what was applied. Migration 007 states the rule: *"editing an applied
migration is how a database and a repository start silently disagreeing."*

Migrations 020–024 were edited after being applied — twice: once to put the
deployment ordering into their headers, once to replace a provenance text with a
blunter version. Their recorded checksums were refreshed rather than corrected by
adding a new migration.

**Why that was acceptable, specifically.** The commits had not been pushed, so no
other copy of the repository contained the files. No credentials for the deployed
database exist on that machine. Nothing applies migrations automatically to a
deployed database — the only automated runs are in continuous integration,
against throwaway containers. The only databases they had reached were local. So
there was no deployed database for the repository to disagree with, which is the
entire failure the checksum guards against.

**What does not follow.** Once a migration has been applied anywhere that is not
a developer's laptop, it is frozen, and a wrong comment is corrected by a new
migration — a worse-looking repository and a database nobody has to wonder
about. The checksum existing is what made this a deliberate exception rather
than unnoticed drift, which is the argument for having it.

**One consequence worth knowing.** The `rulesets` table is append-only by its own
triggers, so correcting that provenance text could not be done with an update
even locally; the table had to be dropped and the migration re-applied. After
deployment that option is gone, and a correction means recording a new version —
which is the cost the immutability is meant to impose, and the first real
evidence it works.

---

## How I would explain this demo to a Head of Compliance in five minutes

"A decision your team made six months ago has to be defensible today. That means
three things have to be recoverable: what the applicant looked like, what the
rules were, and what the lists said. Most systems keep the first and lose the
other two.

This one pins all three to the decision itself. Each decision row stores the
score it was made on, the version of the scoring rules in force, and — through
the screening record — which sanctions list informed it. The rules versions are
in a table you can read, one row per version, with its points, its thresholds,
its reference lists and a note saying why it exists. That table cannot be edited;
corrections are made by adding a new version. So a decision from March still
resolves to March's rules even after the rules have moved twice, and I can show
you that working rather than assert it.

Three things I would point out before you ask. First, the database enforces
this, not the application code — the rules are triggers and constraints, so they
bind the Python service, the web app, and anyone with a SQL console equally. A
convention that only lives in code is one the next developer can forget.

Second, the system refuses to let an officer sign off a case when it knows it has
evidence it failed to record. It would rather stop than let someone decide in
ignorance.

Third, and most useful to you: I found an unsourced country list in my own
project. The FATF jurisdictions had been written from memory and never checked
against a source, while being presented as a legal reference, and every existing
decision was scored against it. I did not
quietly correct it. The affected decisions still say what they were actually
scored against, and the rules table now states in the clear that that version's
list was unsourced. The current version is read from the published statement,
with the file committed and hashed — and I will tell you exactly what that hash
proves and what it does not, because it is a saved web page rather than FATF's
own PDF.

That last part is the demonstration. Not that the system is correct, but that
when it was wrong it could say so afterwards, precisely, without rewriting
anything."
