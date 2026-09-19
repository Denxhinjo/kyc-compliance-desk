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
#
# THIS NUMBER MUST EXCEED THE LONGEST A LEGITIMATE JOB CAN TAKE. Set it too low
# and the reaper steals work that is still running, producing exactly the
# double-processing the queue exists to prevent — a worse failure than the one
# it fixes.
#
# The longest legitimate job is a sweep against an unresponsive vendor:
#
#     SWEEP_BATCH_SIZE (25) x VENDOR_TIMEOUT_SECONDS (10) = 250s worst case
#
# 600 leaves a 2.4x margin. Raised from 300, which did NOT clear the old batch
# size of 50 — that combination allowed a 500s sweep against a 300s threshold,
# and would have had the reaper reclaiming a live sweep after an outage. Found
# while working out what a Heroku dyno restart does; see docs/decisions.md.
#
# The cost of the larger number is recovery latency: a job killed by SIGKILL
# waits up to STALE_SECONDS + REAP_INTERVAL_SECONDS before being requeued.
# Correctness wins over latency here, and the value is env-tunable for demos.
STALE_SECONDS = _float("JOB_STALE_SECONDS", "600")

# How often to look for those. Runs on a timer inside the worker loop, NOT only
# at startup, so a job orphaned by a dyno restart is rescued by the workers that
# are already running rather than waiting for the next deploy.
#
# 60s is a deliberate compromise: short enough that it contributes little to
# recovery latency next to STALE_SECONDS (10% of it), long enough that the extra
# query is irrelevant — one UPDATE a minute per worker, against an index on
# (status, locked_at).
REAP_INTERVAL_SECONDS = _float("JOB_REAP_INTERVAL_SECONDS", "60")

DEFAULT_MAX_ATTEMPTS = _int("JOB_MAX_ATTEMPTS", "5")

# --- Vendor -----------------------------------------------------------------

# 'simulator' or 'live'. The worker reads the same variable the web service
# does, so the two halves of the integration can never disagree about which
# vendor they are talking to.
DIDIT_MODE = os.environ.get("DIDIT_MODE") or "simulator"
DIDIT_BASE_URL = os.environ.get("DIDIT_BASE_URL") or "https://verification.didit.me"
DIDIT_API_KEY = os.environ.get("DIDIT_API_KEY") or ""
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL") or "http://localhost:3001"

# A vendor that hangs must not hold a worker forever. Shorter than
# JOB_STALE_SECONDS by a wide margin, so a slow vendor produces a retry rather
# than a job the reaper has to rescue.
VENDOR_TIMEOUT_SECONDS = _float("VENDOR_TIMEOUT_SECONDS", "10")

# --- Sweeper ----------------------------------------------------------------

# How often the sweeper runs.
SWEEP_INTERVAL_MINUTES = _float("SWEEP_INTERVAL_MINUTES", "15")

# How long an application may sit waiting on the vendor before the sweeper goes
# and asks directly. Must exceed the vendor's own retry schedule (Didit retries
# at roughly one and four minutes), or the sweeper races deliveries that are
# still in flight and does work the webhook was about to do anyway.
SWEEP_STUCK_MINUTES = _float("SWEEP_STUCK_MINUTES", "10")

# Applications examined per sweep. A cap so one sweep cannot monopolise a worker
# after an outage has left thousands stuck.
#
# Bounded by STALE_SECONDS, not chosen freely: this number times
# VENDOR_TIMEOUT_SECONDS is the worst-case duration of a sweep, and that has to
# stay comfortably under the threshold at which the reaper presumes a job dead.
# main.py checks the relationship at boot and complains if it stops holding.
SWEEP_BATCH_SIZE = _int("SWEEP_BATCH_SIZE", "25")

# --- Screening --------------------------------------------------------------

# 'synthetic' | 'ofac' | 'opensanctions'.
#
# synthetic ships in the repository so a fresh clone works offline. ofac is the
# real US Treasury SDN list, a US Government work and therefore public domain.
# opensanctions is richer but CC-BY-NC, so commercial use needs a licence and
# it is deliberately not the default.
SANCTIONS_SOURCE = os.environ.get("SANCTIONS_SOURCE") or "synthetic"

# What to load if SANCTIONS_SOURCE cannot be loaded at boot — empty means "do
# not start", which is the right default for development, where a missing list
# is a mistake you want to see immediately.
#
# Production sets this to 'synthetic' so an unreachable treasury.gov degrades
# the worker instead of crash-looping it. That is a deliberate trade and a bad
# one to make silently, so preload() logs it at ERROR and every match recorded
# while degraded names the fixture as its source in sanctions_snapshots.
SANCTIONS_FALLBACK = os.environ.get("SANCTIONS_FALLBACK") or ""

# --- Queue retention (the loose end from Phase 2) ---------------------------

# How long a completed job is kept. Only 'done' jobs are ever deleted; parked
# ones are the dead letter queue and stay until a human deals with them.
JOB_RETENTION_DAYS = _float("JOB_RETENTION_DAYS", "7")
JOB_CLEANUP_INTERVAL_HOURS = _float("JOB_CLEANUP_INTERVAL_HOURS", "6")
