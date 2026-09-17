"""Tests for the lifecycle state machine.

No database, no network — lifecycle.py is pure functions, which is the reason
it was written that way. These are the rules that make duplicate and
out-of-order delivery harmless, so they deserve to be checked directly rather
than inferred from an end-to-end run.

    .venv/Scripts/python -m pytest
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lifecycle import (
    ALLOWED_TRANSITIONS,
    LIFECYCLE,
    VENDOR_STATUS_TO_LIFECYCLE,
    Outcome,
    evaluate_vendor_result,
    is_legal,
    stage_of,
)

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
LATER = T0 + timedelta(minutes=5)
EARLIER = T0 - timedelta(minutes=5)


def evaluate(
    current: str,
    vendor_status: str,
    *,
    result_at: datetime | None = None,
    last_at: datetime | None = None,
):
    return evaluate_vendor_result(
        current_status=current,
        vendor_status=vendor_status,
        vendor_result_at=result_at,
        last_result_at=last_at,
    )


# --- the shape of the machine ----------------------------------------------


def test_every_state_has_transition_rules():
    assert set(ALLOWED_TRANSITIONS) == set(LIFECYCLE)


def test_every_target_is_a_real_state():
    for source, targets in ALLOWED_TRANSITIONS.items():
        for target in targets:
            assert target in LIFECYCLE, f"{source} -> {target} is not a state"


def test_no_transition_ever_goes_backwards():
    for source, targets in ALLOWED_TRANSITIONS.items():
        for target in targets:
            assert stage_of(target) > stage_of(source), f"{source} -> {target}"


def test_decided_is_terminal():
    assert ALLOWED_TRANSITIONS["decided"] == frozenset()


def test_decided_is_reachable_only_from_screening():
    """The compliance rule: no deciding on a customer you have not screened.

    This is the single most important assertion in the file. If someone adds a
    convenient shortcut — say checking -> decided for auto-rejecting abandoned
    applications — this fails and makes them justify it.
    """
    sources = [s for s, targets in ALLOWED_TRANSITIONS.items() if "decided" in targets]
    assert sources == ["screening"]


def test_every_vendor_status_maps_to_a_real_state():
    for vendor_status, target in VENDOR_STATUS_TO_LIFECYCLE.items():
        assert target in LIFECYCLE, f"{vendor_status} maps to {target}"


def test_vendor_can_never_drive_an_application_to_decided():
    """A vendor's verdict is never our decision.

    "Approved" means the document check passed. Whether the customer may be
    onboarded depends on sanctions screening and risk scoring, which the vendor
    knows nothing about. No vendor status may map to 'decided'.
    """
    assert "decided" not in VENDOR_STATUS_TO_LIFECYCLE.values()


# --- legality ---------------------------------------------------------------


@pytest.mark.parametrize(
    "source,target",
    [
        ("started", "submitted"),
        ("started", "screening"),  # a fast vendor may skip stages
        ("submitted", "checking"),
        ("checking", "screening"),
        ("screening", "decided"),
    ],
)
def test_legal_transitions(source, target):
    assert is_legal(source, target)


@pytest.mark.parametrize(
    "source,target",
    [
        ("decided", "checking"),  # the regression this phase exists to prevent
        ("decided", "screening"),
        ("screening", "checking"),
        ("checking", "submitted"),
        ("submitted", "started"),
        ("checking", "decided"),  # would decide without screening
        ("started", "decided"),
    ],
)
def test_illegal_transitions(source, target):
    assert not is_legal(source, target)


# --- applying a result ------------------------------------------------------


def test_ordinary_progress_is_applied():
    result = evaluate("submitted", "Approved")
    assert result.outcome == Outcome.APPLIED
    assert result.target == "screening"


def test_same_state_is_no_change_not_an_error():
    """A duplicate delivery is the system working normally."""
    result = evaluate("submitted", "In Progress")
    assert result.outcome == Outcome.NO_CHANGE
    assert not result.should_write


def test_late_result_cannot_regress_a_decided_application():
    """The scenario named in the brief.

    The vendor's first result arrives after its second. The application has
    already reached 'decided'; an "In Review" turning up afterwards must not
    drag it back to 'checking'.
    """
    result = evaluate("decided", "In Review")
    assert result.outcome == Outcome.ILLEGAL_TRANSITION
    assert not result.should_write


def test_unknown_vendor_status_is_refused_rather_than_guessed():
    result = evaluate("submitted", "Something New They Added")
    assert result.outcome == Outcome.UNKNOWN_VENDOR_STATUS
    assert not result.should_write


# --- recency ----------------------------------------------------------------


def test_older_vendor_timestamp_is_rejected():
    result = evaluate("submitted", "Approved", result_at=EARLIER, last_at=T0)
    assert result.outcome == Outcome.STALE_RESULT
    assert not result.should_write


def test_newer_vendor_timestamp_is_applied():
    result = evaluate("submitted", "Approved", result_at=LATER, last_at=T0)
    assert result.outcome == Outcome.APPLIED


def test_equal_timestamps_are_not_treated_as_stale():
    """Equal is not older. A redelivery of the same event should fall through to
    the ordinary duplicate path, not be reported as a stale result."""
    result = evaluate("submitted", "Approved", result_at=T0, last_at=T0)
    assert result.outcome == Outcome.APPLIED


def test_missing_vendor_timestamp_falls_back_to_the_state_machine():
    """Results PULLED from the vendor carry no timestamp, because their decision
    endpoint publishes none. That must not disable the legality check."""
    result = evaluate("decided", "In Review", result_at=None, last_at=T0)
    assert result.outcome == Outcome.ILLEGAL_TRANSITION


def test_recency_catches_what_the_state_machine_cannot():
    """The gap that makes both guards necessary.

    A 'Declined' and a corrected 'Approved' both map to 'screening'. The
    transition is legal either way, so transition rules alone would apply
    whichever arrived last — including the older one. Only the timestamp
    separates them.
    """
    stale_but_legal = evaluate(
        "checking", "Declined", result_at=EARLIER, last_at=T0
    )
    assert stale_but_legal.outcome == Outcome.STALE_RESULT

    without_the_guard = evaluate("checking", "Declined")
    assert without_the_guard.outcome == Outcome.APPLIED


def test_state_machine_catches_what_recency_cannot():
    """The mirror image: a newer message proposing an illegal jump."""
    result = evaluate("started", "Approved", result_at=LATER, last_at=T0)
    assert result.outcome == Outcome.APPLIED
    assert result.target == "screening"

    # ... but nothing may jump straight to a decision, however new it is.
    assert not is_legal("started", "decided")
