"""Job handlers, and the registry that maps a job_type to one.

A handler receives an open connection and the job, does its work, and either
returns (success) or raises (failure). It must NOT commit: the caller wraps the
handler and the "mark done" update in a single transaction so that the work and
the record of the work land together.

The demo handlers here exist to exercise the queue in Phase 2. Real ones —
fetching vendor results, running screening — arrive in Phases 4 and 5.
"""

from __future__ import annotations

from typing import Callable

import psycopg

from audit import log_event
from config import WORKER_ID
from jobs import Job

Handler = Callable[[psycopg.Connection, Job], None]

_HANDLERS: dict[str, Handler] = {}


class PermanentError(Exception):
    """Raised by a handler when retrying cannot possibly help.

    A malformed payload, an unknown job type, a vendor saying "this applicant
    does not exist". Parks the job immediately instead of burning five attempts
    and an hour of backoff on something that will never succeed.
    """


def handler(job_type: str) -> Callable[[Handler], Handler]:
    """Register a function as the handler for a job type.

    Used as a decorator. The registry is a plain dict rather than anything
    cleverer because there are four handlers and there is no problem here that
    a dict does not solve.
    """

    def register(fn: Handler) -> Handler:
        if job_type in _HANDLERS:
            raise RuntimeError(f"two handlers registered for {job_type!r}")
        _HANDLERS[job_type] = fn
        return fn

    return register


def get_handler(job_type: str) -> Handler:
    try:
        return _HANDLERS[job_type]
    except KeyError:
        # Permanent, not transient: an unknown job type will still be unknown
        # in forty seconds. Retrying it five times would only delay the moment
        # a human notices.
        raise PermanentError(f"no handler registered for job type {job_type!r}") from None


def _record_execution(conn: psycopg.Connection, job: Job) -> None:
    """Write proof that this job ran, into the append-only audit log.

    This is what the no-double-processing test counts. Using audit_events
    rather than an ad-hoc table means the evidence sits in a table that
    physically rejects UPDATE and DELETE, so the result cannot be massaged
    after the fact.
    """
    log_event(
        conn,
        application_id=None,
        actor="system:worker",
        action="job.executed",
        details={
            "job_id": job.id,
            "job_type": job.job_type,
            "worker": WORKER_ID,
            "attempt": job.attempts,
        },
    )


@handler("demo.noop")
def demo_noop(conn: psycopg.Connection, job: Job) -> None:
    """Succeeds immediately. The workhorse of the concurrency proof."""
    _record_execution(conn, job)


@handler("demo.flaky")
def demo_flaky(conn: psycopg.Connection, job: Job) -> None:
    """Fails until a given attempt, then succeeds. Demonstrates backoff."""
    succeed_on = int(job.payload.get("succeed_on_attempt", 3))
    if job.attempts < succeed_on:
        raise RuntimeError(
            f"transient failure on attempt {job.attempts} "
            f"(will succeed on attempt {succeed_on})"
        )
    _record_execution(conn, job)


@handler("demo.poison")
def demo_poison(conn: psycopg.Connection, job: Job) -> None:
    """Always fails. Demonstrates parking after max_attempts."""
    raise RuntimeError(f"this job always fails (attempt {job.attempts})")


@handler("demo.permanent")
def demo_permanent(conn: psycopg.Connection, job: Job) -> None:
    """Fails permanently on the first attempt. Demonstrates PermanentError."""
    raise PermanentError("payload is not something this handler can ever process")
