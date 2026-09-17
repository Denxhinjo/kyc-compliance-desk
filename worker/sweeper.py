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
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config import (
    SWEEP_BATCH_SIZE,
    SWEEP_INTERVAL_MINUTES,
    SWEEP_STUCK_MINUTES,
)
from handlers import handler
from jobs import Job
from result_handler import apply_result
from vendor import VendorError, get_client

log = logging.getLogger("worker.sweeper")

JOB_TYPE = "vendor.sweep_stuck"


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
    to ask about. Chasing those is a different job (a reminder email), not this.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select id, status, vendor_status, vendor_result_at, vendor_applicant_id
              from applications
             where status in ('submitted', 'checking')
               and vendor_applicant_id is not null
               and updated_at < now() - make_interval(secs => %s)
             order by updated_at
             limit %s
            """,
            (SWEEP_STUCK_MINUTES * 60, SWEEP_BATCH_SIZE),
        )
        return list(cur.fetchall())


@handler(JOB_TYPE)
def sweep_stuck(conn: psycopg.Connection, job: Job) -> None:
    stuck = _stuck_applications(conn)

    if not stuck:
        log.info("sweep: nothing waiting longer than %.0f minutes", SWEEP_STUCK_MINUTES)
        _schedule_next(conn)
        return

    log.info("sweep: %d application(s) waiting on the vendor", len(stuck))
    client = get_client()
    applied = 0
    unreachable = 0

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
            log.warning(
                "sweep: vendor has no record of session %s (application %s)",
                session_id,
                application["id"],
            )
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

    log.info(
        "sweep: %d applied, %d unreachable, %d unchanged",
        applied,
        unreachable,
        len(stuck) - applied - unreachable,
    )

    if applied or unreachable:
        # Worth a row in the audit log: a sweep that had to fix something is
        # evidence that webhook delivery failed, and a run of these is how you
        # notice the vendor integration is quietly broken.
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
                            "stuck_minutes": SWEEP_STUCK_MINUTES,
                        }
                    ),
                ),
            )

    _schedule_next(conn)
