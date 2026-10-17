"""The ruleset in force selects the similarity definition. There is no flag.

WHY THIS MATTERS MORE THAN IT LOOKS

The obvious way to ship a scoring change is behind a feature flag: deploy
dark, turn it on, turn it off if it misbehaves. That would make the ruleset
version a lie. The same version would mean one thing on Tuesday and another on
Wednesday, and "this decision was taken under 2026-10-1" would no longer
identify the rules it was taken under -- which is the single claim this whole
system is built to support.

So the version IS the switch. A decision stamped 2026-10-1 can only ever carry
a v1 score, and one stamped 2026-10-2 only a v2 score, because the scorer is
selected by the version rather than alongside it.

2026-10-1's recorded row names no definition at all: `rulesets` is append-only
and cannot be amended to add the key retrospectively. An absent key therefore
MEANS v1, which is how that version keeps its meaning without its row being
rewritten.
"""

from __future__ import annotations

import pytest

from screening.index import (
    DEFAULT_SIMILARITY,
    SIMILARITY_DEFINITIONS,
    compare,
    compare_symmetric,
    similarity_for,
)
from scoring import NAME_SIMILARITY, RULESET_VERSION

#: Pairs where the two definitions disagree, so "which one ran" is observable.
DIVERGENT = [
    ("Carlos Fernandez", "Carlos Ariel FERNANDEZ CONCEPCION", 100.0, 66.7),
    ("Mohammed Nasser", "Abdal Nasser Sharif Mohammed Said JAMOOS", 100.0, 50.0),
    ("Hassan Al-Rashid", "Salim Hassan Khalifa Rashid AL KUWARI", 100.0, 66.7),
    ("John Smith", "Jane Smith", 80.0, 50.0),
]


def test_an_absent_definition_means_v1():
    """2026-10-1 names none, and must keep meaning what it meant."""
    assert similarity_for(None) is compare
    assert DEFAULT_SIMILARITY == "v1-max-token-set"
    assert SIMILARITY_DEFINITIONS[DEFAULT_SIMILARITY] is compare


def test_the_current_ruleset_names_v2():
    assert RULESET_VERSION == "2026-10-2"
    assert NAME_SIMILARITY == "v2-symmetric-coverage"
    assert similarity_for(NAME_SIMILARITY) is compare_symmetric


@pytest.mark.parametrize("applicant,listed,v1,v2", DIVERGENT)
def test_the_same_inputs_produce_each_rulesets_own_score(applicant, listed, v1, v2):
    """The assertion the whole arrangement exists for.

    One pair of names, scored under each ruleset, yields that ruleset's
    number. Not "the new number"; that ruleset's number.
    """
    under_2026_10_1 = similarity_for(None)
    under_2026_10_2 = similarity_for(NAME_SIMILARITY)

    assert under_2026_10_1(applicant, listed) == pytest.approx(v1, abs=0.05)
    assert under_2026_10_2(applicant, listed) == pytest.approx(v2, abs=0.05)
    assert under_2026_10_1(applicant, listed) != under_2026_10_2(applicant, listed)


def test_the_divergent_set_actually_diverges():
    """Guards the guard: identical scores would make the test above vacuous."""
    for applicant, listed, v1, v2 in DIVERGENT:
        assert abs(v1 - v2) > 10.0, f"{applicant!r} vs {listed!r} barely differs"


def test_an_unknown_definition_is_refused_loudly():
    """A ruleset naming a scorer that does not exist must not silently fall
    back to a default. Scoring under the wrong definition is exactly the
    failure this design prevents, so it fails closed."""
    with pytest.raises(ValueError) as excinfo:
        similarity_for("v3-does-not-exist")
    assert "unknown name similarity definition" in str(excinfo.value)


def test_every_named_definition_is_callable_and_bounded():
    for name, scorer in SIMILARITY_DEFINITIONS.items():
        value = scorer("Ivan Petrov", "Ivan Petrov")
        assert 0.0 <= value <= 100.0, name
        assert scorer("Ivan Petrov", "") == 0.0, name
