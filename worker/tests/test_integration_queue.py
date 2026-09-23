"""The job queue, asserted continuously rather than demonstrated once.

Every claim in this file was previously verified by hand, once, on one machine,
and never again. That is the gap this closes: a regression in the queue would
have passed all 121 pure tests silently.

These run the REAL functions — `claim_job`, `run_job`, `complete_job`,
`reclaim_stale_jobs` — against a real Postgres, on real connections that
genuinely contend.
"""

from __future__ import annotations

import threading
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

import handlers  # noqa: F401,E402  — importing registers the demo handlers
from jobs import (  # noqa: E402
    backoff_seconds,
    claim_job,
    claim_job_naively,
    complete_job,
    fail_job,
    reclaim_stale_jobs,
)
from main import run_job  # noqa: E402


def enqueue(db, job_type: str, count: int = 1, payload: str = "{}") -> list[int]:
    ids = []
    with db.cursor() as cur:
        for _ in range(count):
            cur.execute(
                "insert into jobs (job_type, payload) values (%s, %s::jsonb) returning id",
                (job_type, payload),
            )
            ids.append(cur.fetchone()[0])
    return ids


def watermark(db) -> int:
    """The audit log's high-water mark, so a count can be scoped to one test.

    audit_events rejects DELETE, so a test cannot clear it and start fresh —
    that rule binds the tests as much as anything else. Recording the last id
    first and counting only above it is the alternative, and it makes each test
    independent of everything that ran before it.

    Learned the hard way: this file's 500-job test counted every demo.noop
    execution ever recorded. It passed for months because it was the only thing
    producing them, then test_drain.py arrived, ran eighty of its own first,
    and the count came to 581.
    """
    with db.cursor() as cur:
        cur.execute("select coalesce(max(id), 0) from audit_events")
        return cur.fetchone()[0]


def executed_job_ids(db, job_type: str, since: int = 0) -> list[int]:
    """Which jobs recorded that they ran, from the append-only audit log.

    Read from audit_events rather than from a counter the test keeps, because
    audit_events physically rejects UPDATE and DELETE — the evidence cannot be
    massaged, by the code under test or by the test.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            select (details->>'job_id')::bigint
              from audit_events
             where action = 'job.executed'
               and details->>'job_type' = %s
               and id > %s
            """,
            (job_type, since),
        )
        return [row[0] for row in cur.fetchall()]


def drain(url: str, worker_id: str, naive: bool = False) -> int:
    """One worker, on its own connection, until the queue is empty."""
    processed = 0
    conn = psycopg.connect(url, autocommit=True, application_name="kyc_worker")
    try:
        empty = 0
        while empty < 3:
            job = (
                claim_job_naively(conn, worker_id)
                if naive
                else claim_job(conn, worker_id)
            )
            if job is None:
                empty += 1
                continue
            empty = 0
            run_job(conn, job)
            processed += 1
    finally:
        conn.close()
    return processed


# ---------------------------------------------------------------------------
# The headline claim
# ---------------------------------------------------------------------------


def test_two_workers_never_process_a_job_twice(db, schema):
    """500 jobs, two workers, zero duplicates.

    THE test of this project's central architectural claim. It was demonstrated
    once by hand and never re-run; a change to CLAIM_SQL would have broken it
    invisibly.

    Two threads on separate connections, so they genuinely contend for rows
    rather than taking turns.
    """
    job_type = "demo.noop"
    # Ours must be the only claimable work, or the drained totals below count
    # jobs other tests left behind and the arithmetic stops meaning anything.
    with db.cursor() as cur:
        cur.execute("delete from jobs where status = 'queued'")
    mark = watermark(db)
    enqueue(db, job_type, 500)

    results: dict[str, int] = {}
    threads = [
        threading.Thread(
            target=lambda name=name: results.__setitem__(name, drain(schema, name))
        )
        for name in ("worker-A", "worker-B")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    executed = executed_job_ids(db, job_type, since=mark)

    assert len(executed) == 500, "every job must run"
    assert len(set(executed)) == 500, (
        f"{len(executed) - len(set(executed))} job(s) were processed more than once"
    )
    # Both workers did work — otherwise this proves nothing about contention.
    assert all(count > 0 for count in results.values()), results
    assert sum(results.values()) == 500


def test_the_naive_claim_really_is_broken(db, schema):
    """The counterfactual, kept honest.

    claim_job_naively exists to demonstrate the race rather than assert it. If
    it ever stopped producing duplicates, the comparison in the README would be
    a claim about nothing — and the most likely reason for that is someone
    "fixing" the deliberately broken function.
    """
    job_type = "demo.noop"
    mark = watermark(db)
    enqueue(db, job_type, 60)

    threads = [
        threading.Thread(target=drain, args=(schema, name, True))
        for name in ("naive-A", "naive-B")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    executed = executed_job_ids(db, job_type, since=mark)
    duplicates = len(executed) - len(set(executed))
    assert duplicates > 0, (
        "claim_job_naively processed nothing twice — the demonstration of the "
        "race condition is no longer demonstrating anything"
    )


# ---------------------------------------------------------------------------
# The reaper — the path Heroku will exercise daily
# ---------------------------------------------------------------------------


def test_a_job_abandoned_by_a_dead_worker_is_reclaimed(db):
    """A dyno restart mid-job, which on Heroku happens at least daily.

    A worker killed by SIGKILL never runs a failure handler, so its job stays
    'running' forever and nothing else will ever claim it. This path has real
    production traffic and had never been tested.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, status, attempts, max_attempts,
                              locked_at, locked_by)
            values ('demo.noop', 'running', 1, 5,
                    now() - interval '30 minutes', 'dyno-that-died')
            returning id
            """
        )
        job_id = cur.fetchone()[0]

    reclaimed = reclaim_stale_jobs(db, stale_after_seconds=300)

    assert (job_id, "queued") in reclaimed
    with db.cursor() as cur:
        cur.execute(
            "select status, attempts, locked_by from jobs where id = %s", (job_id,)
        )
        status, attempts, locked_by = cur.fetchone()

    assert status == "queued", "it must become claimable again"
    assert locked_by is None
    # The crucial part: the attempt is already spent. Counting attempts at CLAIM
    # time is what stops a job that kills workers from killing all of them in
    # turn — if the reaper reset this, a poison pill would cycle for ever.
    assert attempts == 1


def test_a_reclaimed_job_with_no_attempts_left_is_parked_not_requeued(db):
    """The poison pill, terminated.

    A job that has killed a worker on each of its allowed attempts must not go
    back on the queue to kill another one.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, status, attempts, max_attempts,
                              locked_at, locked_by)
            values ('demo.noop', 'running', 5, 5,
                    now() - interval '30 minutes', 'dyno-that-died')
            returning id
            """
        )
        job_id = cur.fetchone()[0]

    reclaimed = reclaim_stale_jobs(db, stale_after_seconds=300)

    assert (job_id, "parked") in reclaimed
    with db.cursor() as cur:
        cur.execute("select status from jobs where id = %s", (job_id,))
        assert cur.fetchone()[0] == "parked"


def test_a_job_still_within_the_timeout_is_left_alone(db):
    """The reaper must not steal work that is still running.

    Reclaiming a live job would cause exactly the double-processing the lock
    exists to prevent — the failure mode is worse than the one it fixes.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, status, attempts, locked_at, locked_by)
            values ('demo.noop', 'running', 1, now() - interval '10 seconds', 'busy')
            returning id
            """
        )
        job_id = cur.fetchone()[0]

    reclaimed = reclaim_stale_jobs(db, stale_after_seconds=300)

    assert job_id not in [row[0] for row in reclaimed]
    with db.cursor() as cur:
        cur.execute("select status from jobs where id = %s", (job_id,))
        assert cur.fetchone()[0] == "running"


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


def test_a_failing_job_is_retried_with_backoff_then_parked(db):
    """The whole retry lifecycle, end to end against the real fail_job."""
    # Ours must be the only claimable job, or claim_job will hand back
    # something another test left behind and this asserts nothing.
    with db.cursor() as cur:
        cur.execute("delete from jobs where status = 'queued'")

    [job_id] = enqueue(db, "demo.poison")
    with db.cursor() as cur:
        cur.execute("update jobs set max_attempts = 3 where id = %s", (job_id,))

    statuses = []
    for _ in range(3):
        job = claim_job(db, "retry-worker")
        assert job is not None and job.id == job_id, "the queue should hold only this job"
        run_job(db, job)
        with db.cursor() as cur:
            cur.execute(
                "select status, run_after > now() from jobs where id = %s", (job_id,)
            )
            statuses.append(cur.fetchone())
        # Let the backoff elapse so the next claim can happen immediately.
        with db.cursor() as cur:
            cur.execute("update jobs set run_after = now() where id = %s", (job_id,))

    assert statuses[0][0] == "queued", "first failure retries"
    assert statuses[0][1] is True, "and schedules into the future — backoff"
    assert statuses[1][0] == "queued", "second failure retries"
    assert statuses[2][0] == "parked", "attempts exhausted, parked not retried"

    with db.cursor() as cur:
        cur.execute("select last_error, attempts from jobs where id = %s", (job_id,))
        last_error, attempts = cur.fetchone()
    assert "attempts exhausted" in last_error
    assert attempts == 3


def test_a_permanent_error_parks_immediately_without_burning_retries(db):
    with db.cursor() as cur:
        cur.execute("delete from jobs where status = 'queued'")
    [job_id] = enqueue(db, "demo.permanent")
    job = claim_job(db, "permanent-worker")
    assert job is not None and job.id == job_id
    run_job(db, job)

    with db.cursor() as cur:
        cur.execute("select status, attempts, last_error from jobs where id = %s", (job_id,))
        status, attempts, last_error = cur.fetchone()

    assert status == "parked"
    assert attempts == 1, "it must not have consumed five attempts to learn this"
    assert "permanent error" in last_error


def test_backoff_grows_and_is_jittered():
    """Pure, but it belongs next to the behaviour it governs.

    Jitter matters: without it everything that failed during one outage retries
    at the same instant when it ends.
    """
    first = [backoff_seconds(1) for _ in range(40)]
    second = [backoff_seconds(2) for _ in range(40)]
    assert min(second) > min(first), "later attempts wait longer"
    assert len(set(first)) > 1, "identical delays mean the jitter has gone"


# ---------------------------------------------------------------------------
# Completion is atomic with the work
# ---------------------------------------------------------------------------


def test_marking_done_and_the_handlers_writes_commit_together(db):
    """A job cannot be marked finished unless its effects landed.

    Both writes share one transaction, so a crash between them is impossible by
    construction. Simulated here by rolling back after both.
    """
    with db.cursor() as cur:
        cur.execute("delete from jobs where status = 'queued'")
    [job_id] = enqueue(db, "demo.noop")
    job = claim_job(db, "atomic-worker")
    assert job is not None

    mark = watermark(db)

    # psycopg.Rollback is caught by the transaction block — that is what it is
    # for — so it does not propagate and there is nothing to assert on it.
    with db.transaction() as tx:
        handlers.demo_noop(db, job)
        complete_job(db, job.id)
        raise psycopg.Rollback(tx)

    with db.cursor() as cur:
        cur.execute("select status from jobs where id = %s", (job_id,))
        assert cur.fetchone()[0] == "running", "the completion rolled back"
    assert len(executed_job_ids(db, "demo.noop", since=mark)) == 0, (
        "the audit row rolled back with it — if logEvent opened its own "
        "connection this row would have survived"
    )
