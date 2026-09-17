"""The queue mechanics: claim, complete, fail, reclaim.

This module is the architectural centrepiece of the project. Read CLAIM_SQL
first; everything else is bookkeeping around it.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from config import BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS


@dataclass(frozen=True)
class Job:
    id: int
    job_type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------

CLAIM_SQL = """
with claimed as (
    select id
      from jobs
     where status = 'queued'
       and run_after <= now()
     order by run_after, id
     for update skip locked
     limit 1
)
update jobs j
   set status    = 'running',
       attempts  = j.attempts + 1,
       locked_at = now(),
       locked_by = %s
  from claimed c
 where j.id = c.id
returning j.id, j.job_type, j.payload, j.attempts, j.max_attempts
"""
"""Take exactly one job, safely, with any number of workers competing.

FOR UPDATE takes a row-level lock. Without SKIP LOCKED, a second worker
reaching the same row would BLOCK until the first committed — every worker
queued behind one row, which is the opposite of what a pool of workers is for.

SKIP LOCKED changes one thing: a locked row is treated as if it were not there.
Worker B reaches job 7, sees it locked, and walks straight on to job 8. Three
workers hitting this simultaneously get three different jobs and none of them
waits.

What makes it airtight is that the exclusion is enforced by Postgres' lock
manager, not by application logic. The naive version —

    select id from jobs where status = 'queued' limit 1;   -- both see job 7
    update jobs set status = 'running' where id = 7;       -- both proceed

— has a window between the two statements in which both workers believe they
own job 7. It works on a laptop and fails in production at 3am. See
NAIVE_SELECT_SQL below, which exists only to demonstrate that failure.

The LIMIT 1 sits inside the locking sub-select on purpose: SKIP LOCKED is
applied while scanning, so Postgres keeps going past locked rows until it finds
a free one. You get *a* job, not "no job, because the first one was busy".

attempts is incremented HERE, at claim time, not at failure time — see the
comment in 008_jobs.sql for why that matters.
"""


def claim_job(conn: psycopg.Connection, worker_id: str) -> Job | None:
    """Claim one job, or return None if there is nothing to do."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(CLAIM_SQL, (worker_id,))
        row = cur.fetchone()
    return _to_job(row) if row else None


# --- the deliberately broken version, for demonstration only ----------------

NAIVE_SELECT_SQL = """
select id, job_type, payload, attempts, max_attempts
  from jobs
 where status = 'queued'
   and run_after <= now()
 order by run_after, id
 limit 1
"""

NAIVE_UPDATE_SQL = """
update jobs
   set status = 'running', attempts = attempts + 1,
       locked_at = now(), locked_by = %s
 where id = %s
"""


def claim_job_naively(
    conn: psycopg.Connection, worker_id: str, race_window_seconds: float = 0.01
) -> Job | None:
    """Claim a job WITHOUT locking. This is the bug. Do not copy it.

    Exists so the race condition can be demonstrated rather than merely
    asserted. Two workers running this will both read the same row and both
    process it.

    race_window_seconds widens the gap between the SELECT and the UPDATE so the
    race shows up reliably in a short demo. The window is not created by the
    sleep — it exists in any read-then-write without a lock, and under real load
    it is eventually hit. The sleep only makes a rare event reproducible.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(NAIVE_SELECT_SQL)
        row = cur.fetchone()
        if row is None:
            return None

        time.sleep(race_window_seconds)

        cur.execute(NAIVE_UPDATE_SQL, (worker_id, row["id"]))

    job = _to_job(row)
    # attempts was read before the update incremented it.
    return Job(
        id=job.id,
        job_type=job.job_type,
        payload=job.payload,
        attempts=job.attempts + 1,
        max_attempts=job.max_attempts,
    )


def _to_job(row: dict[str, Any]) -> Job:
    return Job(
        id=row["id"],
        job_type=row["job_type"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
    )


# ---------------------------------------------------------------------------
# Finishing
# ---------------------------------------------------------------------------


def complete_job(conn: psycopg.Connection, job_id: int) -> None:
    """Mark a job done.

    Called INSIDE the same transaction as the handler's database writes, so the
    work and the record of the work commit together. A crash between them would
    otherwise re-run the handler on a job whose effects had already landed.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update jobs
               set status = 'done', completed_at = now(), last_error = null,
                   locked_at = null, locked_by = null
             where id = %s
            """,
            (job_id,),
        )


def backoff_seconds(attempts: int) -> float:
    """How long to wait before retrying, after `attempts` failures.

    Exponential, because if a vendor's API is down then retrying every second
    makes their outage worse and yours longer.

    Jittered, because without it a hundred jobs that failed during the same
    outage all retry at the same instant when it ends — a thundering herd that
    can knock over the service that had just recovered. This is "equal jitter":
    half the delay is fixed (so backoff still grows) and half is random (so the
    herd spreads out).
    """
    raw = min(BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), BACKOFF_CAP_SECONDS)
    return raw / 2 + random.uniform(0, raw / 2)


def fail_job(
    conn: psycopg.Connection, job: Job, error: str, *, permanent: bool = False
) -> tuple[str, float | None]:
    """Record a failure: schedule a retry, or park the job.

    Returns (new_status, delay_seconds) for logging.

    Deliberately NOT inside the handler's transaction — that one has already
    rolled back. The connection is in autocommit mode, so this is a single
    self-contained statement that survives whatever just went wrong.
    """
    exhausted = job.attempts >= job.max_attempts
    if permanent or exhausted:
        reason = "permanent error" if permanent else "attempts exhausted"
        with conn.cursor() as cur:
            cur.execute(
                """
                update jobs
                   set status = 'parked', last_error = %s,
                       locked_at = null, locked_by = null
                 where id = %s
                """,
                (f"[{reason}] {error}", job.id),
            )
        return "parked", None

    delay = backoff_seconds(job.attempts)
    with conn.cursor() as cur:
        cur.execute(
            """
            update jobs
               set status = 'queued',
                   run_after = now() + make_interval(secs => %s),
                   last_error = %s,
                   locked_at = null, locked_by = null
             where id = %s
            """,
            (delay, error, job.id),
        )
    return "queued", delay


# ---------------------------------------------------------------------------
# Reclaiming abandoned work
# ---------------------------------------------------------------------------

RECLAIM_SQL = """
update jobs
   set status = case when attempts >= max_attempts then 'parked' else 'queued' end,
       locked_at = null,
       locked_by = null,
       last_error = %s
 where status = 'running'
   and locked_at < now() - make_interval(secs => %s)
returning id, status
"""


def reclaim_stale_jobs(
    conn: psycopg.Connection, stale_after_seconds: float
) -> list[tuple[int, str]]:
    """Rescue jobs whose worker died mid-flight.

    A worker that is killed outright — OOM, segfault, pulled power — never runs
    a failure handler, so its job stays 'running' forever and nothing else will
    ever claim it. Without this, the retry design has no ending.

    A reclaimed job whose attempts are already exhausted goes straight to
    parked rather than back into the queue, so a job that reliably kills
    workers cannot loop through them indefinitely.

    The timeout is why STALE_SECONDS must exceed the longest a legitimate job
    can take: set it too low and this reclaims work that is still running,
    producing exactly the double-processing the queue exists to prevent.
    """
    with conn.cursor() as cur:
        cur.execute(
            RECLAIM_SQL,
            (
                "reclaimed: worker did not finish within the stale timeout",
                stale_after_seconds,
            ),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def queue_depth(conn: psycopg.Connection) -> dict[str, int]:
    """Counts by status, for logging and for the drain check."""
    with conn.cursor() as cur:
        cur.execute("select status, count(*) from jobs group by status")
        return {row[0]: row[1] for row in cur.fetchall()}


def enqueue_job(
    conn: psycopg.Connection,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    run_after_seconds: float = 0.0,
) -> int:
    """Put a job on the queue from inside the worker.

    The Python twin of enqueueJob() in web/src/lib/jobs.ts, and it carries the
    same obligation: it does NOT commit. The caller owns the transaction, so a
    job is enqueued only if the state change that warranted it also commits.
    One service should not be able to create work for a row that was rolled
    back.
    """
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, payload, run_after)
            values (%s, %s, now() + make_interval(secs => %s))
            returning id
            """,
            (job_type, Jsonb(payload or {}), run_after_seconds),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]
