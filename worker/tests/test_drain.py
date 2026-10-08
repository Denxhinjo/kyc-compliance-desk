"""The drain: a bounded batch, with the queue's guarantees intact.

The production entry point is a function with a wall-clock limit, so it cannot
loop until the queue is empty. It claims at most N jobs and returns, and is
called again instead.

What must NOT change is anything about how a job is claimed or run. These tests
exist to hold that line: the drain is a different SHAPE of caller, not a
different set of rules.
"""

from __future__ import annotations

import threading

import pytest

psycopg = pytest.importorskip("psycopg")

import handlers  # noqa: F401,E402 — importing registers the demo handlers
from batch import DrainResult, drain_once  # noqa: E402


def enqueue(db, count: int, job_type: str = "demo.noop") -> list[int]:
    ids = []
    with db.cursor() as cur:
        for _ in range(count):
            cur.execute(
                "insert into jobs (job_type, payload) values (%s, '{}'::jsonb) "
                "returning id",
                (job_type,),
            )
            ids.append(cur.fetchone()[0])
    return ids


def clear(db) -> None:
    with db.cursor() as cur:
        cur.execute("delete from jobs")


def executed_since(db, marker: int) -> list[int]:
    with db.cursor() as cur:
        cur.execute(
            """
            select (details->>'job_id')::bigint
              from audit_events
             where id > %s and action = 'job.executed'
            """,
            (marker,),
        )
        return [row[0] for row in cur.fetchall()]


def watermark(db) -> int:
    with db.cursor() as cur:
        cur.execute("select coalesce(max(id), 0) from audit_events")
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Bounded
# ---------------------------------------------------------------------------


def test_the_batch_is_bounded(db):
    """The property the whole design rests on.

    A function is killed at its duration limit. If the drain looped until the
    queue emptied, a backlog would mean every invocation is killed mid-job, and
    the queue would make no progress while looking busy.
    """
    clear(db)
    enqueue(db, 20)
    mark = watermark(db)

    result = drain_once(db, limit=5)

    assert result.claimed == 5, "claimed more than the limit"
    assert len(executed_since(db, mark)) == 5
    assert result.remaining == 15
    assert result.more is True, "the caller must be told to come back"


def test_an_empty_queue_returns_immediately(db):
    """The common case once a backlog is cleared, and it must be cheap."""
    clear(db)
    result = drain_once(db, limit=5)

    assert result.claimed == 0
    assert result.remaining == 0
    assert result.more is False, "nothing queued, so nothing to come back for"


def test_repeated_calls_drain_the_queue(db):
    """Called repeatedly instead of looping — so that has to actually work."""
    clear(db)
    enqueue(db, 12)
    mark = watermark(db)

    calls = 0
    while calls < 10:
        calls += 1
        if not drain_once(db, limit=5).more:
            break

    executed = executed_since(db, mark)
    assert len(executed) == 12
    assert len(set(executed)) == 12, "a job ran twice across calls"
    assert calls == 3, f"12 jobs at 5 a time should take 3 calls, took {calls}"


def test_a_partial_batch_is_not_more(db):
    """Fewer jobs than the limit means the queue ran out, not the budget."""
    clear(db)
    enqueue(db, 3)
    result = drain_once(db, limit=5)

    assert result.claimed == 3
    assert result.more is False


# ---------------------------------------------------------------------------
# The queue's guarantees, unchanged
# ---------------------------------------------------------------------------


def test_two_drains_never_process_a_job_twice(db, new_connection, schema):
    """The Phase 2 proof, re-run against the Phase 9 entry point.

    Two concurrent drains are two concurrent workers — the claim is the same
    FOR UPDATE SKIP LOCKED statement. If that were not true, this is where it
    would show.
    """
    clear(db)
    enqueue(db, 60)
    mark = watermark(db)

    results: list[DrainResult] = []

    def run():
        conn = psycopg.connect(schema, autocommit=True, application_name="drain-test")
        try:
            for _ in range(12):
                outcome = drain_once(conn, limit=5, reap=False)
                results.append(outcome)
                if not outcome.more:
                    break
        finally:
            conn.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    executed = executed_since(db, mark)
    assert len(executed) == 60, f"expected 60 executions, got {len(executed)}"
    assert len(set(executed)) == 60, (
        f"{len(executed) - len(set(executed))} job(s) were processed twice by "
        "concurrent drains"
    )


def test_the_drain_reclaims_abandoned_jobs(db):
    """A function killed mid-job leaves one 'running' for ever otherwise.

    The loop in main.py reaps on a timer. A function has no timer, so the reap
    rides along with the drain — which is why `reap` defaults to on.
    """
    clear(db)
    with db.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, status, attempts, max_attempts,
                              locked_at, locked_by)
            values ('demo.noop', 'running', 1, 5,
                    now() - interval '30 minutes', 'function-that-died')
            returning id
            """
        )
        job_id = cur.fetchone()[0]

    result = drain_once(db, limit=5)

    assert result.reclaimed == 1
    with db.cursor() as cur:
        cur.execute("select status, attempts from jobs where id = %s", (job_id,))
        status, attempts = cur.fetchone()
    # Reclaimed, then claimed and run in the same drain.
    assert status == "done", f"expected the reclaimed job to be run, got {status}"
    assert attempts == 2, "the earlier attempt must still be spent"


def test_a_failing_job_still_retries_and_parks(db):
    """Retry accounting is the runner's, not the loop's — so it survives."""
    clear(db)
    [job_id] = enqueue(db, 1, "demo.poison")
    with db.cursor() as cur:
        cur.execute("update jobs set max_attempts = 2 where id = %s", (job_id,))

    for _ in range(2):
        drain_once(db, limit=5, reap=False)
        with db.cursor() as cur:
            cur.execute("update jobs set run_after = now() where id = %s", (job_id,))

    with db.cursor() as cur:
        cur.execute("select status, attempts from jobs where id = %s", (job_id,))
        status, attempts = cur.fetchone()

    assert status == "parked"
    assert attempts == 2


# ---------------------------------------------------------------------------
# The endpoint's own guard
# ---------------------------------------------------------------------------


def test_the_endpoint_refuses_without_the_secret(monkeypatch):
    """An unset secret must refuse everything, not allow everything.

    A deployment that forgot to configure it should be visibly broken rather
    than quietly open to anyone who finds the URL.
    """
    import importlib.util
    from pathlib import Path

    endpoint = Path(__file__).resolve().parent.parent.parent / "api" / "drain.py"

    def load():
        """Load api/drain.py BY PATH.

        Not by name: `api/drain.py` and the worker package both sit on
        sys.path at test time, and importing "drain" would be a coin toss
        between them. Loading by path is unambiguous and needs no sys.path
        surgery to undo afterwards.
        """
        spec = importlib.util.spec_from_file_location("_drain_endpoint", endpoint)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    monkeypatch.setenv("DRAIN_SECRET", "")
    module = load()
    assert module.authorised({"x-drain-secret": ""}) is False
    assert module.authorised({"x-drain-secret": "anything"}) is False

    monkeypatch.setenv("DRAIN_SECRET", "s3cret")
    module = load()
    assert module.authorised({"x-drain-secret": "s3cret"}) is True
    assert module.authorised({"x-drain-secret": "wrong"}) is False
    assert module.authorised({}) is False


# ---------------------------------------------------------------------------
# A drain that fails must SAY it failed
# ---------------------------------------------------------------------------
#
# Added 2026-10-08, after five days in which every automatic decision on
# production failed behind a green scheduled run. The endpoint answered 200
# whatever happened inside it, because drain_once returns normally when its
# jobs fail — each failure is caught per job, recorded on the row and logged.
# The HTTP call had succeeded, so the caller was told so.
#
# See docs/incident-2026-10-03.md.


def test_the_result_counts_failed_jobs(db):
    """The number the endpoint needs, which did not exist before.

    Without a count of failures, "I ran five jobs" and "I attempted five jobs
    and every one failed" are the same response.
    """
    clear(db)
    enqueue(db, 2, "demo.poison")
    enqueue(db, 1, "demo.noop")

    result = drain_once(db, limit=10, reap=False)

    assert result.claimed == 3
    assert result.failed == 2, "two poison jobs should be counted as failures"


def test_a_clean_drain_reports_no_failures(db):
    """The counterweight.

    A result that always reported failures would turn the endpoint permanently
    red, which is the same uselessness as permanently green.
    """
    clear(db)
    enqueue(db, 3)

    result = drain_once(db, limit=10, reap=False)

    assert result.claimed == 3
    assert result.failed == 0
    assert result.parked == 0


def test_parked_is_counted_separately_from_failed(db):
    """`failed` includes jobs that will retry; `parked` is the terminal subset.

    Both are reported so a reader can tell a transient blip from a job the
    system has given up on. Only the second is an incident.
    """
    clear(db)
    [job_id] = enqueue(db, 1, "demo.poison")
    with db.cursor() as cur:
        cur.execute("update jobs set max_attempts = 1 where id = %s", (job_id,))

    result = drain_once(db, limit=5, reap=False)

    assert result.failed == 1
    assert result.parked == 1, "one attempt with max_attempts=1 parks immediately"


def test_run_job_reports_its_outcome(db):
    """The change that makes the counting possible at all."""
    from runner import JobOutcome, run_job
    from jobs import claim_job

    clear(db)
    enqueue(db, 1, "demo.noop")
    assert run_job(db, claim_job(db, "outcome-test")) == JobOutcome.DONE

    enqueue(db, 1, "demo.poison")
    assert run_job(db, claim_job(db, "outcome-test")) in (
        JobOutcome.RETRYING,
        JobOutcome.PARKED,
    )
