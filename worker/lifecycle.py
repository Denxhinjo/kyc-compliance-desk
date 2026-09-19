"""The application lifecycle, as an explicit state machine.

    started -> submitted -> checking -> screening -> decided

Pure functions with no database and no I/O, so every rule here is testable in
isolation and readable without following a call chain. This module is the
safety-critical part of Phase 4: it is what makes duplicate and out-of-order
delivery harmless.

WHY A LIFECYCLE RATHER THAN A STORED RESULT

A result column answers "what did the vendor say" and can never answer "where is
this application". When a case has sat untouched for an hour, `result is null`
cannot distinguish between: the applicant never started; started and abandoned;
finished but the webhook was lost; finished and our worker crashed. Those need
four different responses, and a result column collapses them into one. The
sweeper's entire premise — "which applications are in a state they should have
left by now?" — is a question you cannot ask of a nullable result.

WHY THE STATE MACHINE MAKES OUT-OF-ORDER DELIVERY SAFE

At-least-once delivery plus network reordering means every message may arrive
twice and any two may arrive in either order. "Apply what the message says" is
therefore a bug: the last arrival wins regardless of whether it is the newest.

A state machine turns ordering from something you hope about into something the
code decides. Every update becomes "is this transition permitted from where we
are?", and the answer depends on the pair of states, not on arrival time. A late
'checking' reaching a 'decided' application is not a race that was lost; it is a
transition that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

Status = str

#: Ordered for display and for reasoning about direction.
LIFECYCLE: tuple[Status, ...] = (
    "started",
    "submitted",
    "checking",
    "screening",
    "decided",
)

#: What each stage means, so the vocabulary stays fixed.
MEANING: dict[Status, str] = {
    "started": "created; the applicant has not begun verification",
    "submitted": "a verification session exists; the applicant is in the vendor's flow",
    "checking": "the applicant has finished; we are waiting for the vendor's verdict",
    "screening": "the vendor's verdict is in; our own checks are running",
    "decided": "a decision has been recorded",
}

#: The permitted transitions.
#:
#: ============================================================================
#: THIS IS A MIRROR. THE DATABASE IS AUTHORITATIVE.
#: ============================================================================
#:
#: The real rule lives in the `application_transitions` table and is enforced by
#: the BEFORE UPDATE trigger on `applications` (migration 017). This copy exists
#: only so the worker can decide what to do WITHOUT a round trip — to answer
#: "should I write this?" before attempting the write.
#:
#: It is not the source of truth and must never be treated as one. If the two
#: ever disagree, the database wins by construction: it refuses the UPDATE. The
#: only consequence of this copy drifting is that the worker would attempt a
#: write the database then rejects — noisy, but not incorrect.
#:
#: test_lifecycle_trigger.py asserts the two are identical, so drift is a test
#: failure rather than a surprise in production.
#:
#: WHY THE DATABASE AND NOT HERE
#:
#: Two services share this database and nothing else. A rule held in Python is
#: a rule the TypeScript side can ignore — and for two releases it did, guarded
#: only by a WHERE clause in each query, which is a convention duplicated across
#: two languages. /db exists precisely so the schema is owned by neither
#: service; Phase 4 put the lifecycle here and did not follow that principle.
#: Migration 017 corrects it.
#:
#: Forward skips are allowed because a fast vendor genuinely can jump a stage —
#: an applicant who completes instantly goes from 'submitted' straight to a
#: verdict, never pausing in 'checking'. Backward moves are never allowed.
#:
#: Note what is absent: NOTHING reaches 'decided' except 'screening'. That is
#: not tidiness, it is a compliance rule — you may not decide on a customer you
#: have not screened. 'decided' is terminal.
ALLOWED_TRANSITIONS: dict[Status, frozenset[Status]] = {
    "started": frozenset({"submitted", "checking", "screening"}),
    "submitted": frozenset({"checking", "screening"}),
    "checking": frozenset({"screening"}),
    "screening": frozenset({"decided"}),
    "decided": frozenset(),
}

#: The vendor's status vocabulary mapped onto ours.
#:
#: These are deliberately NOT the same vocabulary. "Approved" from the vendor
#: means the DOCUMENT CHECK passed — it does not mean we have approved the
#: customer, which is a decision we have not made yet and which depends on
#: sanctions screening and risk scoring in Phase 5. Conflating the vendor's
#: verdict with our decision would be the worst mistake available in this phase,
#: so every terminal vendor outcome maps to 'screening': "their part is done,
#: ours begins".
VENDOR_STATUS_TO_LIFECYCLE: dict[str, Status] = {
    "Not Started": "submitted",
    "In Progress": "submitted",
    "Awaiting User": "submitted",
    "Resubmitted": "submitted",
    # The vendor's own humans are reviewing. No verdict yet, so we are waiting.
    "In Review": "checking",
    # Terminal vendor outcomes. Their part is finished, whatever the answer.
    "Approved": "screening",
    "Declined": "screening",
    "Abandoned": "screening",
    "Expired": "screening",
    "Kyc Expired": "screening",
}


class Outcome:
    """Why a proposed update was applied or refused."""

    APPLIED = "applied"
    #: Same state; nothing to do. The ordinary result of a duplicate delivery.
    NO_CHANGE = "no_change"
    #: The transition is not in ALLOWED_TRANSITIONS.
    ILLEGAL_TRANSITION = "illegal_transition"
    #: The vendor's own timestamp is older than the last result we applied.
    STALE_RESULT = "stale_result"
    #: The vendor sent a status we have no mapping for.
    UNKNOWN_VENDOR_STATUS = "unknown_vendor_status"


@dataclass(frozen=True)
class Evaluation:
    outcome: str
    target: Status | None
    reason: str

    @property
    def should_write(self) -> bool:
        return self.outcome == Outcome.APPLIED


def is_legal(current: Status, target: Status) -> bool:
    """Would the database accept this transition?

    An advisory answer, read from the local mirror so no round trip is needed.
    The authoritative answer is whatever the trigger on `applications` does when
    the UPDATE is attempted — this only lets the worker avoid attempting writes
    it already knows will be refused.
    """
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def stage_of(status: Status) -> int:
    """Position in the lifecycle, or -1 if unknown. For display only.

    Deliberately not used to decide legality: "the index went up" is a weaker
    rule than the explicit edge list, and it would silently permit
    checking -> decided, which is the one transition that must never happen.
    """
    try:
        return LIFECYCLE.index(status)
    except ValueError:
        return -1


def evaluate_vendor_result(
    *,
    current_status: Status,
    vendor_status: str,
    vendor_result_at: datetime | None,
    last_result_at: datetime | None,
) -> Evaluation:
    """Decide what, if anything, a vendor result should change.

    The two guards run in this order, because they answer different questions:

      1. RECENCY, using the vendor's clock only. `vendor_result_at` is None for
         results we PULLED from the vendor's API rather than received by
         webhook, because their decision endpoint exposes no result timestamp.
         That is not a gap: a pulled result IS the session's current state by
         construction, so it cannot be stale, and the state machine still
         governs whether it may be applied.

      2. LEGALITY, using our own state machine. This never depends on anyone
         else's clock, which is why it is the primary guarantee.

    Neither alone is sufficient. Recency alone would let a newer message take an
    application from 'started' to 'decided' unscreened. Legality alone cannot
    separate two results that map to the SAME stage — a 'Declined' and a
    corrected 'Approved' arriving out of order are both a legal move to
    'screening', so the older would win and nothing would notice.
    """
    target = VENDOR_STATUS_TO_LIFECYCLE.get(vendor_status)
    if target is None:
        return Evaluation(
            Outcome.UNKNOWN_VENDOR_STATUS,
            None,
            f"no lifecycle mapping for vendor status {vendor_status!r}",
        )

    if (
        vendor_result_at is not None
        and last_result_at is not None
        and vendor_result_at < last_result_at
    ):
        return Evaluation(
            Outcome.STALE_RESULT,
            target,
            f"vendor timestamp {vendor_result_at.isoformat()} is older than the "
            f"last applied result {last_result_at.isoformat()}",
        )

    if target == current_status:
        return Evaluation(
            Outcome.NO_CHANGE,
            target,
            f"already {current_status}",
        )

    if not is_legal(current_status, target):
        return Evaluation(
            Outcome.ILLEGAL_TRANSITION,
            target,
            f"{current_status} -> {target} is not a permitted transition",
        )

    return Evaluation(Outcome.APPLIED, target, f"{current_status} -> {target}")
