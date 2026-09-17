"""Handler for vendor webhook events.

The job Phase 3's webhook enqueues. It does the interpreting that the webhook
endpoint deliberately refused to do.

THIS HANDLER MUST BE SAFE TO RUN TWICE. That is not a stylistic preference, it
follows from Phase 2: the queue gives at-least-once delivery, because a worker
can do its work and then die before marking the job done. There is no setting
that fixes that — an HTTP request cannot be rolled back, so exactly-once does
not exist across a process boundary.

The technique used here is worth stating plainly, because it is what makes
idempotency demonstrable rather than merely claimed:

    Compute the desired state. Compare it with the current state.
    Write only the difference. Log only what was actually written.

Run it a second time and the difference is empty, so nothing is written and —
critically — NO NEW AUDIT ROWS appear. That last part depends on audit_events
recording state *changes* rather than handler *invocations*. Get that wrong and
a perfectly idempotent handler still leaves a growing trail of identical rows,
which looks exactly like a bug and destroys the audit log's usefulness.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

from audit import log_event
from handlers import PermanentError, handler
from jobs import Job

log = logging.getLogger("worker.didit")

# The lifecycle from 001_applications.sql, in order. Used to refuse backwards
# transitions: webhooks can arrive out of order, and a late "In Progress" must
# not drag an application that has already reached 'checking' back a step.
LIFECYCLE = ["started", "submitted", "checking", "screening", "decided"]

# Didit's session statuses -> where that puts our application.
#
# Phase 3 only needs to get an application moving. The full lifecycle, including
# what Approved and Declined ultimately mean for a decision, belongs to Phase 4
# and Phase 5 — a webhook saying "Approved" means the DOCUMENT check passed, not
# that we have approved the customer. Those are very different claims and
# conflating them would be the single worst mistake available here.
VENDOR_STATUS_TO_LIFECYCLE: dict[str, str] = {
    "Not Started": "started",
    "In Progress": "submitted",
    "Resubmitted": "submitted",
    "Awaiting User": "submitted",
    "Approved": "checking",
    "Declined": "checking",
    "In Review": "checking",
    "Abandoned": "submitted",
    "Expired": "submitted",
    "Kyc Expired": "submitted",
}


def _stage(status: str) -> int:
    try:
        return LIFECYCLE.index(status)
    except ValueError:
        return -1


@handler("didit.process_webhook")
def process_webhook(conn: psycopg.Connection, job: Job) -> None:
    vendor_event_id = job.payload.get("vendor_event_id")
    if vendor_event_id is None:
        raise PermanentError("job payload has no vendor_event_id")

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select id, payload, application_id, event_type
              from vendor_events
             where id = %s
            """,
            (vendor_event_id,),
        )
        event = cur.fetchone()

    if event is None:
        # The row is gone or never existed. Retrying cannot conjure it.
        raise PermanentError(f"no vendor_event with id {vendor_event_id}")

    payload: dict[str, Any] = event["payload"]

    # vendor_data is OUR applications.id. Note that the payload ALSO has a field
    # called application_id, which is Didit's own application — their account's
    # app, nothing to do with our applicants. Reading that one instead would
    # produce a foreign key violation at best and a wrong link at worst.
    application_id = payload.get("vendor_data")
    if not application_id:
        raise PermanentError("webhook payload has no vendor_data to link to")

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select id, status from applications where id = %s",
            (application_id,),
        )
        application = cur.fetchone()

    if application is None:
        # Not an error worth retrying or parking loudly: Didit's console can
        # send test events, and an event for an application we do not have is
        # information, not a failure. Recorded in the log and the job completes.
        log.warning(
            "vendor_event %s references unknown application %s — ignoring",
            vendor_event_id,
            application_id,
        )
        return

    changes: dict[str, Any] = {}

    # --- difference 1: link the event to its application ---------------------
    if event["application_id"] is None:
        with conn.cursor() as cur:
            cur.execute(
                "update vendor_events set application_id = %s where id = %s",
                (application_id, vendor_event_id),
            )
        changes["linked_vendor_event"] = vendor_event_id

    # --- difference 2: move the application forward, never backward ----------
    vendor_status = payload.get("status")
    target = VENDOR_STATUS_TO_LIFECYCLE.get(vendor_status or "")
    current = application["status"]

    if target and _stage(target) > _stage(current):
        with conn.cursor() as cur:
            cur.execute(
                "update applications set status = %s where id = %s",
                (target, application_id),
            )
        changes["status_from"] = current
        changes["status_to"] = target

    # --- write an audit row only if something actually changed ---------------
    if not changes:
        log.info(
            "vendor_event %s: nothing to change (application already %s) — "
            "this is what a second run looks like",
            vendor_event_id,
            current,
        )
        return

    log_event(
        conn,
        application_id=application_id,
        actor="vendor:didit",
        action="vendor_event.processed",
        details={
            "vendor_event_id": vendor_event_id,
            "webhook_type": event["event_type"],
            "vendor_status": vendor_status,
            **changes,
        },
    )
    log.info("vendor_event %s applied %s", vendor_event_id, changes)
