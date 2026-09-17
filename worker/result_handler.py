"""Applying a verification result to an application.

Replaces the Phase 3 stub. Two job types arrive here and both funnel into
`apply_result`:

    vendor.process_webhook   the vendor told us something changed
    (the sweeper)            we went and asked

They converge on purpose. The webhook path and the sweeper path must produce
identical outcomes, because the sweeper's whole job is to make the system
correct when the webhook path fails. Two implementations would mean two sets of
bugs and a recovery path exercised only during incidents.

Note also what the webhook handler does NOT do: trust the webhook body. A
verified signature proves the message is genuine and unaltered — it does not
make it the authority on the current state. The webhook says "something
changed"; we then ask the vendor what it is. The body could describe a state
already superseded by the time we process it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from audit import log_event
from handlers import PermanentError, handler
from jobs import Job, enqueue_job
from lifecycle import Evaluation, Outcome, evaluate_vendor_result
from vendor import VendorError, VerificationResult, get_client

log = logging.getLogger("worker.result")


@handler("vendor.process_webhook")
def process_webhook(conn: psycopg.Connection, job: Job) -> None:
    """Handle one stored vendor message."""
    vendor_event_id = job.payload.get("vendor_event_id")
    if vendor_event_id is None:
        raise PermanentError("job payload has no vendor_event_id")

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select id, payload, application_id, event_type from vendor_events where id = %s",
            (vendor_event_id,),
        )
        event = cur.fetchone()

    if event is None:
        raise PermanentError(f"no vendor_event with id {vendor_event_id}")

    payload: dict[str, Any] = event["payload"]

    # vendor_data is OUR applications.id. The payload ALSO has a field called
    # application_id, which is the vendor's own application — their account's
    # app, nothing to do with our applicant. Reading that one would produce a
    # foreign key violation at best and a wrong link at worst.
    application_id = payload.get("vendor_data")
    session_id = payload.get("session_id")
    if not application_id:
        raise PermanentError("webhook payload has no vendor_data to link to")

    application = _load_application(conn, application_id)
    if application is None:
        # The vendor's console can send test events. An event for an application
        # we do not have is information, not a failure worth retrying.
        log.warning(
            "vendor_event %s references unknown application %s — ignoring",
            vendor_event_id,
            application_id,
        )
        return

    # Link the stored message to its case, if it is not linked already.
    if event["application_id"] is None:
        with conn.cursor() as cur:
            cur.execute(
                "update vendor_events set application_id = %s where id = %s",
                (application_id, vendor_event_id),
            )

    if not session_id:
        log.warning("vendor_event %s has no session_id to fetch", vendor_event_id)
        return

    result = _fetch(session_id)
    if result is None:
        raise PermanentError(
            f"vendor has no record of session {session_id}"
        )

    # A webhook carries the vendor's own event time even though their decision
    # endpoint does not, so the recency guard has real data on this path.
    webhook_result_at = _epoch_to_datetime(payload.get("timestamp"))
    result_at = result.result_at or webhook_result_at

    apply_result(
        conn,
        application=application,
        result=result,
        result_at=result_at,
        source="webhook",
        vendor_event_id=vendor_event_id,
    )


def _epoch_to_datetime(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _fetch(session_id: str) -> VerificationResult | None:
    client = get_client()
    try:
        return client.fetch_session(session_id)
    except VendorError as err:
        # Transient by assumption: let the job retry with backoff rather than
        # parking. A vendor being briefly unreachable is the ordinary case the
        # queue exists for.
        raise RuntimeError(f"vendor lookup failed: {err}") from err


def _load_application(
    conn: psycopg.Connection, application_id: str
) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select id, status, vendor_status, vendor_result_at, vendor_applicant_id
              from applications
             where id = %s
            """,
            (application_id,),
        )
        return cur.fetchone()


def apply_result(
    conn: psycopg.Connection,
    *,
    application: dict[str, Any],
    result: VerificationResult,
    result_at: datetime | None,
    source: str,
    vendor_event_id: int | str | None = None,
) -> Evaluation:
    """Decide what this result changes, then write only that.

    The decision is made by pure functions in lifecycle.py; this function does
    the I/O. Keeping them apart is what makes the rules testable without a
    database, and it means the interesting logic can be read in one file with no
    SQL in the way.

    Idempotent by construction: compute the desired state, compare, write only
    the difference, and record only what was written. A second run finds no
    difference and therefore adds no audit rows — which depends on audit_events
    recording state CHANGES rather than handler INVOCATIONS.
    """
    evaluation = evaluate_vendor_result(
        current_status=application["status"],
        vendor_status=result.status,
        vendor_result_at=result_at,
        last_result_at=application["vendor_result_at"],
    )

    if not evaluation.should_write:
        # A refusal is not an error. Out-of-order and duplicate delivery are
        # normal, and the system being unmoved by them is the feature.
        log.info(
            "application %s: %s (%s)",
            application["id"],
            evaluation.outcome,
            evaluation.reason,
        )
        _record_refusal(conn, application, result, evaluation, source)
        return evaluation

    with conn.cursor() as cur:
        cur.execute(
            """
            update applications
               set status = %s,
                   vendor_status = %s,
                   -- greatest() so a result without a vendor timestamp can never
                   -- erase one we already have. coalesce handles the first
                   -- result, where there is nothing to compare against.
                   vendor_result_at = greatest(
                       coalesce(%s, vendor_result_at),
                       coalesce(vendor_result_at, %s)
                   )
             where id = %s
            """,
            (
                evaluation.target,
                result.status,
                result_at,
                result_at,
                application["id"],
            ),
        )

    log_event(
        conn,
        application_id=application["id"],
        actor="system:worker",
        action="status.changed",
        details={
            "from": application["status"],
            "to": evaluation.target,
            "vendor_status": result.status,
            "source": source,
            "vendor_session_id": result.session_id,
            "vendor_event_id": vendor_event_id,
            "vendor_result_at": result_at.isoformat() if result_at else None,
        },
    )
    # Reaching 'screening' means the vendor is finished and our own checks
    # begin. Enqueued in the SAME transaction as the status change, so there is
    # no state in which an application is screening and nothing is going to
    # screen it.
    if evaluation.target == "screening":
        job_id = enqueue_job(
            conn, "screening.run", {"application_id": str(application["id"])}
        )
        log.info("queued screening job %s for application %s", job_id, application["id"])

    log.info(
        "application %s: %s -> %s (vendor %s, via %s)",
        application["id"],
        application["status"],
        evaluation.target,
        result.status,
        source,
    )
    return evaluation


def _record_refusal(
    conn: psycopg.Connection,
    application: dict[str, Any],
    result: VerificationResult,
    evaluation: Evaluation,
    source: str,
) -> None:
    """Write an audit row for a result we deliberately did not apply.

    Only for refusals that mean something happened and we chose to ignore it —
    a stale result, or an illegal transition. NO_CHANGE is silent, because a
    duplicate delivery changing nothing is the system working normally and
    logging it would fill the audit log with noise and make a genuinely
    idempotent handler look like it was doing something.

    A rejected result IS worth recording: "the vendor sent us a verdict and we
    refused it" is exactly the kind of thing an auditor asks about later.
    """
    if evaluation.outcome == Outcome.NO_CHANGE:
        return

    log_event(
        conn,
        application_id=application["id"],
        actor="system:worker",
        action="vendor_result.refused",
        details={
            "outcome": evaluation.outcome,
            "reason": evaluation.reason,
            "current_status": application["status"],
            "proposed_status": evaluation.target,
            "vendor_status": result.status,
            "source": source,
        },
    )
