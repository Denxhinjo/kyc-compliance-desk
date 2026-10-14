# Fragilities

Things that are known to be brittle, known to be missing, or known to mislead —
carried deliberately rather than fixed, so that whoever meets one next does not
spend an afternoon rediscovering it.

Not a bug list. A bug gets fixed. These are either costed-and-declined, blocked
on something outside the repo, or a limitation of a decision taken on purpose.

Format: what it is, how it shows up, and what to do about it if it bites.

> Note: asked to match the reconciliation project's format, I could not — that
> repository is not available from here. This structure is a guess. Reshape it
> to match and this note goes away.

---

## An orphaned `next dev` keeps port 3001 and serves 500s

**How it shows up.** The disclosure tests fail in a way that looks exactly like
a status-page regression. The web app answers on 3001, but every page returns
HTTP 500 with `Jest worker encountered 2 child process exceptions` in the dev
server log, hitting `/status/` and `/desk/` alike.

**Why.** Stopping the background task kills the shell, not the `next dev` child
it spawned. The child keeps the port while its render workers are dead. A
restart then fails with `EADDRINUSE` and silently leaves the broken process
serving.

**What to do.** Kill whatever holds the port, then restart:

```powershell
Get-NetTCPConnection -LocalPort 3001 -State Listen |
  Select-Object -ExpandProperty OwningProcess -Unique |
  ForEach-Object { Stop-Process -Id $_ -Force }
```

**Partly mitigated.** The disclosure suite no longer reports this as a
disclosure failure — `fetch()` raises `Unreachable` for any 5xx and routes it to
"the check could not run". It still has to be cleared by hand.

---

## Scoring flattens the FATF call-for-action tiers

**What it is.** The 19 June 2026 statement draws two tiers: counter-measures
are called for on the DPRK and Iran, while for Myanmar it says enhanced due
diligence "and not countermeasures". `scoring.py` has one `CALL_FOR_ACTION` set
worth one number, so a Myanmar applicant scores exactly what a DPRK applicant
scores.

**Why it is carried.** Changing who scores what is a policy decision, and the
work that surfaced this was a provenance exercise. Mixing the two would make a
ruleset version mean two things at once.

**Mitigation in place.** The distinction is recorded in the data, not just
here: `CALL_FOR_ACTION.tiers` carries both groups and `flattening_note` states
the limitation, and both reach every decision through `reference_data`. An
auditor reading a decision can find it without reading this file.

**If you fix it.** It is a new ruleset version with a new points constant, not
an edit to `2026-10-1`.

---

## The call-for-action list may move in October 2026

**What it is.** The statement recorded in ruleset `2026-10-1` says that if
Myanmar makes no further progress by October 2026, the FATF will consider
counter-measures. `next_review_expected` on that version is `2026-10`.

**What to do.** When the October plenary publishes, obtain the statement, place
it in `data/fatf/`, and record a NEW ruleset version from the file — never an
edit to `2026-10-1`. The procedure is the one `025_ruleset_2026_10_1.sql`
documents.

## `/web` has no test runner

**What it is.** `package.json` has `typecheck` and some manual scripts. No
vitest, jest, playwright or cypress.

**What goes untested.** Server actions and page rendering. The compiler catches
a moved column; nothing catches a JSX branch that stops rendering. The
live-page fetches in `test_applicant_disclosure.py` run unauthenticated, so
they cannot reach anything behind `requireStaff()`.

**Mitigation in place.** Rules that matter are enforced in the database instead
— the ruleset-version CHECK, the risk freeze, the stuck-evidence guard. For the
officer's decide path the constraint *is* the test. That is a deliberate
trade, not an accident, but it means a page can break without failing anything.

---

## 13 of 22 monitored jurisdictions still cannot appear in the demo

**What it is.** The seeder's higher-risk tail is the sourced list intersected
with the countries `CITIES` has an address for. 9 of 22 monitored and 3 of 3
call-for-action are reachable.

**Why not just derive it.** Taking the list wholesale put applicants at a
`"Springfield", "00000"` placeholder — a more visible lie than the one being
fixed.

**Mitigation in place.** `seed.py` prints coverage every run and warns below one
third. It is visible rather than silent; it is not solved.

---

## An unscoped assertion in the pipeline tests

**What it is.** `worker/tests/test_integration_pipeline.py:392` asserts
`count == 1` over every queued `vendor.sweep_stuck` job, not scoped to the run.

**When it bites.** The moment a second test creates one. Same class as the
`demo.noop` bug that counted every execution ever recorded, which was fixed with
an audit watermark — this one has not been.

---

## Correcting a `rulesets` row after deployment requires a new version

**What it is.** `rulesets` is append-only by its own triggers, so a provenance
text cannot be corrected with an `UPDATE`. Locally the table can be dropped and
the migration re-applied; once deployed, that option is gone.

**Not a defect.** It is the cost the immutability is meant to impose, recorded
here so it is not met as a surprise mid-incident. A correction means recording a
new ruleset version.

---

## `/stats` answers 200 with a loading shell when the database is unreachable

**How it shows up.** The figures page looks healthy to anything that checks it
mechanically. `curl -o /dev/null -w '%{http_code}'` returns **200**, the HTML
arrives, and a human opening the page sees the heading, the layout and six
skeleton placeholders where the numbers should be — the same thing they would
see for the half-second before real figures arrive on a working page. Nothing in
the status code or the headers distinguishes "the database is down" from "the
database is fine".

Observed on production on 8 October 2026, while the rotated database credential
had not yet been applied to the web app. The page answered 200 throughout.

**Why.** `web/src/app/stats/` has a `loading.tsx`, which Next.js turns into an
automatic Suspense boundary for that route. The response therefore *begins* —
status line, headers, and the shell — before the Server Component's queries run.
By the time a query throws, the 200 has already been committed and sent. The
error surfaces mid-stream, where it degrades into a placeholder that never
resolves rather than into a status code anybody can act on.

This is visible in a healthy response too: fetch `/stats` and the HTML contains
both the skeleton markup and the real figures, because the shell is sent first
and the content streamed in behind it. On an unhealthy response only the first
half arrives.

**Why it is not fixed.** Fixing it properly means either querying before
committing the response — which gives up the streaming shell that makes the page
feel fast — or adding a separate health route that actually reaches the database
and returning a non-200 from it. The second is the right answer and is a small
piece of work, but it is a *new* check rather than a repair of this one, and
nothing currently consumes it.

**What to do if it bites.** Do not trust the status code of `/stats` as a
liveness signal, in a monitor or by hand. Check for a figure instead of a 200:

```bash
curl -s https://kyc-compliance-desk.vercel.app/stats | grep -qE '[0-9]+ of [0-9]+' \
  && echo "data resolved" || echo "SHELL ONLY - database unreachable"
```

**Worth naming.** This is another instance of the pattern recorded in
`decisions.md` as *things that look like a check and are not one* — alongside
the sanctions screen that returns fewer matches from a half-loaded list, the
drain endpoint that reported success whatever happened inside it
(`docs/incident-2026-10-03.md`), and the equivalence test that stayed red in CI
while its badge advertised it. The defect is always the same shape: a signal
that reports on the wrong thing and is read as though it reported on the right
one.

---

## The continuous seeder produces no sanctions-match referrals

**How it shows up.** Over time the review queue fills with cases referred for
country risk and declined document checks, and the proportion referred for a
sanctions name match falls. The oldest cases — from the bulk seed — have
sanctions matches; the new ones never do.

**Why.** `trickle.py` calls `make_applicant(rng, today, [], None)`. The empty
list is the list of sanctions entries to draw names from, and the bulk seeder
passes a real one so that some applicants deliberately resemble somebody
listed. The trickle passes nothing, because drawing from the live OFAC list
means putting real people's names into the applicant table and that was out of
scope for the round that built it.

So trickle applicants are generated from the culture name lists and will only
coincidentally resemble a listed entity. Referrals still happen — country risk
and a declined document check each score 40 — but the sanctions path does not
exercise.

**What to do if it matters.** Either accept it, or pass listed entries and
accept real names in the data. The second is what the bulk seeder already does,
so it is a decision about scope rather than a new category of risk. Loading the
list in the hourly job would also mean a 6 MB download every hour, so the
sensible version reads a handful of listed names from `sanctions_entries` in
the route instead.

---

## Two-character diacritic expansions do not link as the same person

**How it shows up.** `person_key` treats "Weiss" and "Weiß" as two different
people, likewise "Aegir"/"Ægir" and "Thorvaldur"/"Þorvaldur".

**Why.** The normalisation uses `translate()`, which maps one character to one
character. Eszett, the ae- and oe-ligatures, thorn and the ij-ligature all
expand to two ASCII letters and cannot go through it. Closing the gap means a
`regexp_replace` chain, one call per character, inside a function that has to
stay IMMUTABLE for the generated column.

**Why it is carried.** The failure direction is the safe one. The key is shown
to a reviewer as a statement of fact — "these are the same person" — with no
second check in front of it, so a missed link shows them less than it could
while a false link tells them something untrue. Three tests assert this gap
explicitly rather than leaving it to be discovered.

---

## `seed.py` can generate applicants the public form would refuse

**How it shows up.** It does not, in the bulk seeder. It would have shown up in
the continuous one as intermittently red workflow runs, roughly one tick in
twenty.

**Why.** `sample_date_of_birth` clamps the sampled age to a minimum of 18 and
then picks a day within that calendar year, so an applicant sampled at 18 is
born anywhere in it — and someone born late in that year is still 17 by the
whole-year rule the form uses. About a fifth of age-18 samples are under age on
the day they are generated.

This never mattered because `seed.py` writes applications straight to the
database and never meets `validateApplicant`. `trickle.py` goes through the
real form validation, which is the point of it, so the mismatch surfaces there.

**Mitigated, not fixed.** `trickle.adult_applicant` resamples until the
applicant is between 18 and 120 by the same whole-year rule the form applies,
and raises rather than looping forever if that becomes impossible. `seed.py`
itself is unchanged: it was out of scope, and nothing it does today is wrong
given that it bypasses validation.

**What to do if it bites elsewhere.** Anything else that feeds `seed.py`'s
output through the form's validator needs the same filter. The honest fix is in
`sample_date_of_birth` — clamp the resulting DATE to at least 18 years ago
rather than clamping the sampled age — which would make the generator correct
for every consumer at the cost of a slight change to the age distribution at
its lower bound.
