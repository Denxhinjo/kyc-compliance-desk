"""Queue retention: the loose end left open in Phase 2.

Phase 2 was honest that a table used as a queue accumulates rows and that
nothing was cleaning them up. This is the cleanup, and the interesting part is
what it refuses to delete.

    done    deleted after JOB_RETENTION_DAYS. These are the overwhelming
            majority of the table and carry no information once the work they
            describe has landed in applications, decisions and audit_events.

    parked  NEVER deleted. Parked is the dead letter queue — the only record
            that a piece of work failed and was given up on. Deleting it
            deletes the evidence that something went wrong, which is the exact
            opposite of why it exists. Parked jobs leave when a human deals
            with them, not when a timer fires.

    queued  not touched, obviously.
    running not touched — the stale-job reaper owns those.

And the contrast worth drawing: the job queue is operational plumbing and has a
retention policy. audit_events is evidence and has none. One is machinery, the
other is the record of what the machinery did, and only one of them is allowed
to forget.

THE HONEST COST

DELETE does not reclaim space immediately: it leaves dead tuples for vacuum to
clean up, and a queue that churns hard enough will spend real effort on that.
At very high throughput the answer is a partitioned table and DROP PARTITION,
which is an O(1) metadata operation rather than a row-by-row delete. At this
scale DELETE is correct, and saying when it would stop being correct is more
useful than implying it always is.
"""

from __future__ import annotations

import logging

import psycopg

from config import JOB_CLEANUP_INTERVAL_HOURS, JOB_RETENTION_DAYS
from handlers import handler
from jobs import Job

log = logging.getLogger("worker.retention")

JOB_TYPE = "jobs.cleanup"

#: Deleted per run. A cap so one cleanup cannot hold a worker for minutes after
#: a backlog, and cannot produce a single enormous transaction.
BATCH_SIZE = 5_000


def ensure_scheduled(conn: psycopg.Connection) -> bool:
    """Put one cleanup on the queue if there is not one already.

    Every worker calls this at startup; the partial unique index from migration
    016 means only the first insert succeeds and the rest are a no-op.
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
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, payload, run_after)
            values (%s, '{}'::jsonb, now() + make_interval(secs => %s))
            on conflict do nothing
            """,
            (JOB_TYPE, JOB_CLEANUP_INTERVAL_HOURS * 3600),
        )


@handler(JOB_TYPE)
def cleanup(conn: psycopg.Connection, job: Job) -> None:
    with conn.cursor() as cur:
        # Explicitly status = 'done'. Not "everything older than N", which would
        # eventually eat the parked jobs nobody had got round to yet.
        #
        # The subquery with a LIMIT keeps each statement bounded; a bare DELETE
        # over a large backlog would lock and log far more than necessary.
        cur.execute(
            """
            delete from jobs
             where id in (
                   select id from jobs
                    where status = 'done'
                      and completed_at < now() - make_interval(secs => %s)
                    limit %s
             )
            """,
            (JOB_RETENTION_DAYS * 86400, BATCH_SIZE),
        )
        deleted = cur.rowcount

    with conn.cursor() as cur:
        cur.execute(
            "select status, count(*) from jobs group by status order by status"
        )
        remaining = {row[0]: row[1] for row in cur.fetchall()}

    log.info(
        "retention: deleted %d done job(s) older than %.0f days; remaining %s",
        deleted,
        JOB_RETENTION_DAYS,
        remaining or "{}",
    )

    parked = remaining.get("parked", 0)
    if parked:
        # Said out loud on every run. A growing parked count is the queue
        # telling you something is broken and nobody has looked.
        log.warning(
            "retention: %d parked job(s) kept — these need a human, not a timer",
            parked,
        )

    _schedule_next(conn)
