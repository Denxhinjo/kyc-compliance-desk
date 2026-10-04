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

## `CALL_FOR_ACTION` is unsourced

**What it is.** `IR`, `KP`, `MM` in `worker/scoring.py`. The codes are believed
current but were never fetched from a FATF statement or cited to a plenary. The
data says so: `sourcing: "unsourced"`, null publication date, and the reason
(HTTP 403 when the statement was attempted) recorded in its provenance.

**Why it matters.** Ruleset `2026-09-2` uses it. The rule this project has
adopted is sourced-and-cited or not used, and this is neither — the one
remaining instance of the defect that produced the fabricated country list.

**Blocked on.** A copy of the statement being placed in the repo. The fix is a
new ruleset version carrying the file hash and provenance, never an edit to
`2026-09-2`.

---

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
