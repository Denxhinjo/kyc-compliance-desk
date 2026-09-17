"""Worker tuning, read once from the root .env.

Kept in one module so the numbers that govern retry behaviour are visible in a
single place rather than scattered as literals through the code.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _float(name: str, default: str) -> float:
    return float(os.environ.get(name) or default)


def _int(name: str, default: str) -> int:
    return int(os.environ.get(name) or default)


# Identifies this worker in jobs.locked_by and in audit events. Host plus pid,
# so two workers on the same machine are still distinguishable — which is
# exactly what the no-double-processing proof depends on.
WORKER_ID = os.environ.get("WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"

# How long to wait before polling again when the queue is empty. This is the
# cost of polling rather than using LISTEN/NOTIFY: a job can sit for up to this
# long before anyone notices it.
POLL_SECONDS = _float("WORKER_POLL_SECONDS", "1")

# Exponential backoff: base * 2^(attempts-1), capped, with jitter.
# 5s, 10s, 20s, 40s, 80s ...
BACKOFF_BASE_SECONDS = _float("JOB_BACKOFF_BASE_SECONDS", "5")
BACKOFF_CAP_SECONDS = _float("JOB_BACKOFF_CAP_SECONDS", "3600")

# A job 'running' longer than this is presumed abandoned — its worker died
# without getting to a failure handler — and is reclaimed.
STALE_SECONDS = _float("JOB_STALE_SECONDS", "300")

# How often to look for those.
REAP_INTERVAL_SECONDS = _float("JOB_REAP_INTERVAL_SECONDS", "60")

DEFAULT_MAX_ATTEMPTS = _int("JOB_MAX_ATTEMPTS", "5")
