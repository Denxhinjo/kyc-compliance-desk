"""Executing jobs. The half of the worker that both entry points share.

There are two ways into this code now and they are genuinely different
runtimes, not a wrapper and a real thing:

    main.py     a long-lived loop. Local docker-compose, and any host that
                will run a process for weeks. Claims, runs, sleeps, repeats.

    drain.py    a bounded batch, called from outside and returning when it is
                done. An on-demand function has a duration limit measured in
                seconds, so "loop until empty" is not available to it.

What must NOT differ is what happens to a job once it has been claimed —
the transaction boundary, the retry accounting, what counts as permanent. So
that lives here, is imported by both, and neither gets to have its own opinion
about it.

The alternative was for drain.py to import run_job from main.py, which works
and reads like an accident: an HTTP handler reaching into a CLI entry point for
its core logic. Naming the shared part is most of the value of extracting it.
"""

from __future__ import annotations

import logging
import time

import psycopg

from handlers import PermanentError, get_handler
from jobs import Job, complete_job, fail_job

log = logging.getLogger("worker.runner")


def run_job(conn: psycopg.Connection, job: Job) -> None:
    """Execute one claimed job and record how it went.

    The handler's database writes and the 'done' update share ONE transaction,
    so a job cannot be marked finished unless its effects committed, and its
    effects cannot commit without the job being marked finished.

    What this does NOT protect is external I/O. A handler that calls a vendor
    API and then fails to commit will call that API again on the retry, because
    an HTTP request cannot be rolled back. That is not a gap in this code — it
    is what distributed systems are. You get AT-LEAST-ONCE delivery, and the
    obligation that follows is that handlers must be idempotent: safe to run
    twice. The same word, and the same idea, as the webhook handling in Phase 3.
    """
    started = time.monotonic()
    try:
        handler = get_handler(job.job_type)
        with conn.transaction():
            handler(conn, job)
            complete_job(conn, job.id)
    except PermanentError as err:
        fail_job(conn, job, str(err), permanent=True)
        log.error("job %s %s PARKED (permanent): %s", job.id, job.job_type, err)
    except Exception as err:
        # Keep the traceback in the log for a human, but store only the message
        # on the row — last_error is read in a list view, not a debugger.
        log.debug("job %s failed", job.id, exc_info=True)
        status, delay = fail_job(conn, job, f"{type(err).__name__}: {err}")
        if status == "parked":
            log.error(
                "job %s %s PARKED after %s/%s attempts: %s",
                job.id, job.job_type, job.attempts, job.max_attempts, err,
            )
        else:
            log.warning(
                "job %s %s failed (attempt %s/%s), retrying in %.1fs: %s",
                job.id, job.job_type, job.attempts, job.max_attempts, delay, err,
            )
    else:
        elapsed = (time.monotonic() - started) * 1000
        log.info(
            "job %s %s done (attempt %s, %.0fms)",
            job.id, job.job_type, job.attempts, elapsed,
        )
