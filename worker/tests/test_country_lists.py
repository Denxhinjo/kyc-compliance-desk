"""The FATF lists are reference data, and reference data needs provenance.

WHY THIS FILE EXISTS

The sanctions list carries a source, a publication date, a record count, a
SHA-256 and a load timestamp, and every screening result points at the snapshot
it used. The country lists carried a comment.

The gap was not cosmetic. The increased-monitoring set in this repo was, until
2026-09-24, a set FATF never published: it held Monaco, grey-listed at the June
2024 plenary, beside Turkiye and the UAE, de-listed at that same plenary and in
February 2024 respectively. Nothing could have caught it, because nothing
recorded which plenary the codes were meant to have come from.

THE RULE THESE TESTS ENFORCE

FATF publishes each list as a COMPLETE SET at each plenary, about three times a
year. It is not amended incrementally: a jurisdiction's presence and its absence
are both statements as of one date. So the set has exactly one valid shape --
every code from the same plenary, and the date beside it that plenary's own.

A set mixing plenaries is wrong in both directions at once: it screens against
countries already cleared and misses countries added since. That is worse than
being out of date, because out of date is at least a coherent statement about a
known moment.

These tests do not check that the lists are CORRECT -- no test can, because
correctness lives at fatf-gafi.org and moves three times a year. They check that
the codes and the date they belong to cannot drift apart.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest

from scoring import (
    CALL_FOR_ACTION,
    COUNTRY_LISTING,
    INCREASED_MONITORING,
    ApplicantProfile,
    score_application,
)

#: The monitoring list as it stands: its codes and the plenary they came from,
#: pinned TOGETHER.
#:
#: Pinning them as a pair is the whole mechanism. Editing the set changes the
#: digest and fails this test, and the only way to make it pass is to come here
#: and state which plenary the new codes are from. An undated edit cannot be
#: committed -- which is exactly the defect that produced the fabricated list.
PINNED_MONITORING = (
    "c8e4e1edab7aec194195b15d30a56fd1c1fb0123c674095d1f3a5156000aea02",
    "2026-06-19",
)


def test_the_monitoring_list_and_its_plenary_date_cannot_drift_apart():
    actual = (INCREASED_MONITORING.digest, INCREASED_MONITORING.published_at)
    assert actual == PINNED_MONITORING, (
        "the increased-monitoring list changed.\n\n"
        "If that was deliberate it must be a WHOLESALE replacement from one "
        "FATF plenary statement, never a country-by-country amendment:\n"
        "  1. take the complete set from a single statement\n"
        "  2. set published_at to that statement own date\n"
        "  3. update retrieved_at, plenary and provenance\n"
        "  4. update PINNED_MONITORING here to the new pair\n\n"
        "A set assembled from two plenaries screens against countries already "
        "cleared AND misses countries added since."
    )


def test_the_monitoring_list_is_sourced_to_a_real_plenary_date():
    """Non-null, ISO-8601, not in the future, not a placeholder."""
    published = INCREASED_MONITORING.published_at
    assert published is not None, (
        "the monitoring list must name the plenary that published it; it is "
        "fetchable from one public page, and a disclaimer is not a substitute "
        "for closing a gap that cheap"
    )
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", published), (
        f"published_at is {published!r}; it must be a full ISO-8601 date, "
        "because it is stored on every decision and read back by machines. "
        "A month and a year is not a date."
    )
    parsed = dt.date.fromisoformat(published)
    assert parsed <= dt.date.today(), (
        f"published_at is {published}, in the future -- FATF cannot have "
        "published a revision that has not happened"
    )
    # Placeholders that parse. 1970-01-01 is what a default looks like when
    # somebody wires a field up meaning to fill it in later.
    assert parsed.year >= 2000, f"{published} is a placeholder, not a plenary"

    assert INCREASED_MONITORING.retrieved_at is not None
    assert parsed <= dt.date.fromisoformat(INCREASED_MONITORING.retrieved_at), (
        "retrieved_at is before published_at -- the codes cannot have been "
        "read from a statement that had not been published yet"
    )


@pytest.mark.parametrize(
    "revision",
    [INCREASED_MONITORING, CALL_FOR_ACTION],
    ids=["increased_monitoring", "call_for_action"],
)
def test_every_revision_names_its_source(revision):
    assert revision.source_url.startswith("https://www.fatf-gafi.org"), (
        f"source_url is {revision.source_url!r} -- reference data of this kind "
        "is cited to the publisher, not to a summary of it"
    )


@pytest.mark.parametrize(
    "revision",
    [INCREASED_MONITORING, CALL_FOR_ACTION],
    ids=["increased_monitoring", "call_for_action"],
)
def test_every_code_is_well_formed_and_carries_a_sourced_name(revision):
    """Shape, plus a name read off the same statement.

    Shape alone is weak: UK is two uppercase letters and is not an ISO-3166
    code (Great Britain is GB). Requiring a name for every code means a typo
    has to be made twice, consistently, in two places -- and the names were
    read from the statement rather than from memory.
    """
    malformed = sorted(c for c in revision.codes if not re.fullmatch(r"[A-Z]{2}", c))
    assert not malformed, (
        f"{malformed} are not ISO-3166-1 alpha-2 codes. These are compared "
        "against applications.address_country, so a malformed entry silently "
        "matches nobody and the jurisdiction scores zero."
    )
    assert set(revision.codes) == set(revision.names), (
        "codes and names disagree: "
        f"{sorted(set(revision.codes) ^ set(revision.names))}"
    )
    assert all(revision.names[code].strip() for code in revision.codes)


def test_the_two_lists_are_disjoint():
    """Different points, so an overlap would make scoring order-dependent.

    FATF would never publish one: a jurisdiction is subject to a call for
    action or to increased monitoring, not both. An overlap means a hand-edit
    went wrong.
    """
    overlap = COUNTRY_LISTING.call_for_action & COUNTRY_LISTING.increased_monitoring
    assert not overlap, (
        f"{sorted(overlap)} appear on both lists. Scoring tests call-for-action "
        "first, so these would score 40 and the monitoring branch would never "
        "run -- a silent precedence rather than a stated one."
    )


def test_an_unsourced_revision_still_says_so_in_the_data():
    """The null-date path, now load-bearing ONLY for call-for-action.

    IR/KP/MM were not re-fetched during the monitoring correction because the
    FATF call-for-action statement returned HTTP 403. Rather than borrow the
    monitoring list date to make them look sourced, that revision carries a
    null date and says why. The invariant is not that there must be a date --
    it is that if there is no date, the data must say so, in the data.
    """
    for revision in (INCREASED_MONITORING, CALL_FOR_ACTION):
        if revision.published_at is None:
            assert "UNSOURCED" in revision.provenance, (
                "a revision with no publication date must say plainly that it "
                "is not traceable to a published statement. A reader skimming "
                "a stored decision should not have to infer it from a null."
            )
            assert revision.plenary is None, (
                "a revision naming a plenary but carrying no date is the "
                "half-sourced state this exists to prevent"
            )


def test_the_provenance_of_both_lists_reaches_the_decision_record():
    """The point of the exercise: on the decision, not near it.

    An auditor reading one stored assessment must be able to answer which
    revision it was scored against, without reading the source at whichever
    commit produced it.
    """
    recorded = score_application(
        ApplicantProfile(country="IQ", vendor_status="Approved", hits=())
    ).as_dict()["country_list"]

    monitoring = recorded["increased_monitoring"]
    assert monitoring["published_at"] == "2026-06-19"
    assert monitoring["plenary"] == "June 2026"
    assert monitoring["digest"] == INCREASED_MONITORING.digest
    assert monitoring["source_url"].startswith("https://")
    assert recorded["call_for_action"]["digest"] == CALL_FOR_ACTION.digest


def test_it_is_recorded_even_when_no_country_signal_fires():
    """Consulted-and-not-listed is itself a finding.

    If provenance were attached only when a country scored, an auditor asking
    why a jurisdiction was NOT flagged would have nothing to read -- which is
    the question that gets asked after something has gone wrong.
    """
    assessment = score_application(
        ApplicantProfile(country="GB", vendor_status="Approved", hits=())
    )
    assert not any(s.code.startswith("country") for s in assessment.signals)
    recorded = assessment.as_dict()["country_list"]
    assert recorded["increased_monitoring"]["digest"] == INCREASED_MONITORING.digest
