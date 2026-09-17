"""Tests for name normalisation and fuzzy matching.

These are regression tests for a judgement call. The numbers below are the
measured basis for where the bands were drawn, so if a change to normalisation
or to the scorer moves them, the threshold argument in docs/decisions.md is no
longer supported by the code and someone has to look again.
"""

from __future__ import annotations

import pytest

from scoring import CONFIRMED, PROBABLE, WEAK, band_of
from screening.index import ListEntry, compare, build_index
from screening.normalise import name_tokens, normalise_name


# ---------------------------------------------------------------------------
# Normalisation — the unglamorous part that moved the numbers most
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("José Muñoz", "jose munoz"),
        ("Tomáš Novák", "tomas novak"),
        ("O'Brien", "o brien"),
        ("Al-Sayed", "al sayed"),
        ("  Ahmed   Hassan  ", "ahmed hassan"),
        ("Dr. Ahmed Hassan Jr", "ahmed hassan"),
        ("MR VLADIMIR PUTIN", "vladimir putin"),
    ],
)
def test_normalisation(raw, expected):
    assert normalise_name(raw) == expected


def test_punctuation_becomes_a_space_not_nothing():
    """"Al-Sayed" must become two tokens, not one.

    Stripping the hyphen outright would produce "alsayed", which is LESS similar
    to "Al Sayed" than the original was. The fix has to split, not delete.
    """
    assert normalise_name("Al-Sayed") == "al sayed"
    assert name_tokens("Al-Sayed") == {"al", "sayed"}


def test_normalisation_is_what_rescues_diacritics():
    """The single largest improvement in the whole matching pipeline."""
    without = compare("Jose Munoz", "Jose Munoz")
    with_accents = compare("Jose Munoz", "José Muñoz")
    assert with_accents == without == 100.0


# ---------------------------------------------------------------------------
# The measured cases behind the bands
# ---------------------------------------------------------------------------

#: (should_match, applicant, list entry). The scores these produce are what the
#: bands in scoring.py were chosen against.
CASES = [
    (True, "Ahmed Hassan", "Ahmad Hasan"),
    (True, "Sergey Ivanov", "Sergei Ivanoff"),
    (True, "Xi Jinping", "Jinping Xi"),
    (True, "Vladimir Putin", "Vladimir Vladimirovich Putin"),
    (True, "Jose Munoz", "José Muñoz"),
    (True, "John Smith", "Jon Smith"),
    (True, "O'Brien Patrick", "Patrick OBrien"),
    (False, "Ahmed Hassan", "Ahmed Hussein"),
    (False, "Ahmed Hassan", "Fatima Hassan"),
    (False, "John Smith", "Jane Smith"),
    (False, "Elena Vasquez", "Elena Rodriguez"),
    (False, "Tomas Novak", "Vladimir Putin"),
]


@pytest.mark.parametrize("applicant,listed", [(a, b) for ok, a, b in CASES if ok])
def test_true_matches_clear_the_noise_floor(applicant, listed):
    """Every genuine match must at least be RECORDED, even if weakly.

    Not "must be confirmed" — some transliterations cannot be. But an officer
    must get the chance to see it, so nothing true may fall below the floor.
    """
    assert compare(applicant, listed) >= WEAK


@pytest.mark.parametrize("applicant,listed", [(a, b) for ok, a, b in CASES if not ok])
def test_different_people_never_reach_the_probable_band(applicant, listed):
    """The invariant that actually holds — and note what it does NOT say.

    It does not say different people score below the noise floor. They do not:
    "John Smith" against "Jane Smith" scores exactly 80.0 and WILL be recorded
    as a weak match. That is not a bug to be tuned away, it is the measured
    reality that the bands were designed around.

    What must hold is that a pair of genuinely different people never reaches
    'probable', because that is the band where points start to matter. Weak
    matches are recorded so an officer can see them, contribute 20 points, and
    can never decide a case alone.
    """
    score = compare(applicant, listed)
    assert score < PROBABLE, f"{applicant!r} vs {listed!r} scored {score}"


def test_name_order_is_handled():
    """Chinese and Hungarian put the family name first; token_set_ratio is why
    this works and plain ratio is why it would not."""
    assert compare("Xi Jinping", "Jinping Xi") == 100.0


def test_patronymics_do_not_break_a_match():
    assert compare("Vladimir Putin", "Vladimir Vladimirovich Putin") == 100.0


def test_the_bands_overlap_and_that_is_the_whole_problem():
    """The finding that shaped this phase, pinned so it cannot be forgotten.

    A true match and two false positives score IDENTICALLY. No threshold
    separates them, because the information needed to separate them is not in
    the strings. This is why the ambiguous range contributes points without
    ever deciding alone — it routes to a human instead.
    """
    true_match = compare("Mohammed Al-Sayed", "Muhammad Al Sayyid")
    false_positive_one = compare("John Smith", "Jane Smith")
    false_positive_two = compare("Ahmed Hassan", "Ahmed Hussein")

    assert band_of(true_match) == "weak"
    assert band_of(false_positive_one) == "weak"
    assert band_of(false_positive_two) == "weak"
    assert abs(true_match - false_positive_one) < 2.0


def test_a_transliteration_pair_no_threshold_can_resolve_is_fixed_by_aliases():
    """What actually solves the hard case: the list's own spellings.

    "Mohammed Al-Sayed" against "Muhammad Al Sayyid" scores 80 — weak, and
    rightly so. But if the list publishes both spellings for the same entity,
    the match becomes exact and the threshold never has to adjudicate. This is
    the strongest practical argument for using real list data, which ships
    aliases, over anything hand-rolled.
    """
    assert band_of(compare("Mohammed Al-Sayed", "Muhammad Al Sayyid")) == "weak"

    index = build_index(
        [
            ListEntry(
                entity_id="E1",
                name="Muhammad Al Sayyid",
                aliases=("Mohammed Al-Sayed",),
                list_name="TEST",
            )
        ],
        source="test",
    )
    matches = index.search("Mohammed Al-Sayed")
    assert len(matches) == 1
    assert matches[0].score == 100.0
    assert band_of(matches[0].score) == "confirmed"


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


def small_index():
    return build_index(
        [
            ListEntry("E1", "Ahmad Hasan", ("A. Hasan",), "L", "sanctions",
                      ("EG",), "1975-04-12"),
            ListEntry("E2", "Tomas Novak", (), "L", "sanctions", ("CZ",), "1944-01-01"),
            ListEntry("E3", "Elena Vasquez Moreno", ("Elena Vazquez",), "L", "pep",
                      ("ES",), "1992-07-21"),
        ],
        source="test",
    )


def test_an_entity_appears_once_however_many_of_its_aliases_match():
    index = build_index(
        [ListEntry("E1", "Ahmad Hasan", ("Ahmad Hassan", "Ahmed Hasan"), "L")],
        source="test",
    )
    matches = index.search("Ahmed Hassan")
    assert len(matches) == 1
    assert matches[0].entry.entity_id == "E1"


def test_the_matched_spelling_is_reported_not_just_the_primary_name():
    """An officer comparing names needs to see WHICH spelling matched."""
    index = build_index(
        [ListEntry("E1", "Muhammad Al Sayyid", ("Mohammed Al-Sayed",), "L")],
        source="test",
    )
    assert index.search("Mohammed Al-Sayed")[0].matched_name == "Mohammed Al-Sayed"


def test_results_come_back_strongest_first():
    index = build_index(
        [
            ListEntry("E1", "Ahmed Hassan", (), "L"),
            ListEntry("E2", "Ahmad Hasan", (), "L"),
        ],
        source="test",
    )
    scores = [m.score for m in index.search("Ahmed Hassan")]
    assert scores == sorted(scores, reverse=True)


def test_a_conflicting_date_of_birth_is_flagged():
    """The common-name defence. Tomas Novak is a very ordinary Czech name, and
    a forty-year gap is what separates two people who share it."""
    matches = small_index().search("Tomas Novak", date_of_birth="1983-01-09")
    assert len(matches) == 1
    assert matches[0].score == 100.0
    assert matches[0].date_of_birth_conflict is True


def test_an_agreeing_date_of_birth_is_not_flagged_as_a_conflict():
    matches = small_index().search("Elena Vasquez", date_of_birth="1992-07-21")
    assert matches[0].date_of_birth_conflict is False


def test_a_missing_date_of_birth_never_counts_as_a_conflict():
    """Absence of evidence is not evidence of a different person."""
    matches = small_index().search("Tomas Novak", date_of_birth=None)
    assert matches[0].date_of_birth_conflict is False


def test_partial_dates_are_compared_by_year():
    index = build_index(
        [ListEntry("E1", "Viktor Kozlov", (), "L", "sanctions", (), "1971")],
        source="test",
    )
    assert index.search("Viktor Kozlov", date_of_birth="1971-06-02")[0].date_of_birth_conflict is False
    assert index.search("Viktor Kozlov", date_of_birth="1980-06-02")[0].date_of_birth_conflict is True


def test_a_shared_token_is_required():
    """Similarity scorers will return 60-something for names with nothing in
    common. On a twenty-thousand-entry list that noise dominates everything."""
    assert small_index().search("Wolfgang Schmidt") == []


def test_an_empty_name_matches_nothing_rather_than_everything():
    assert small_index().search("") == []
    assert small_index().search("   ") == []


def test_the_bands_are_ordered():
    assert CONFIRMED > PROBABLE > WEAK
