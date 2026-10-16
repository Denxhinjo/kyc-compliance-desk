"""The continuous seeder: what it generates, and what it is forbidden to do.

trickle.py is deliberately thin. It invents synthetic people and posts them to
/api/seed/tick; it holds no database credential and executes no part of the
pipeline. So the tests split the same way:

  * the Python half is tested directly — the shape of what it generates, the
    arrival rate, and the fact that its names and reviewer notes come from
    seed.py rather than from a second set of lists
  * the TypeScript half's SAFETY PROPERTIES are asserted by reading the route's
    source, in the same spirit as the test that fails if the review desk's
    `for update` ever becomes `for update skip locked`. Those properties — only
    seeded applications, only cases older than 48 hours, a clearly synthetic
    reviewer, never 'system' — are the ones whose quiet removal would be
    hardest to notice and most damaging: a bot deciding a visitor's application
    would be indistinguishable from the demo working.
"""

from __future__ import annotations

import pathlib
import random
import re
import statistics

import pytest

import trickle
from seed import APPROVE_REASONS, FOLLOW_UPS, REJECT_REASONS

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
ROUTE = REPO_ROOT / "web" / "src" / "app" / "api" / "seed" / "tick" / "route.ts"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "trickle.yml"


# ---------------------------------------------------------------------------
# What it generates
# ---------------------------------------------------------------------------

#: The field names the public form's validator reads. The route builds a
#: FormData from the generated dict and runs validateApplicant on it, so a
#: mismatch here means every applicant is silently refused.
FORM_FIELDS = {
    "fullName",
    "dateOfBirth",
    "addressLine1",
    "addressLine2",
    "addressCity",
    "addressPostcode",
    "addressCountry",
}


def test_generated_applicants_use_the_forms_field_names():
    """The route validates with validateApplicant, which reads these names.

    If this drifts, /api/seed/tick refuses every applicant and reports them in
    `rejected_input` — which trickle.py turns into a non-zero exit rather than
    a quiet success, so the workflow goes red. This test is the cheaper way to
    find out.
    """
    generated = trickle.generate(3, random.Random(1))
    assert len(generated) == 3
    for applicant in generated:
        assert set(applicant) == FORM_FIELDS


def test_generated_applicants_would_pass_the_forms_own_rules():
    """The constraints the validator and the database both enforce.

    Not a reimplementation of validateApplicant — it checks the handful of
    things that would make an applicant impossible rather than merely odd: a
    blank name, a malformed date, a country code that is not two letters.
    """
    import re
    from datetime import date

    for applicant in trickle.generate(25, random.Random(2)):
        assert applicant["fullName"].strip(), "blank names violate a CHECK"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", applicant["dateOfBirth"])
        assert re.fullmatch(r"[A-Z]{2}", applicant["addressCountry"])
        assert applicant["addressLine1"].strip()
        assert applicant["addressCity"].strip()
        assert applicant["addressPostcode"].strip()
        # The form requires an adult, and the database requires a plausible
        # date of birth (>= 1900-01-01).
        born = date.fromisoformat(applicant["dateOfBirth"])
        assert born >= date(1900, 1, 1)
        age = (date.today() - born).days / 365.25
        assert 18 <= age <= 120, f"{applicant['fullName']} is {age:.0f}"


def test_no_real_names_are_introduced():
    """NAMES COME FROM seed.py'S CULTURE LISTS, NOT THE SANCTIONS LIST.

    `make_applicant` will draw an applicant's name from a listed entity when it
    is given entries to draw from — that is how the bulk seeder makes some
    applicants deliberately resemble somebody listed. trickle.py passes an
    empty list precisely so it cannot, because adding real people's names to
    the data was out of scope for this round.

    Asserted by reading the call rather than by inspecting output, since the
    absence of a listed name in one sample proves nothing.
    """
    source = (REPO_ROOT / "worker" / "trickle.py").read_text(encoding="utf-8")
    assert "make_applicant(rng, today, [], None)" in source, (
        "trickle.generate must call make_applicant with NO listed entries, or "
        "it will start drawing applicant names from the sanctions list"
    )


def test_the_synthetic_sources_are_imported_not_copied():
    """One set of name lists and reviewer notes, in seed.py.

    A second copy is a second thing to drift, and the instruction was to reuse
    the existing synthetic sources rather than add new ones.
    """
    source = (REPO_ROOT / "worker" / "trickle.py").read_text(encoding="utf-8")
    assert "from seed import" in source
    for name in ("APPROVE_REASONS", "REJECT_REASONS", "FOLLOW_UPS", "make_applicant"):
        assert name in source, f"{name} should come from seed.py"


# ---------------------------------------------------------------------------
# The arrival rate
# ---------------------------------------------------------------------------


def test_the_weights_are_a_distribution():
    total = sum(trickle.PER_TICK_WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9, f"weights sum to {total}"
    assert min(trickle.PER_TICK_WEIGHTS) == 0, "a quiet hour has to be possible"


def test_hourly_ticks_land_in_the_requested_daily_band():
    """20 to 40 applicants a day, which is what was asked for.

    Computed from the weights rather than sampled, so it cannot flake.
    """
    mean_per_tick = sum(
        count * weight for count, weight in trickle.PER_TICK_WEIGHTS.items()
    )
    per_day = mean_per_tick * 24
    assert 20 <= per_day <= 40, f"{per_day:.1f} a day is outside 20-40"


def test_a_tick_never_exceeds_what_the_route_accepts():
    """The route refuses more than MAX_APPLICANTS_PER_TICK with a 400.

    Two constants in two languages that must agree. If they diverge, the
    workflow starts failing on the ticks that happen to be large.
    """
    route = ROUTE.read_text(encoding="utf-8")
    import re

    match = re.search(r"MAX_APPLICANTS_PER_TICK\s*=\s*(\d+)", route)
    assert match, "could not find MAX_APPLICANTS_PER_TICK in the route"
    assert max(trickle.PER_TICK_WEIGHTS) <= int(match.group(1))
    assert trickle.MAX_PER_TICK == int(match.group(1)), (
        f"trickle.py caps at {trickle.MAX_PER_TICK}, the route at {match.group(1)}"
    )


def test_the_observed_rate_matches_the_computed_one():
    """A sanity check on rng.choices, over a long enough run to be stable."""
    rng = random.Random(99)
    days = [
        sum(trickle.applicants_for_this_tick(rng) for _ in range(24)) for _ in range(300)
    ]
    assert 20 <= statistics.mean(days) <= 40, statistics.mean(days)
    assert min(days) >= 0


# ---------------------------------------------------------------------------
# Reviewer notes
# ---------------------------------------------------------------------------


def test_reviewer_notes_come_from_the_seeders_lists():
    rng = random.Random(5)
    for approve in (True, False):
        pool = APPROVE_REASONS if approve else REJECT_REASONS
        for note in trickle.reviewer_notes(rng, approve=approve):
            head = next((p for p in pool if note.startswith(p)), None)
            assert head is not None, f"note not from seed.py's lists: {note!r}"
            tail = note[len(head) :].strip()
            assert tail == "" or tail in FOLLOW_UPS, f"unexpected tail: {tail!r}"


def test_reviewer_notes_clear_the_desks_own_minimum():
    """The review desk requires 15 characters of a human. The bot is held to it.

    The route filters anything shorter, so a too-short note would silently fall
    back to a generic sentence rather than failing — which is exactly the kind
    of quiet degradation worth a test.
    """
    rng = random.Random(6)
    for approve in (True, False):
        for note in trickle.reviewer_notes(rng, approve=approve):
            assert len(note.strip()) >= 15, note


def test_approve_and_reject_notes_are_kept_apart():
    """An approval note on a refusal is worse than a generic one.

    `decisions.reason` is what an auditor reads to judge whether a decision was
    reasonable. "Cleared, not the same person" under a refusal reads as a
    contradiction in the case file.
    """
    rng = random.Random(7)
    approvals = set(trickle.reviewer_notes(rng, approve=True))
    rejections = set(trickle.reviewer_notes(rng, approve=False))
    for note in approvals:
        assert not any(note.startswith(r) for r in REJECT_REASONS)
    for note in rejections:
        assert not any(note.startswith(a) for a in APPROVE_REASONS)


# ---------------------------------------------------------------------------
# The route's safety properties, read from its source
# ---------------------------------------------------------------------------


def test_the_reviewer_only_ever_decides_seeded_applications():
    """THE MOST IMPORTANT ASSERTION IN THIS FILE.

    A bot deciding an application a visitor created would be invisible — the
    demo would look like it was working — and it would put a decision in
    someone's case file that no person made. The route restricts itself by
    reading each application's own `application.created` audit row, which
    cannot be rewritten, rather than a column that could be.
    """
    route = ROUTE.read_text(encoding="utf-8")
    assert "e.action = 'application.created'" in route
    assert "e.details->>'seeded' = 'true'" in route
    assert "and exists (" in route, (
        "the seeded restriction must be a required EXISTS, not an optional hint"
    )


def test_the_reviewer_leaves_recent_cases_for_visitors():
    """A queue containing only cases the bot has taken is a worse demo.

    48 hours of arrivals stay untouched, so a visitor always has something to
    review.
    """
    route = ROUTE.read_text(encoding="utf-8")
    import re

    match = re.search(r"REVIEW_AFTER_HOURS\s*=\s*(\d+)", route)
    assert match, "could not find REVIEW_AFTER_HOURS"
    assert int(match.group(1)) >= 48
    assert "r.decided_at < now() - make_interval(hours => $1)" in route, (
        "the age filter must be in the query, not applied after fetching"
    )


def test_the_reviewer_is_clearly_synthetic_and_is_not_the_system():
    """Never 'system', and unmistakably not a person.

    `decided_by = 'system'` is what the stats page counts as auto-decided, so a
    bot using it would silently inflate the automatic-decision figure with
    decisions that were not automatic. And `.invalid` is reserved by RFC 2606:
    it can never be a real domain, so the account cannot be mistaken for a
    colleague.
    """
    route = ROUTE.read_text(encoding="utf-8")
    import re

    match = re.search(r'SYNTHETIC_REVIEWER\s*=\s*"([^"]+)"', route)
    assert match, "could not find SYNTHETIC_REVIEWER"
    reviewer = match.group(1)
    assert reviewer.startswith("staff:"), reviewer
    assert reviewer != "system" and not reviewer.startswith("system"), reviewer
    assert "synthetic" in reviewer, reviewer
    assert reviewer.endswith(".invalid"), (
        f"{reviewer} should use a reserved TLD that can never resolve"
    )


def test_the_reviewer_uses_the_desks_own_decision_code():
    """Every check the desk applies must apply to the bot too.

    The lock, the already-decided re-read, migration 023's parked-evidence
    refusal and migration 007's unique-index backstop all live in
    lib/decisions.ts. A second implementation for seeded data would have
    exempted it from the guarantees the desk exists to provide — unreviewed,
    and running every hour.
    """
    route = ROUTE.read_text(encoding="utf-8")
    assert 'from "@/lib/decisions"' in route
    assert "recordDecision(" in route
    assert "insert into decisions" not in route, (
        "the route must not write decisions itself"
    )


def test_seeded_applicants_go_through_the_real_pipeline():
    """Creation, verification and the vendor result, all through shared code.

    The bulk seeder wrote `vendor_applicant_id` directly with no matching
    simulator session, which is why 38 applications pointed at sessions that
    had never existed and could never be resolved. This asserts the route
    cannot repeat that: it calls the same functions the apply form and the
    verify page call, and the webhook is delivered by the simulator rather than
    forged.
    """
    route = ROUTE.read_text(encoding="utf-8")
    assert 'from "@/lib/applications"' in route and "insertApplication(" in route
    assert 'from "@/lib/verification"' in route and "startVerificationSession(" in route
    assert 'from "@/lib/mock-vendor"' in route and "completeSession(" in route

    # Comments stripped before asserting what the route does NOT do: the file
    # explains the 38-stuck-applications defect in prose, and matching that
    # prose is not the same as matching code.
    code = re.sub(r"/\*.*?\*/", "", route, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)

    assert "insert into applications" not in code, (
        "the route must not insert applications itself"
    )
    assert "vendor_applicant_id" not in code, (
        "a session id written directly is the defect that caused the 38 stuck "
        "applications"
    )


def test_the_route_refuses_without_the_shared_secret():
    """An endpoint that creates data must be closed, and unset must refuse."""
    route = ROUTE.read_text(encoding="utf-8")
    assert "timingSafeEqual" in route, "compare the secret in constant time"
    assert 'status: 401' in route
    assert "refusing every seed request" in route, (
        "an unset secret must refuse everything rather than allow everything"
    )


def test_the_route_marks_only_what_it_originates():
    """The route sets the mark ONCE, at creation, and nowhere else.

    This replaced an assertion that the route said `seeded: true` three times —
    once for creation, once for verification, once for the decision. That was
    true of the first design and was exactly the weakness in it: three callers
    each had to remember, and the one that mattered most, the review desk, was
    not in this file at all and did not.

    The mark is now READ from the application's creation event by
    lib/verification.ts and lib/decisions.ts, so those two sites cannot forget
    it and neither can any site added later. What remains here is the single
    origin — `insertApplication`, which writes the creation event itself and has
    no earlier event to read from.

    test_seeded_propagation.py::test_every_web_write_site_consults_the_marker
    is what now covers the rest.
    """
    route = ROUTE.read_text(encoding="utf-8")
    assert route.count("seeded: true") == 1, (
        "the route should set the mark exactly once, at creation; the other "
        f"write paths read it. Found {route.count('seeded: true')}"
    )
    assert "insertApplication(client, checked.value, { seeded: true })" in route, (
        "the one occurrence should be the creation call"
    )


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------


def test_the_workflow_runs_hourly_and_offset_from_the_drain():
    """Hourly, and not in the same minute as drain.yml.

    Not a correctness requirement — the queue is safe under concurrency — but
    two unrelated sets of log lines interleaving makes both harder to read, and
    ten minutes after the drain means this tick's screening jobs are picked up
    promptly rather than an hour later.
    """
    import re

    trickle_yml = WORKFLOW.read_text(encoding="utf-8")
    drain_yml = (REPO_ROOT / ".github" / "workflows" / "drain.yml").read_text(
        encoding="utf-8"
    )
    mine = re.findall(r'cron:\s*"([^"]+)"', trickle_yml)
    theirs = re.findall(r'cron:\s*"([^"]+)"', drain_yml)
    assert mine == ["10 * * * *"], mine
    assert theirs == ["0 * * * *"], theirs
    assert mine != theirs


def test_the_workflow_installs_what_trickle_actually_imports():
    """trickle.py is stdlib-only; its IMPORT GRAPH is not.

    It imports seed.py for the synthetic name sources, and seed.py imports
    psycopg at module scope. The workflow originally asserted that no
    dependency install was needed -- true of trickle.py, false of what it
    pulls in -- and the first real run died on ModuleNotFoundError.

    The local tests did not catch it because this virtualenv has psycopg
    installed, so the import succeeded here and nowhere else. This asserts the
    property that actually matters: whatever trickle.py reaches must be
    installed by the job that runs it.
    """
    source = (REPO_ROOT / "worker" / "trickle.py").read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    # If it imports from the worker package at all, it inherits that package's
    # dependencies and the workflow has to install them.
    imports_worker_code = "from seed import" in source or "import seed" in source
    if imports_worker_code:
        assert "pip install -r worker/requirements.txt" in workflow, (
            "trickle.py imports seed.py, which imports psycopg at module "
            "scope, so the workflow must install worker/requirements.txt"
        )


def test_the_workflow_fails_closed_on_missing_configuration():
    """No invented defaults. Unset means a red run, not a guess at a url."""
    trickle_yml = WORKFLOW.read_text(encoding="utf-8")
    assert "PUBLIC_BASE_URL is set neither as a repository variable nor" in trickle_yml
    assert "exit 1" in trickle_yml
    assert "DRAIN_SECRET" in trickle_yml


def test_trickle_py_also_refuses_rather_than_guessing(monkeypatch, capsys):
    """Belt and braces: the script checks too, not only the workflow.

    It can be run by hand, and a hand-run with nothing configured should say so
    rather than post to a default host.
    """
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("DRAIN_SECRET", raising=False)
    monkeypatch.setattr("sys.argv", ["trickle.py", "--count", "0"])
    assert trickle.main() == 1
    assert "not set" in capsys.readouterr().err
