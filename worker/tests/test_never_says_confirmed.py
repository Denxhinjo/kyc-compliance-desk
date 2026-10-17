"""The system never outputs "confirmed".

"Confirmed match" and "false positive" are REVIEWER OUTCOMES. They are the
conclusion of a human comparing an applicant against a list entry, and a
string comparison is not entitled to either word. Until 2026-10-2 the scorer
emitted "confirmed" from name similarity alone -- with a scorer that handed
100% to any name appearing inside a longer one -- which told a reviewer the
opposite of the truth in the place they were most likely to believe it.

This asserts the absence, at the two levels it can be asserted:

  * no scored signal, over every band and every conflict combination, puts
    the word in its code, its reason or its evidence;
  * no source file emits it into anything a user reads.

History is exempt. Decisions taken under 2026-10-1 keep their stored reason
text verbatim -- it is the evidence they were taken on -- and the desk marks
those cases as using an older vocabulary rather than rewriting them.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from scoring import ApplicantProfile, ScreeningHit, score_application

BANNED = ("confirmed", "probable")

SCORES = [100.0, 99.9, 90.0, 80.0, 79.9, 50.0, 20.0, 0.1]
CONFLICTS = [
    (),
    ({"identifier": "date_of_birth", "outcome": "conflict", "detail": "d"},),
    ({"identifier": "suffix", "outcome": "conflict", "detail": "d"},),
    (
        {"identifier": "date_of_birth", "outcome": "agree", "detail": "d"},
        {"identifier": "nationality", "outcome": "not_available", "detail": "d"},
    ),
]


@pytest.mark.parametrize("match_type", ["sanctions", "pep", "adverse_media"])
@pytest.mark.parametrize("score", SCORES)
@pytest.mark.parametrize("comparisons", CONFLICTS, ids=range(len(CONFLICTS)))
def test_no_scored_signal_ever_says_confirmed(match_type, score, comparisons):
    hit = ScreeningHit(
        match_type=match_type,
        list_name="OFAC SDN",
        matched_name="Someone Listed",
        match_score=score,
        entity_id="E1",
        comparisons=comparisons,
    )
    assessment = score_application(
        ApplicantProfile(country="GB", vendor_status="Approved", hits=(hit,))
    )
    rendered = json.dumps(assessment.as_dict()).lower()
    for word in BANNED:
        assert word not in rendered, (
            f"{match_type} at {score} produced {word!r}: {rendered[:400]}"
        )


def test_a_scored_signal_says_the_strength_instead():
    """Not merely absent -- replaced by something a reviewer can act on."""
    hit = ScreeningHit(
        match_type="sanctions",
        list_name="OFAC SDN",
        matched_name="Marat Shakirzyanovich KHUSNULLIN",
        match_score=100.0,
        entity_id="E1",
        comparisons=(
            {"identifier": "date_of_birth", "outcome": "agree", "detail": "listed 1966"},
            {"identifier": "nationality", "outcome": "not_available", "detail": "none"},
        ),
    )
    assessment = score_application(
        ApplicantProfile(country="GB", vendor_status="Approved", hits=(hit,))
    )
    signal = assessment.signals[0]
    assert "exact" in signal.reason
    assert "date of birth: agree" in signal.reason
    assert "nationality: not available" in signal.reason
    assert signal.evidence["name_match_strength"] == "exact"


# ---------------------------------------------------------------------------
# Source-level
# ---------------------------------------------------------------------------

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

#: Files that legitimately contain the word: the migration recording what
#: changed, the code explaining why it changed, and the tests asserting it is
#: gone. Each is documentation of the reversal, not an output.
ALLOWED = {
    "db/migrations/030_ruleset_2026_10_2.sql",
    "db/migrations/025_ruleset_2026_10_1.sql",
    "db/migrations/024_rulesets.sql",
    "worker/scoring.py",
    "worker/screening/index.py",
    "worker/screening/identifiers.py",
    "worker/seed.py",
    "worker/lifecycle.py",
    "worker/sweeper.py",
    # These two TELL a reviewer that an older case uses an older vocabulary.
    # Naming the word is the entire point of the sentence; a case scored under
    # 2026-10-1 still reads "(confirmed)" in its frozen evidence, and leaving
    # that unexplained would be worse than mentioning it here.
    "web/src/app/desk/queue-table.tsx",
    "web/src/app/desk/[id]/page.tsx",
}


def _sources():
    for root, patterns in (("worker", ("*.py",)), ("web/src", ("*.ts", "*.tsx"))):
        base = REPO / root
        for pattern in patterns:
            for path in base.rglob(pattern):
                rel = path.relative_to(REPO).as_posix()
                if "node_modules" in rel or "/tests/" in rel or "/.venv/" in rel:
                    continue
                yield rel, path


def test_no_source_file_emits_confirmed_into_a_users_view():
    """The word may be explained; it may not be produced.

    Occurrences are allowed only in files that document the change. Anywhere
    else, a literal "confirmed" is almost certainly on its way to a screen.
    """
    offenders = []
    for rel, path in _sources():
        if rel in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("#", "//", "*", "/*")):
                continue
            if '"confirmed"' in line or "'confirmed'" in line:
                offenders.append(f"{rel}:{number}: {stripped[:90]}")
    assert offenders == [], "\n".join(offenders)


def test_the_allowlist_does_not_hide_a_file_that_stopped_existing():
    """An allowlist that names a deleted file quietly grows stale."""
    missing = [name for name in ALLOWED if not (REPO / name).exists()]
    assert missing == [], f"allowlisted files that no longer exist: {missing}"
