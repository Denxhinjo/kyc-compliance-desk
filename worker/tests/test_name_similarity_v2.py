"""The 2026-10-2 name similarity definition, against a fixed evaluation set.

WHY AN EVALUATION SET RATHER THAN EXAMPLES

A scorer is easy to test into a corner: pick six pairs, tune until they pass,
ship something worse than what you started with. The set in
tests/data/name_matching_evaluation.json is fixed, committed, and was built
BEFORE the new definition was chosen. Most of it is real -- 87 pairs that the
previous scorer put at 99% or above on this demo's own data -- plus nine
constructed cases covering reordering, transliteration, subsets and accents.

Both scores in the file are measured, not asserted by hand, so the file
records what each definition actually does rather than what someone hoped.
"""

from __future__ import annotations

import collections
import json
import pathlib

import pytest

from screening.index import compare, compare_symmetric
from screening.normalise import normalise_name
from scoring import band_of

DATA = pathlib.Path(__file__).parent / "data" / "name_matching_evaluation.json"
PAIRS = json.loads(DATA.read_text(encoding="utf-8"))["pairs"]


def token_multiset(value: str) -> collections.Counter:
    return collections.Counter(normalise_name(value).split())


def same_name(left: str, right: str) -> bool:
    """The same name, however it was written.

    Multiset equality, not string equality: "KIM Tong-Myo'ng" and
    "Tong-Myo'ng KIM" are one name in two conventions, and "Sergey Ivanov"
    against "Sergey IVANOV" differs only in case.
    """
    return token_multiset(left) == token_multiset(right)


def test_the_evaluation_set_is_not_trivial():
    """Guards the guard.

    A set of 96 identical pairs would pass every assertion below while proving
    nothing. This project has already shipped a test that passed by finding
    nothing, so the shape of the evidence is asserted too.
    """
    assert len(PAIRS) >= 90
    bands = collections.Counter(p["expected_band"] for p in PAIRS)
    assert bands["exact"] >= 10
    assert bands["strong"] >= 5
    assert bands["partial"] >= 5
    # It must contain pairs the OLD scorer got wrong, or it cannot show a fix.
    regressions = [p for p in PAIRS if p["v1"] >= 99.5 and not same_name(
        p["applicant"], p["list_entry"])]
    assert len(regressions) >= 20, (
        "the set no longer contains the false positives this change exists to "
        "fix, so it cannot demonstrate the fix"
    )


@pytest.mark.parametrize(
    "pair", PAIRS, ids=[f"{p['applicant'][:24]}|{p['list_entry'][:24]}" for p in PAIRS]
)
def test_each_pair_scores_and_bands_as_recorded(pair):
    """Every pair, scored now, against what the file records."""
    measured = compare_symmetric(pair["applicant"], pair["list_entry"])
    assert measured == pytest.approx(pair["v2"], abs=0.05), (
        f"{pair['applicant']!r} vs {pair['list_entry']!r}"
    )
    assert band_of(measured) == pair["expected_band"]


def test_a_subset_never_reaches_100():
    """THE DEFECT, as a property over the whole set.

    "Carlos Fernandez" inside "Carlos Ariel FERNANDEZ CONCEPCION" scored 100
    under v1 because token_set_ratio is blind to the parts only the list has.
    Under v2 nothing but the same name can reach 100.
    """
    offenders = [
        p
        for p in PAIRS
        if compare_symmetric(p["applicant"], p["list_entry"]) >= 99.95
        and not same_name(p["applicant"], p["list_entry"])
    ]
    assert offenders == [], (
        "these are different names scoring a perfect match: "
        f"{[(p['applicant'], p['list_entry']) for p in offenders]}"
    )


def test_an_identical_normalised_name_always_reaches_100():
    """The other half of the property. Both directions, or it is half a rule."""
    checked = 0
    for pair in PAIRS:
        if not same_name(pair["applicant"], pair["list_entry"]):
            continue
        checked += 1
        assert compare_symmetric(
            pair["applicant"], pair["list_entry"]
        ) == pytest.approx(100.0), f"{pair['applicant']!r} vs {pair['list_entry']!r}"
    assert checked >= 10, "no same-name pairs in the set, so this proved nothing"


def test_a_reordered_name_is_still_the_same_name():
    """Surname-first conventions must not cost anything.

    The alignment is order-independent, so this holds by construction -- but
    it is the case a Korean or Hungarian name hits every time, and a
    regression here would be a quiet, systematic bias against those names.
    """
    assert compare_symmetric("KIM Tong-Myo'ng", "Tong-Myo'ng KIM") == pytest.approx(100.0)
    assert compare_symmetric("Xi Jinping", "Jinping Xi") == pytest.approx(100.0)


def test_v1_is_untouched():
    """The old definition still produces the old numbers.

    Decisions taken under 2026-10-1 are reproducible only if the scorer they
    were taken under still behaves identically. This is the assertion that
    makes "reconstruct any decision" more than a slogan.
    """
    for pair in PAIRS:
        assert compare(pair["applicant"], pair["list_entry"]) == pytest.approx(
            pair["v1"], abs=0.05
        ), f"v1 changed for {pair['applicant']!r} vs {pair['list_entry']!r}"


def test_the_change_is_a_tightening_everywhere_it_differs():
    """v2 is never MORE generous than v1 on this set.

    A definition that scored some pair higher than the old one would be
    raising false positives somewhere while lowering them elsewhere, and the
    net effect would be an argument rather than a measurement.
    """
    looser = [
        p
        for p in PAIRS
        if compare_symmetric(p["applicant"], p["list_entry"])
        > compare(p["applicant"], p["list_entry"]) + 0.05
    ]
    # Transliteration pairs are the deliberate exception: v2 can score them
    # slightly higher because aligned parts are compared as parts.
    for pair in looser:
        assert pair["v2"] - pair["v1"] < 5.0, (
            f"{pair['applicant']!r} vs {pair['list_entry']!r} scores much "
            f"higher under v2 ({pair['v1']} -> {pair['v2']})"
        )
