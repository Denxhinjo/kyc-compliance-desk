"""The sweeper: ask the vendor about applications nobody told us about.

WHY THIS EXISTS

A webhook is a delivery ATTEMPT, not a delivery guarantee. Didit retries twice —
roughly one minute and four minutes — and then drops the message permanently.
So a five-minute deploy, a database blip that makes us answer 500, an expired
tunnel, a firewall change, or a bug in our own handler all end the same way: the
vendor has a verdict, we never hear it, and the applicant waits forever.

Without this, the recovery path is "a human notices". For a compliance system,
an application silently stuck for days is a regulatory problem, not merely poor
service.

The sweeper inverts the dependency. Rather than trusting the vendor to tell us,
we periodically ask. That makes webhooks an OPTIMISATION — they make the common
case fast — while this is what makes the system correct. Push for latency, poll
for correctness.

It is a self-rescheduling JOB rather than a timer in the worker loop. A timer
fires in every worker, so the sweep would run once per worker instead of once.
As a queued job, FOR UPDATE SKIP LOCKED already guarantees exactly one worker
claims it, the next run is visible in the jobs table rather than buried in a
process, and it survives restarts.

THREE ANSWERS, NOT TWO — added 2026-10-14

Asking the vendor can end three ways, and for a long time this file only had
somewhere to put two of them:

  1. The vendor answered with a result    -> apply it through the normal path
  2. The vendor could not be asked       -> count it, retry on the next sweep
  3. The vendor has no record of the session

The third answer is TERMINAL INFORMATION and it was being discarded. The code
logged a line and moved to the next application, so the same 38 applications
were asked about on every sweep for three weeks, got the same answer every
time, and stayed "in flight" with nothing recorded anywhere. A sweeper that
cannot conclude anything is not a recovery mechanism, it is a loop.

It now escalates: every "no record" answer writes an audit event, and once
there are enough of them spread over enough time, the application is closed as
`verification_unavailable`.

NOT A REJECTION. A rejection asserts a risk finding about a person. There is no
finding here — we could not obtain the evidence. Recording this as a rejection
would invent an adverse outcome out of a vendor's data loss.

PROVABLE FROM HISTORY. The escalation counts `vendor.session_not_found` rows in
the append-only audit log rather than a column on the application. Months
later, "why was this closed?" is answered by five immutable rows with
timestamps, not by a counter whose current value says nothing about how it got
there and which an UPDATE could have reset without trace.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config import (
    ABANDONED_EXPIRY_DAYS,
    SESSION_NOT_FOUND_CLOSE_AFTER,
    SESSION_NOT_FOUND_MIN_SPAN_HOURS,
    SWEEP_BATCH_SIZE,
    SWEEP_INTERVAL_MINUTES,
    SWEEP_STUCK_MINUTES,
)
from audit import seeded_marker
from handlers import handler
from jobs import Job
from result_handler import apply_result
from vendor import VendorError, get_client

log = logging.getLogger("worker.sweeper")

JOB_TYPE = "vendor.sweep_stuck"

#: The audit action written each time the vendor disowns a session. The
#: escalation reads these back, so the name is part of the contract and not
#: just a log string.
NOT_FOUND_ACTION = "vendor.session_not_found"

#: Written when an application is closed because the vendor never had its
#: session, and when one is closed for never having started verification.
CLOSED_ACTION = "verification.unavailable"
EXPIRED_ACTION = "application.expired"


def ensure_scheduled(conn: psycopg.Connection) -> bool:
    """Make sure exactly one sweep is on the queue. Safe to call from anywhere.

    Called by every worker at startup, so several may race. The partial unique
    index from migration 011 settles it: at most one row of this job type may be
    'queued', so ON CONFLICT DO NOTHING turns the race into a no-op rather than
    an error.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, payload, run_after)
            values (%s, '{}'::jsonb, now())
            on conflict do nothing
            returning id
            """,
            (JOB_TYPE,),
        )
        return cur.fetchone() is not None


def _schedule_next(conn: psycopg.Connection) -> None:
    """Queue the next sweep.

    Runs while this sweep is still 'running', which is exactly why the unique
    index covers only 'queued' rows — a constraint including 'running' would
    make a recurring job unable to schedule its own successor.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, payload, run_after)
            values (%s, '{}'::jsonb, now() + make_interval(secs => %s))
            on conflict do nothing
            """,
            # secs, not mins: make_interval's `mins` parameter is an integer, so
            # a fractional interval would be a type error. `secs` is double
            # precision, which also keeps sub-minute values usable in demos.
            (JOB_TYPE, SWEEP_INTERVAL_MINUTES * 60),
        )


def _stuck_applications(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Applications waiting on the vendor for longer than they should be.

    The threshold must exceed the vendor's own retry schedule, or the sweeper
    races deliveries still in flight and does work the webhook was about to do.
    Harmless — both paths are idempotent — but wasteful, and it would make the
    sweeper look responsible for work that arrived normally.

    'started' is deliberately excluded: an application with no verification
    session is waiting on the APPLICANT, not on the vendor, and there is nothing
    to ask about. Those are closed by `expire_abandoned` below, after a much
    longer window, and never by asking the vendor.

    ORDERED BY WHEN WE LAST ASKED, NOT BY updated_at.

    This fixes a starvation bug. The batch is capped at SWEEP_BATCH_SIZE and the
    order used to be `updated_at` ascending — but a sweep that resolves nothing
    does not touch `updated_at`, so the same oldest N applications were examined
    on every run and anything beyond the cap was NEVER examined at all. On
    production that was 25 of 38 examined forever and 13 never once.

    `last_asked` comes from the audit log, so the rotation is driven by the same
    immutable history the escalation counts, and `nulls first` puts applications
    we have never asked about at the front. Every qualifying application is
    therefore examined eventually, which is what makes the escalation reachable
    for all of them rather than only for the oldest batch.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select a.id,
                   a.status,
                   a.vendor_status,
                   a.vendor_result_at,
                   a.vendor_applicant_id,
                   (select max(e.occurred_at)
                      from audit_events e
                     where e.application_id = a.id
                       and e.action = %s) as last_asked
              from applications a
             where a.status in ('submitted', 'checking')
               and a.vendor_applicant_id is not null
               and a.updated_at < now() - make_interval(secs => %s)
             order by last_asked asc nulls first, a.updated_at asc
             limit %s
            """,
            (NOT_FOUND_ACTION, SWEEP_STUCK_MINUTES * 60, SWEEP_BATCH_SIZE),
        )
        return list(cur.fetchall())


def _record_not_found(
    conn: psycopg.Connection, application_id: Any, session_id: Any
) -> None:
    """Write down that the vendor disowned this session.

    One row per answer. These accumulate, and they are the only evidence the
    escalation below consults.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into audit_events (application_id, actor, action, details)
            values (%s, 'system:worker', %s, %s)
            """,
            (
                application_id,
                NOT_FOUND_ACTION,
                Jsonb(
                    {
                        # Carried through from the application's creation
                        # event, so a seeded case's whole history is marked.
                        **seeded_marker(conn, application_id),
                        "session_id": str(session_id),
                        "vendor": "the vendor has no record of this session",
                        "close_after": SESSION_NOT_FOUND_CLOSE_AFTER,
                        "min_span_hours": SESSION_NOT_FOUND_MIN_SPAN_HOURS,
                    }
                ),
            ),
        )


def _close_if_confirmed_missing(
    conn: psycopg.Connection, application_id: Any, session_id: Any
) -> bool:
    """Close the application if the vendor has disowned it enough times.

    Returns True if this call closed it.

    TWO CONDITIONS, BOTH REQUIRED: at least SESSION_NOT_FOUND_CLOSE_AFTER
    answers, spanning at least SESSION_NOT_FOUND_MIN_SPAN_HOURS from first to
    last. Three answers a minute apart is one broken deploy of the simulator;
    three answers over an hour is a vendor that has genuinely lost the session.

    CONCURRENCY. Two sweeps can be in flight at once — nothing stops an operator
    triggering a drain while the scheduled one runs. Both would see the same
    application and both would reach this function, and closing twice would
    write two closure events for one event.

    `for update skip locked` settles it without blocking: the second sweep does
    not get the row, returns False, and leaves the application alone. Next sweep
    it will see the status has changed and stop at the guard below. The status is
    re-read INSIDE the lock rather than trusted from the caller's earlier SELECT,
    which is what covers the other interleaving — where the first sweep has
    already committed and the lock is free, but the work is done.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select status from applications
             where id = %s
               and status in ('submitted', 'checking')
               for update skip locked
            """,
            (application_id,),
        )
        row = cur.fetchone()
        if row is None:
            # Either another sweep holds it, or it is no longer in a state this
            # can close. Both mean: not ours to act on.
            return False
        current_status = row[0]

        cur.execute(
            """
            select count(*),
                   min(occurred_at),
                   max(occurred_at),
                   max(occurred_at) - min(occurred_at)
                     >= make_interval(hours => 0, secs => %s) as span_reached
              from audit_events
             where application_id = %s
               and action = %s
            """,
            (
                SESSION_NOT_FOUND_MIN_SPAN_HOURS * 3600,
                application_id,
                NOT_FOUND_ACTION,
            ),
        )
        seen, first_at, last_at, span_reached = cur.fetchone()

        if seen < SESSION_NOT_FOUND_CLOSE_AFTER or not span_reached:
            return False

        # The transition itself. The lifecycle trigger is what actually permits
        # it — migration 027 added submitted/checking -> verification_unavailable
        # and nothing else — so an attempt from any other state is refused by
        # the database rather than by this code remembering to check.
        cur.execute(
            "update applications set status = 'verification_unavailable' where id = %s",
            (application_id,),
        )

        cur.execute(
            """
            insert into audit_events (application_id, actor, action, details)
            values (%s, 'system:worker', %s, %s)
            """,
            (
                application_id,
                CLOSED_ACTION,
                Jsonb(
                    {
                        **seeded_marker(conn, application_id),
                        "from_status": current_status,
                        "session_id": str(session_id),
                        "not_found_answers": seen,
                        "first_asked": first_at.isoformat() if first_at else None,
                        "last_asked": last_at.isoformat() if last_at else None,
                        "reason": (
                            "verification not completed: the vendor has no record "
                            "of the verification session"
                        ),
                        "not_a_rejection": (
                            "no risk finding was made; the evidence could not be "
                            "obtained"
                        ),
                    }
                ),
            ),
        )

    log.warning(
        "closed application %s as verification_unavailable after %d not-found answers",
        application_id,
        seen,
    )
    return True


def expire_abandoned(conn: psycopg.Connection) -> int:
    """Close applications that were created and then never begun.

    'started' with no vendor session means the applicant filled in the form and
    never continued to verification. There is no session to ask about, so the
    sweeper above cannot help and correctly ignores them — which is why they sat
    in 'in flight' for up to 53 days on production with nothing examining them.

    A PROCESS TIMEOUT, NOT A RISK DECISION, and `expired` is a separate status
    from `decided` for exactly that reason. Nobody has been refused anything.

    `for update skip locked` for the same reason as above, and the status is
    re-checked by the WHERE clause inside the lock. One UPDATE per application
    rather than one set-based UPDATE, because each needs its own audit row and
    the lifecycle trigger fires per row anyway.
    """
    expired = 0
    with conn.cursor() as cur:
        cur.execute(
            """
            select id from applications
             where status = 'started'
               and vendor_applicant_id is null
               and created_at < now() - make_interval(secs => %s)
             order by created_at
             limit %s
               for update skip locked
            """,
            (ABANDONED_EXPIRY_DAYS * 86400, SWEEP_BATCH_SIZE),
        )
        candidates = [row[0] for row in cur.fetchall()]

        for application_id in candidates:
            cur.execute(
                "update applications set status = 'expired' where id = %s",
                (application_id,),
            )
            cur.execute(
                """
                insert into audit_events (application_id, actor, action, details)
                values (%s, 'system:worker', %s, %s)
                """,
                (
                    application_id,
                    EXPIRED_ACTION,
                    Jsonb(
                        {
                            **seeded_marker(conn, application_id),
                            "from_status": "started",
                            "after_days": ABANDONED_EXPIRY_DAYS,
                            "reason": (
                                "verification never begun: no verification session "
                                "was created within the abandonment window"
                            ),
                            "not_a_rejection": (
                                "a process timeout; no risk finding was made"
                            ),
                        }
                    ),
                ),
            )
            expired += 1

    if expired:
        log.warning("expired %d abandoned application(s)", expired)
    return expired


@handler(JOB_TYPE)
def sweep_stuck(conn: psycopg.Connection, job: Job) -> None:
    stuck = _stuck_applications(conn)

    client = get_client() if stuck else None
    applied = 0
    unreachable = 0
    not_found = 0
    closed = 0

    for application in stuck:
        session_id = application["vendor_applicant_id"]
        try:
            result = client.fetch_session(session_id)
        except VendorError as err:
            # One unreachable session must not abandon the rest of the batch.
            # The next sweep will try again, and nothing has been lost.
            unreachable += 1
            log.warning("sweep: could not ask about %s: %s", session_id, err)
            continue

        if result is None:
            # THE THIRD ANSWER. Recorded, counted, and eventually terminal.
            # Before 2026-10-14 this branch logged and did nothing else, which
            # is how 38 applications stayed in flight for three weeks.
            not_found += 1
            log.warning(
                "sweep: vendor has no record of session %s (application %s)",
                session_id,
                application["id"],
            )
            _record_not_found(conn, application["id"], session_id)
            if _close_if_confirmed_missing(conn, application["id"], session_id):
                closed += 1
            continue

        # Exactly the same function the webhook path uses. The sweeper is a
        # different way of LEARNING a result, not a different way of applying
        # one — so the state machine and the recency guard apply identically,
        # and a result the sweeper finds cannot do anything a webhook could not.
        evaluation = apply_result(
            conn,
            application=application,
            result=result,
            result_at=result.result_at,
            source="sweeper",
        )
        if evaluation.should_write:
            applied += 1

    expired = expire_abandoned(conn)

    log.info(
        "sweep: %d examined, %d applied, %d unreachable, %d not found, "
        "%d closed, %d expired",
        len(stuck),
        applied,
        unreachable,
        not_found,
        closed,
        expired,
    )

    # ALWAYS, not only when something happened.
    #
    # This used to be written `if applied or unreachable`, which meant the
    # commonest outcome — every session examined came back "no record" — wrote
    # nothing at all. From outside, a sweeper working correctly was
    # indistinguishable from one that had never run: three weeks of sweeps left
    # exactly zero rows. That is the same defect this project has recorded three
    # times elsewhere, a thing that looks like a check and is not one, and the
    # health page now reads this row to show when the last sweep happened.
    #
    # A row per sweep is cheap. Not being able to prove the sweeper ran is not.
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into audit_events (application_id, actor, action, details)
            values (null, 'system:worker', 'sweep.completed', %s)
            """,
            (
                Jsonb(
                    {
                        "examined": len(stuck),
                        "applied": applied,
                        "unreachable": unreachable,
                        "not_found": not_found,
                        "closed_verification_unavailable": closed,
                        "expired_abandoned": expired,
                        "stuck_minutes": SWEEP_STUCK_MINUTES,
                        "batch_size": SWEEP_BATCH_SIZE,
                    }
                ),
            ),
        )

    _schedule_next(conn)
