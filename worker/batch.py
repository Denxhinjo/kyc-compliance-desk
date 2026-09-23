"""Work a BOUNDED batch of jobs and return. The second entry point.

NAMED batch.py, NOT drain.py, and that is not cosmetic. Vercel derives a route
from a filename, so the endpoint must be `api/drain.py`; if this were also
`drain.py` then both would be the module `drain` on sys.path and the endpoint
would import whichever won — quite possibly itself. A collision that would only
show up once deployed.

WHY THIS EXISTS AND main.py STILL DOES TOO

`main.py` loops: claim, run, sleep, repeat, for as long as the process lives.
That is the right shape for a machine you rent by the month, and it is not
available to a function you rent by the invocation — an on-demand function has
a wall-clock limit measured in seconds and is killed mid-sentence when it is
reached.

So this claims at most N jobs, runs them, and returns. It is called repeatedly
instead of looping: once by the web app the moment work is enqueued, so an
applicant sees a decision in seconds, and every few minutes by a scheduler, so
that nothing is left behind if that call is lost.

BOUNDED IS THE WHOLE POINT, and the bound is small on purpose. Five jobs at a
few hundred milliseconds each is comfortably inside any function limit with
room for the slowest job in the system. Raising it trades safety margin for
fewer invocations, and invocations are the cheap thing.

WHAT DOES NOT CHANGE

Queue semantics. Jobs are still claimed one at a time with FOR UPDATE SKIP
LOCKED, still run through `runner.run_job`, still retried and parked by the
same accounting. Two drains racing each other behave exactly like two workers
racing each other, because they are doing the identical thing — which is why
the concurrency tests did not need a line changed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass

import psycopg

# Importing these registers their handlers. The import IS the use, exactly as
# in main.py — a drain that has not imported the handlers claims jobs it cannot
# run and parks every one of them.
import handlers  # noqa: F401
import result_handler  # noqa: F401
import retention  # noqa: F401
import screening_handler  # noqa: F401
import sweeper  # noqa: F401
from config import STALE_SECONDS
from jobs import claim_job
from runner import run_job

log = logging.getLogger("worker.drain")

#: Jobs claimed per call. See the note above on why this is small.
DEFAULT_BATCH = 5

#: Stop claiming when this much of the budget is gone, even if the batch is not
#: full. A job that takes longer than expected must not be started at second 55
#: of a 60-second function — it would be killed mid-flight, and although the
#: reaper would eventually rescue it, "eventually" is minutes away and the
#: applicant is watching a status page now.
DEFAULT_BUDGET_SECONDS = 25.0


@dataclass
class DrainResult:
    claimed: int
    reclaimed: int
    remaining: int
    elapsed_ms: int
    #: True when the batch filled up or the budget ran out — i.e. there is
    #: almost certainly more to do and the caller should come straight back.
    more: bool

    def as_dict(self) -> dict:
        return asdict(self)


def drain_once(
    conn: psycopg.Connection,
    *,
    limit: int = DEFAULT_BATCH,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    reap: bool = True,
) -> DrainResult:
    """Claim up to `limit` jobs, run them, and say what happened.

    `reap` defaults on because in production nothing else does it. The loop in
    main.py reclaims abandoned jobs on a timer; a function has no timer, so the
    reap rides along with the drain. It is cheap — one indexed UPDATE — and
    skipping it would mean a job orphaned by a killed function stays 'running'
    until a human notices.
    """
    started = time.monotonic()
    reclaimed = 0

    if reap:
        for job_id, new_status in reclaim_stale(conn):
            reclaimed += 1
            log.warning("reclaimed stale job %s -> %s", job_id, new_status)

    claimed = 0
    hit_budget = False
    while claimed < limit:
        if time.monotonic() - started >= budget_seconds:
            hit_budget = True
            log.info("drain stopping early: %.1fs budget spent", budget_seconds)
            break

        job = claim_job(conn, worker_id())
        if job is None:
            break

        claimed += 1
        run_job(conn, job)

    remaining = claimable_now(conn)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    result = DrainResult(
        claimed=claimed,
        reclaimed=reclaimed,
        remaining=remaining,
        elapsed_ms=elapsed_ms,
        # A full batch means the limit stopped us, not an empty queue.
        more=hit_budget or claimed >= limit or remaining > 0,
    )
    log.info(
        "drain: %d claimed, %d reclaimed, %d still queued, %dms",
        claimed, reclaimed, remaining, elapsed_ms,
    )
    return result


def claimable_now(conn: psycopg.Connection) -> int:
    """Jobs that could be claimed RIGHT NOW, not every queued row.

    `queue_depth` counts by status, which includes work deliberately scheduled
    for later: the sweeper and the retention cleanup are always sitting in
    'queued' with a `run_after` minutes or hours ahead.

    Counting those made `more` permanently true, which is not a cosmetic
    difference — the cron loop calls the endpoint again for as long as `more`
    says there is work, so every scheduled run would make its full twelve calls
    and claim nothing on eleven of them. Observed in production on the first
    deploy: remaining sat at 2 forever with claimed at 0.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) from jobs
             where status = 'queued' and run_after <= now()
            """
        )
        return cur.fetchone()[0]


def reclaim_stale(conn: psycopg.Connection):
    from jobs import reclaim_stale_jobs

    return reclaim_stale_jobs(conn, STALE_SECONDS)


def worker_id() -> str:
    """Who claimed the job, for jobs.locked_by and the audit trail.

    A function invocation has no stable identity — no hostname worth recording,
    no pid that means anything after it exits. So this records what it honestly
    is: a drain, at a moment. Enough to tell two concurrent drains apart in
    `pg_stat_activity` and in `locked_by`, which is all locked_by is for.
    """
    import os
    import socket

    explicit = os.environ.get("WORKER_ID")
    if explicit:
        return explicit
    return f"drain:{socket.gethostname()}:{os.getpid()}"


def ensure_recurring(conn: psycopg.Connection) -> None:
    """Make sure the sweeper and the cleanup are on the queue.

    main.py does this at startup. A function has no startup, so it is done here
    — and it is safe to call on every drain because both use a partial unique
    index over queued rows: the second insert is a no-op, not an error.
    """
    import retention
    import sweeper

    if sweeper.ensure_scheduled(conn):
        log.info("scheduled the recurring %s job", sweeper.JOB_TYPE)
    if retention.ensure_scheduled(conn):
        log.info("scheduled the recurring %s job", retention.JOB_TYPE)
