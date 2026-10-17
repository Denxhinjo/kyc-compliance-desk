"""Ruleset 2026-10-2: a recorded sanctions hit always reaches a human.

THE PROPERTY

    No application carrying a sanctions hit is ever decided automatically --
    neither approved nor rejected -- whatever else is true about it.

WHY IT IS A ROUTING RULE AND NOT A CHOICE OF POINTS

Points stack. Enumerating every combination of a sanctions hit with every
other signal, 481 of 720 reached the rejection threshold, the smallest being
a hit the list's OWN date of birth contradicts (20) plus a call-for-action
country (40) plus an in-progress document check (20). No assignment of points
fixes that while keeping the other signals meaningful, because the sanctions
hit is not what pushes the total over the line -- the other signals are.

WHAT IT WITHDRAWS

2026-10-1 said, deliberately: "'confirmed' is 60: enough to guarantee review,
not enough to refuse anyone by itself. Reaching 80 needs a second, independent
signal." That second route to automatic refusal is now closed. A genuine
sanctions match must be escalated and reported rather than silently refused,
and a case the list's own evidence contradicts may well be a different person.
"""

from __future__ import annotations

import itertools

import pytest

from scoring import (
    ADVERSE_MEDIA_POINTS,
    DOCUMENT_POINTS,
    FATF_CALL_FOR_ACTION,
    FATF_INCREASED_MONITORING,
    PEP_POINTS,
    SANCTIONS_POINTS,
    ApplicantProfile,
    Routing,
    ScreeningHit,
    Thresholds,
    score_application,
)

CONFLICT = ({"identifier": "date_of_birth", "outcome": "conflict", "detail": "x"},)

#: One score per band, plus the contradicted case.
SANCTIONS_CASES = [
    ("exact", 100.0, ()),
    ("strong", 90.0, ()),
    ("partial", 50.0, ()),
    ("exact-contradicted", 100.0, CONFLICT),
    ("partial-contradicted", 50.0, CONFLICT),
]

COUNTRIES = ["GB", sorted(FATF_INCREASED_MONITORING)[0], sorted(FATF_CALL_FOR_ACTION)[0]]
DOCUMENTS = [None, "Approved", "In Review", "In Progress", "Abandoned", "Declined"]
OTHER_HITS = [
    (),
    (ScreeningHit("pep", "L", "N", 100.0, "P1"),),
    (ScreeningHit("adverse_media", "L", "N", 100.0, "A1"),),
    (
        ScreeningHit("pep", "L", "N", 100.0, "P1"),
        ScreeningHit("adverse_media", "L", "N", 100.0, "A1"),
    ),
]


def _cases():
    for (label, score, comparisons), country, document, others in itertools.product(
        SANCTIONS_CASES, COUNTRIES, DOCUMENTS, OTHER_HITS
    ):
        hit = ScreeningHit(
            match_type="sanctions",
            list_name="OFAC SDN",
            matched_name="Someone Listed",
            match_score=score,
            entity_id="S1",
            comparisons=comparisons,
        )
        yield label, country, document, ApplicantProfile(
            country=country, vendor_status=document, hits=(hit, *others)
        )


ALL_CASES = list(_cases())


def test_the_enumeration_is_broad_enough_to_mean_something():
    """Guards the guard: a property proved over three cases is an anecdote."""
    assert len(ALL_CASES) >= 300
    # It must contain combinations that WOULD auto-reject on points alone,
    # or it is not exercising the rule at all.
    threshold = Thresholds().auto_reject_at_or_above
    over = [c for c in ALL_CASES if score_application(c[3]).score >= threshold]
    assert len(over) >= 100, (
        "no combination reaches the rejection threshold, so this says nothing "
        "about whether the override is doing any work"
    )


@pytest.mark.parametrize(
    "label,country,document,profile",
    ALL_CASES,
    ids=[f"{c[0]}|{c[1]}|{c[2]}" for c in ALL_CASES],
)
def test_a_sanctions_hit_is_never_decided_automatically(
    label, country, document, profile
):
    assessment = score_application(profile)
    assert assessment.routing == Routing.REVIEW, (
        f"{label} in {country} with document {document!r} scored "
        f"{assessment.score} and routed {assessment.routing}"
    )


def test_auto_approval_was_already_unreachable_by_arithmetic():
    """The other half, and it holds without the override.

    The weakest sanctions band is worth 20 and auto-approval needs a total
    below 20, so a recorded sanctions hit could never have been approved
    automatically. Asserted rather than assumed, because it is the half
    someone would otherwise delete the override believing it redundant.
    """
    assert min(SANCTIONS_POINTS.values()) >= Thresholds().auto_approve_below


def test_the_override_is_what_prevents_rejection_not_the_points():
    """Shows the override is load-bearing.

    Without it, this exact profile rejects: 20 + 40 + 20 = 80. With it, a
    reviewer sees a case the list's own date of birth contradicts.
    """
    profile = ApplicantProfile(
        country=sorted(FATF_CALL_FOR_ACTION)[0],
        vendor_status="In Progress",
        hits=(
            ScreeningHit(
                match_type="sanctions",
                list_name="OFAC SDN",
                matched_name="Someone Listed",
                match_score=100.0,
                entity_id="S1",
                comparisons=CONFLICT,
            ),
        ),
    )
    assessment = score_application(profile)
    assert assessment.score >= Thresholds().auto_reject_at_or_above
    assert assessment.routing == Routing.REVIEW


def test_other_match_types_are_not_given_the_override():
    """PEP and adverse media keep the ordinary rules.

    The override exists because a sanctions listing is a legal prohibition
    that has to be escalated and reported. Being a PEP is neither illegal nor
    grounds for refusal, and adverse media is not a listing at all, so neither
    acquires a blanket route to review it did not have before.
    """
    worst = ApplicantProfile(
        country=sorted(FATF_CALL_FOR_ACTION)[0],
        vendor_status="Declined",
        hits=(
            ScreeningHit("pep", "L", "N", 100.0, "P1"),
            ScreeningHit("adverse_media", "L", "N", 100.0, "A1"),
        ),
    )
    total = (
        PEP_POINTS["exact"]
        + ADVERSE_MEDIA_POINTS["exact"]
        + DOCUMENT_POINTS["Declined"]
        + 40
    )
    assessment = score_application(worst)
    assert assessment.score == total
    assert assessment.routing == Routing.REJECT
