"""Comparing secondary identifiers: dates of birth, suffixes, nationality.

A name match is a potential match. These comparisons are what a reviewer uses
to decide whether it is the same person, so each rule here is a rule about
what the system is allowed to claim from the evidence it actually holds.
"""

from __future__ import annotations

import datetime as dt

import pytest

from screening.identifiers import (
    AGREE,
    CONFLICT,
    NOT_AVAILABLE,
    YEAR_AGREES,
    compare_dates_of_birth,
    compare_nationality,
    compare_suffix,
)
from screening.normalise import normalise_name, suffix_of

D = dt.date


# ---------------------------------------------------------------------------
# Dates of birth
# ---------------------------------------------------------------------------


def test_a_full_date_that_matches_agrees():
    assert compare_dates_of_birth(D(1948, 12, 10), ("10 Dec 1948",)).outcome == AGREE


def test_a_full_date_that_differs_conflicts():
    result = compare_dates_of_birth(D(1983, 4, 2), ("10 Dec 1948",))
    assert result.outcome == CONFLICT
    # The reviewer is told both values, not just the verdict.
    assert "10 Dec 1948" in result.detail
    assert "1983-04-02" in result.detail


def test_a_year_only_value_compares_at_year_level():
    assert compare_dates_of_birth(D(1975, 6, 1), ("1975",)).outcome == YEAR_AGREES
    assert compare_dates_of_birth(D(1976, 6, 1), ("1975",)).outcome == CONFLICT


def test_no_listed_date_is_not_available_rather_than_agreement():
    """Silence is not agreement.

    "The list does not say" and "they match" are different answers, and
    collapsing them would let an absence of evidence read as evidence.
    """
    assert compare_dates_of_birth(D(1975, 6, 1), ()).outcome == NOT_AVAILABLE
    assert compare_dates_of_birth(D(1975, 6, 1), None).outcome == NOT_AVAILABLE


def test_no_applicant_date_is_not_available():
    assert compare_dates_of_birth(None, ("10 Dec 1948",)).outcome == NOT_AVAILABLE


# --- multiple dates --------------------------------------------------------


def test_agreement_with_any_listed_date_is_agreement():
    """Hasan NASRALLAH publishes four dates. Matching one is matching."""
    listed = ("31 Aug 1960", "31 Aug 1953", "31 Aug 1955", "31 Aug 1958")
    assert compare_dates_of_birth(D(1955, 8, 31), listed).outcome == AGREE


def test_a_conflict_requires_disagreeing_with_every_listed_date():
    """The bug this replaces.

    Keeping only the first date let the system announce a conflict with a date
    the list itself also publishes, and weaken a genuine match on the strength
    of it.
    """
    listed = ("31 Aug 1960", "31 Aug 1953", "31 Aug 1955", "31 Aug 1958")
    assert compare_dates_of_birth(D(1990, 1, 1), listed).outcome == CONFLICT
    for year in (1960, 1953, 1955, 1958):
        assert compare_dates_of_birth(D(year, 8, 31), listed).outcome == AGREE


# --- imprecise values ------------------------------------------------------


def test_a_circa_value_can_never_conflict():
    """"circa 1951" cannot rule anybody out, so it never contradicts one."""
    assert compare_dates_of_birth(D(1990, 3, 4), ("circa 1951",)).outcome == NOT_AVAILABLE


@pytest.mark.parametrize(
    "listed",
    [
        "circa 1951",
        "1950 to 1955",
        "02 Jan 1960 to 05 Jan 1960",
        "Mar 1962",
        "Mar 1962 to Jun 1962",
    ],
)
def test_every_imprecise_shape_in_the_snapshot_blocks_a_conflict(listed):
    """All 260 non-standard values in the current snapshot look like one of
    these. A range or an approximation cannot rule somebody out, so none of
    them is allowed to produce `conflict`."""
    assert compare_dates_of_birth(D(1990, 3, 4), (listed,)).outcome != CONFLICT


def test_an_imprecise_value_blocks_a_conflict_even_beside_precise_ones():
    """The tightening, stated as its own case.

    An entry listing both "circa 1951" and "10 Dec 1948" cannot contradict an
    applicant born in 1990, even though the precise date disagrees: the
    approximate value is still unresolved, and a conflict asserts that every
    possibility has been ruled out.
    """
    result = compare_dates_of_birth(D(1990, 3, 4), ("10 Dec 1948", "circa 1951"))
    assert result.outcome == NOT_AVAILABLE
    assert "circa 1951" in result.detail


def test_an_imprecise_value_does_not_block_agreement():
    """Agreement needs only one date to line up, so it survives."""
    result = compare_dates_of_birth(D(1948, 12, 10), ("circa 1951", "10 Dec 1948"))
    assert result.outcome == AGREE


# ---------------------------------------------------------------------------
# Suffixes
# ---------------------------------------------------------------------------


def test_suffixes_are_no_longer_stripped_from_a_name():
    """They used to be discarded as noise, which made a father and his son an
    exact match for each other."""
    assert suffix_of("Sergey IVANOV JR.") == "jr"
    assert suffix_of("Sergey Ivanov Sr") == "sr"
    assert suffix_of("John Smith III") == "iii"
    assert suffix_of("Sergey Ivanov") is None
    # Honorifics ARE still noise and still go.
    assert suffix_of("Dr. Ayman AL ZAWAHIRI") is None
    assert normalise_name("Dr. Ayman AL ZAWAHIRI") == "ayman al zawahiri"


def test_the_name_itself_is_compared_without_the_suffix():
    """A name is the same name with or without one; the suffix is a separate
    identifier rather than part of the string being matched."""
    assert normalise_name("Sergey IVANOV JR.") == normalise_name("Sergey Ivanov")


def test_jr_against_sr_is_a_conflict():
    result = compare_suffix("jr", "sr")
    assert result.outcome == CONFLICT
    assert "Sr" in result.detail and "Jr" in result.detail


def test_the_same_suffix_on_both_sides_agrees():
    assert compare_suffix("jr", "jr").outcome == AGREE


def test_a_suffix_on_one_side_only_is_not_available():
    """Most records simply omit it, so its absence proves nothing either way."""
    assert compare_suffix(None, "jr").outcome == NOT_AVAILABLE
    assert compare_suffix("jr", None).outcome == NOT_AVAILABLE
    assert compare_suffix(None, None).outcome == NOT_AVAILABLE


# ---------------------------------------------------------------------------
# Nationality
# ---------------------------------------------------------------------------


def test_nationality_is_not_available_until_the_form_collects_it():
    """Release A ships the comparison; Release B collects the value.

    Showing "not available" is the point: an identifier the interface never
    mentions is one nobody asks for.
    """
    assert compare_nationality(None, ("Egypt",)).outcome == NOT_AVAILABLE


def test_nationality_agrees_and_conflicts_once_both_sides_have_one():
    assert compare_nationality("Egypt", ("Egypt",)).outcome == AGREE
    assert compare_nationality("Egypt", ("egypt",)).outcome == AGREE
    assert compare_nationality("France", ("Egypt", "Syria")).outcome == CONFLICT
    assert compare_nationality("Syria", ("Egypt", "Syria")).outcome == AGREE
