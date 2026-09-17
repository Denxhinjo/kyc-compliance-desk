"""Worker entry point: the job queue consumer.

    .venv/Scripts/python main.py                 run until stopped
    .venv/Scripts/python main.py --drain         exit once the queue is empty
    .venv/Scripts/python main.py --naive         claim WITHOUT locking (broken,
                                                 for demonstration only)

Replaces Phase 0's heartbeat. The shape of the loop is unchanged — claim work,
do it, record the outcome, sleep — but the work is now real.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from types import FrameType

import psycopg

# Importing these registers their handlers in the registry. The noqa silences
# "imported but unused" — the import IS the use.
import handlers  # noqa: F401
import didit_handler  # noqa: F401
from config import (
    POLL_SECONDS,
    REAP_INTERVAL_SECONDS,
    STALE_SECONDS,
    WORKER_ID,
)
from db import Database
from handlers import PermanentError, get_handler
from jobs import Job, claim_job, claim_job_naively, complete_job, fail_job, reclaim_stale_jobs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

_shutdown = False


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    global _shutdown
    _shutdown = True
    log.info("signal %s received — finishing current job, then stopping", signum)


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


def main() -> int:
    parser = argparse.ArgumentParser(description="KYC job queue worker")
    parser.add_argument(
        "--drain",
        action="store_true",
        help="exit once the queue has been empty three polls in a row",
    )
    parser.add_argument(
        "--naive",
        action="store_true",
        help="claim without FOR UPDATE SKIP LOCKED — broken on purpose, to "
             "demonstrate the race condition",
    )
    args = parser.parse_args()

    # signal is imported late so that --help works identically everywhere.
    import signal

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    if args.naive:
        log.warning("NAIVE CLAIM MODE — no row locking. Jobs will be processed twice.")
    log.info("worker %s starting (poll %.1fs)", WORKER_ID, POLL_SECONDS)

    db = Database()
    empty_polls = 0
    processed = 0
    last_reap = 0.0

    while not _shutdown:
        try:
            conn = db.connection()

            now = time.monotonic()
            if now - last_reap > REAP_INTERVAL_SECONDS:
                last_reap = now
                for job_id, new_status in reclaim_stale_jobs(conn, STALE_SECONDS):
                    log.warning("reclaimed stale job %s -> %s", job_id, new_status)

            job = (
                claim_job_naively(conn, WORKER_ID)
                if args.naive
                else claim_job(conn, WORKER_ID)
            )

            if job is None:
                empty_polls += 1
                if args.drain and empty_polls >= 3:
                    break
                # Sleep in slices so Ctrl-C is felt immediately rather than
                # after a full poll interval.
                for _ in range(max(1, int(POLL_SECONDS * 10))):
                    if _shutdown:
                        break
                    time.sleep(0.1)
                continue

            empty_polls = 0
            processed += 1
            run_job(conn, job)

        except psycopg.OperationalError as err:
            # The database went away. Expected and recoverable: throw the
            # connection out and try again on the next pass. Any job this
            # worker held stays 'running' until the reaper rescues it.
            db.discard()
            log.error("database unreachable: %s", str(err).strip().splitlines()[0])
            time.sleep(1)
        except Exception:
            log.critical("unexpected error in the worker loop:\n%s", traceback.format_exc())
            time.sleep(1)

    db.close()
    log.info("worker %s stopped after %d job(s)", WORKER_ID, processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
