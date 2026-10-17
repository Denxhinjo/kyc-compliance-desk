"""What `screening_results.match_score` holds, and why it is not the other one.

THE BUG THIS LOCKS OUT

Under 2026-10-2 a match has two numbers. The RETRIEVAL score decides whether a
candidate is worth recording at all and is computed by the old v1 scorer,
unchanged, so recall cannot regress. The NAME SCORE is the ruleset's own
measure, and it is what the band, the points and the reason text come from.

For "Carlos Fernandez" against "Carlos Ariel FERNANDEZ CONCEPCION" those are
100 and 67.

The officer sees `match_score` in a column headed STRENGTH, on the case page,
directly beneath a flag quoting the other number. Shipping the retrieval score
into that column put

    STRENGTH 100%

under

    Sanctions name match (partial, 67%)

on the same screen -- reinstating, in the evidence table, the exact figure this
version exists to stop showing, and inviting a reviewer to believe the one that
flatters the match. It reached production before anyone noticed, because every
test asserted on the signal and none on the row beside it.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from screening.identifiers import Comparison  # noqa: E402
from screening.index import ListEntry, Match  # noqa: E402
from screening_handler import _store_matches  # noqa: E402

ENTRY = ListEntry(
    entity_id="OFAC-TEST-1",
    name="Carlos Ariel FERNANDEZ CONCEPCION",
    list_name="OFAC SDN",
    entity_type="sanctions",
    dates_of_birth=("14 Jan 1973",),
)

MATCH = Match(
    entry=ENTRY,
    score=100.0,          # retrieval: the v1 scorer, unchanged
    matched_name="Carlos Ariel FERNANDEZ CONCEPCION",
    date_of_birth_conflict=True,
    name_score=66.67,     # the ruleset's own measure
    comparisons=(
        Comparison("date_of_birth", "conflict", "listed 14 Jan 1973"),
        Comparison("suffix", "not_available", "neither side gives one"),
    ),
)


@pytest.fixture()
def stored(db, make_application):
    application_id = make_application(status="screening", full_name="Carlos Fernandez")
    _store_matches(db, application_id, "ofac", [MATCH], snapshot_id=None)
    db.commit()
    with db.cursor() as cur:
        cur.execute(
            """
            select match_score, payload
              from screening_results
             where application_id = %s
            """,
            (application_id,),
        )
        row = cur.fetchone()
    assert row is not None, "nothing was stored"
    return row


def test_the_stored_score_is_the_rulesets_not_the_retrieval_score(stored):
    match_score, _ = stored
    assert float(match_score) == pytest.approx(66.67), (
        "screening_results.match_score is the number shown under STRENGTH on "
        "the case page. It must be the ruleset's own score, not the retrieval "
        "score, or the evidence table contradicts the flag above it."
    )
    assert float(match_score) != pytest.approx(100.0)


def test_the_retrieval_score_is_kept_in_the_payload(stored):
    """Not discarded: it answers "why was this candidate even considered?"."""
    _, payload = stored
    assert payload["retrieval_score"] == pytest.approx(100.0)


def test_the_identifier_comparisons_travel_with_the_evidence(stored):
    """The row carries what contradicted it, not just a number.

    A reviewer reading one match should not have to reconstruct the identifier
    comparison from the signal's prose.
    """
    _, payload = stored
    outcomes = {c["identifier"]: c["outcome"] for c in payload["identifier_comparisons"]}
    assert outcomes["date_of_birth"] == "conflict"
    assert outcomes["suffix"] == "not_available"


def test_every_listed_date_is_kept_not_just_the_first(stored):
    _, payload = stored
    assert payload["listed_dates_of_birth"] == ["14 Jan 1973"]


def test_the_two_scores_are_genuinely_different_in_this_fixture():
    """Guards the guard: equal numbers would make the assertions above vacuous."""
    assert MATCH.score != MATCH.name_score
    assert abs(MATCH.score - MATCH.name_score) > 10
