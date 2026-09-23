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
import result_handler  # noqa: F401
import screening_handler  # noqa: F401
import sweeper  # noqa: F401
import retention  # noqa: F401
from config import (
    POLL_SECONDS,
    REAP_INTERVAL_SECONDS,
    SANCTIONS_FALLBACK,
    SANCTIONS_SOURCE,
    STALE_SECONDS,
    SWEEP_BATCH_SIZE,
    VENDOR_TIMEOUT_SECONDS,
    WORKER_ID,
)
from db import Database
from handlers import PermanentError, get_handler
from jobs import Job, claim_job, claim_job_naively, reclaim_stale_jobs
# Re-exported: tests and drain.py both reach for run_job, and it lives in
# runner.py so that neither entry point owns it. See runner.py's docstring.
from runner import run_job  # noqa: F401
from db import connect as db_check
from screening.store import active_snapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

_shutdown = False


def _resident_mb() -> float | None:
    """This process's RSS in MB, or None where it cannot be read.

    No dependency: /proc on Linux, which is what Heroku runs. Logged at boot so
    the number that matters on a 512MB Eco dyno — R14 starts swapping there — is
    visible in `heroku logs` rather than something anyone has to go and measure.
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _check_timeouts() -> None:
    """Complain if the reaper could reclaim a job that is still running.

    The longest legitimate job is a sweep against an unresponsive vendor, and
    its worst case is a simple product. If that ever reaches STALE_SECONDS the
    reaper starts handing live sweeps to other workers, which is the exact
    double-processing the whole queue design exists to prevent.

    Checked at boot rather than left as a comment because it is a relationship
    between three environment variables, and any one of them can be changed on
    Heroku by someone who has not read this file.
    """
    worst_case = SWEEP_BATCH_SIZE * VENDOR_TIMEOUT_SECONDS
    if worst_case >= STALE_SECONDS:
        log.error(
            "MISCONFIGURED: a sweep can take up to %.0fs (SWEEP_BATCH_SIZE %d x "
            "VENDOR_TIMEOUT_SECONDS %.0fs) but JOB_STALE_SECONDS is %.0fs. The "
            "reaper will reclaim sweeps that are still running and they will be "
            "processed twice. Raise JOB_STALE_SECONDS or lower SWEEP_BATCH_SIZE.",
            worst_case, SWEEP_BATCH_SIZE, VENDOR_TIMEOUT_SECONDS, STALE_SECONDS,
        )
    else:
        log.info(
            "timeouts: worst-case job %.0fs, stale after %.0fs (%.1fx margin), "
            "reaped every %.0fs",
            worst_case, STALE_SECONDS, STALE_SECONDS / worst_case,
            REAP_INTERVAL_SECONDS,
        )


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    global _shutdown
    _shutdown = True
    log.info("signal %s received — finishing current job, then stopping", signum)


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

    _check_timeouts()

    # Load the sanctions list BEFORE claiming anything. Deliberately not wrapped
    # in a try: a worker that cannot load its list would claim screening jobs and
    # fail every one, and a crash loop is far easier to notice than a worker
    # quietly parking everything it touches. See screening/sources.preload.
    # The list is in Postgres now, so there is nothing to load into memory —
    # this only checks that a searchable version exists. The worker used to
    # spend 86MB and fifteen seconds here.
    #
    # Refusing to start is deliberate. A worker with no list would claim every
    # screening job and park it, and a queue full of parked jobs is a far
    # quieter failure than a process that will not boot.
    with db_check() as conn:
        snapshot = active_snapshot(conn, SANCTIONS_SOURCE)
    if snapshot is None:
        log.error(
            "no fully-loaded %r sanctions snapshot. Run: python load_sanctions.py",
            SANCTIONS_SOURCE,
        )
        return 1
    resident = _resident_mb()
    log.info(
        "sanctions list ready: %s entries from %r, published %s%s",
        f"{snapshot.record_count:,}",
        snapshot.source,
        snapshot.published_at or "unknown",
        f" (RSS {resident:.0f}MB)" if resident is not None else "",
    )

    db = Database()

    # Make sure the recurring sweep exists. Every worker does this at startup;
    # the partial unique index means only the first one actually inserts.
    try:
        conn = db.connection()
        if sweeper.ensure_scheduled(conn):
            log.info("scheduled the recurring %s job", sweeper.JOB_TYPE)
        if retention.ensure_scheduled(conn):
            log.info("scheduled the recurring %s job", retention.JOB_TYPE)
    except psycopg.Error as err:
        log.error("could not schedule the recurring jobs: %s", err)

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
