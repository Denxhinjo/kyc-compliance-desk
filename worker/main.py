"""Worker entry point.

Phase 0 scope: prove the Python service can reach the same Postgres the web
app uses, and keep proving it on a timer. From Phase 2 this same loop becomes
the job-queue consumer; the shape is intentionally already right.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from types import FrameType

import psycopg

from db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

HEARTBEAT_SECONDS = int(os.environ.get("WORKER_HEARTBEAT_SECONDS", "10"))

# Flipped by SIGINT/SIGTERM so the loop can finish what it is doing and exit
# cleanly. In Phase 2 this is what stops a worker from being killed midway
# through a claimed job and leaving it stuck.
_shutdown = False


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    global _shutdown
    _shutdown = True
    log.info("signal %s received — shutting down after this beat", signum)


def main() -> int:
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    log.info("worker starting — heartbeat every %ss", HEARTBEAT_SECONDS)
    db = Database()
    beat = 0

    while not _shutdown:
        beat += 1
        try:
            server_time, database, backend_pid = db.fetch_server_time()
            log.info(
                "heartbeat %d  db=%s  backend_pid=%s  db_time=%s  ok",
                beat,
                database,
                backend_pid,
                server_time,
            )
        except psycopg.OperationalError as err:
            # An unreachable or dropped connection is expected and recoverable:
            # Postgres may be restarting. Throw the connection away, log it, and
            # try again on the next beat rather than crashing the process.
            db.discard()
            log.error("heartbeat %d  database unreachable: %s", beat, str(err).strip())
        except Exception:
            db.discard()
            log.exception("heartbeat %d  unexpected error", beat)

        # Sleep in short slices so Ctrl-C is felt immediately instead of after
        # a full heartbeat interval.
        for _ in range(HEARTBEAT_SECONDS * 10):
            if _shutdown:
                break
            time.sleep(0.1)

    db.close()
    log.info("worker stopped cleanly after %d beats", beat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
