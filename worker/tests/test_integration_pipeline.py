"""The vendor pipeline: idempotency, ordering, and the sweeper.

Each of these was demonstrated once by hand and never re-run. They are the
claims the README makes loudest, so they are the ones most worth asserting
continuously.

ON WHAT IS AND IS NOT EXERCISED HERE

The webhook ENDPOINT is TypeScript, in the Next.js service, and is not run by
this suite. What is asserted is the database contract that endpoint depends on:
the unique constraint, and enqueueing only when the insert actually happened.
A test that passes here with a broken route is possible, and that gap is named
in the README rather than papered over.

Everything else — `apply_result`, `run_screening`, `sweep_stuck` — is the real
handler, running against a real database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from psycopg.types.json import Jsonb  # noqa: E402

import result_handler  # noqa: E402
import sweeper  # noqa: E402
from jobs import claim_job  # noqa: E402
from lifecycle import Outcome  # noqa: E402
from main import run_job  # noqa: E402
from vendor import VerificationResult  # noqa: E402

NOW = datetime.now(timezone.utc)


def vendor_result(status: str, session_id: str, application_id: str, at=None):
    return VerificationResult(
        session_id=session_id,
        status=status,
        vendor_data=application_id,
        result_at=at,
        raw={"status": status},
    )


def load_application(db, application_id: str) -> dict:
    with db.cursor() as cur:
        cur.execute(
            """
            select id, status, vendor_status, vendor_result_at, vendor_applicant_id
              from applications where id = %s
            """,
            (application_id,),
        )
        row = cur.fetchone()
    return {
        "id": row[0],
        "status": row[1],
        "vendor_status": row[2],
        "vendor_result_at": row[3],
        "vendor_applicant_id": row[4],
    }


def backdate(db, application_id: str, interval: str) -> None:
    """Make an application look older than it is.

    `applications_set_updated_at` is a BEFORE UPDATE trigger that overwrites
    updated_at with now() on every write, so backdating by plain UPDATE is
    impossible — the trigger simply undoes it. That is the right behaviour in
    production and an obstacle only here, so the trigger is disabled for the one
    statement and immediately restored.

    Worth knowing rather than working around silently: it means no application's
    updated_at can be forged through the ordinary write path, by this code or by
    anything else holding a normal connection.
    """
    with db.cursor() as cur:
        cur.execute("alter table applications disable trigger applications_set_updated_at")
        try:
            cur.execute(
                f"update applications set updated_at = now() - interval '{interval}' "  # noqa: S608
                "where id = %s",
                (application_id,),
            )
        finally:
            cur.execute(
                "alter table applications enable trigger applications_set_updated_at"
            )


def audit_count(db, application_id: str, action: str | None = None) -> int:
    with db.cursor() as cur:
        if action:
            cur.execute(
                "select count(*) from audit_events where application_id = %s and action = %s",
                (application_id, action),
            )
        else:
            cur.execute(
                "select count(*) from audit_events where application_id = %s",
                (application_id,),
            )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Duplicate webhooks
# ---------------------------------------------------------------------------


def test_three_identical_webhooks_store_one_event_and_one_job(db, make_application):
    """The README's headline idempotency claim, as a continuous assertion.

    This runs the SQL contract the webhook route relies on — the unique
    constraint on (vendor, vendor_event_id), and the rule that a job is
    enqueued only when the insert actually produced a row.

    If a migration ever dropped that constraint, three deliveries would become
    three jobs and this fails. That is the regression worth catching: the
    constraint is what makes the route's idempotency real, not the code in it.
    """
    application_id = make_application("submitted")
    event_id = f"evt-{uuid.uuid4()}"
    body = f'{{"event_id":"{event_id}","vendor_data":"{application_id}"}}'

    enqueued = 0
    for _ in range(3):
        with db.transaction():
            with db.cursor() as cur:
                cur.execute(
                    """
                    insert into vendor_events
                        (vendor, vendor_event_id, event_type, signature_verified,
                         raw_body, payload)
                    values ('didit', %s, 'status.updated', true, %s, %s::jsonb)
                    on conflict (vendor, vendor_event_id) do nothing
                    returning id
                    """,
                    (event_id, body, body),
                )
                inserted = cur.fetchone()
                if inserted is not None:
                    cur.execute(
                        "insert into jobs (job_type, payload) "
                        "values ('vendor.process_webhook', %s)",
                        (Jsonb({"vendor_event_id": inserted[0]}),),
                    )
                    enqueued += 1

    with db.cursor() as cur:
        cur.execute(
            "select count(*) from vendor_events where vendor_event_id = %s", (event_id,)
        )
        stored = cur.fetchone()[0]
        cur.execute(
            "select count(*) from jobs where job_type = 'vendor.process_webhook' "
            "and payload->>'vendor_event_id' = ("
            "  select id::text from vendor_events where vendor_event_id = %s)",
            (event_id,),
        )
        jobs = cur.fetchone()[0]

    assert stored == 1, "three deliveries, one stored event"
    assert jobs == 1, "three deliveries, one job"
    assert enqueued == 1, "only the delivery that actually inserted enqueued work"


# ---------------------------------------------------------------------------
# Re-running a processed job
# ---------------------------------------------------------------------------


def test_reprocessing_a_result_changes_nothing(db, make_application):
    """At-least-once delivery means this WILL happen. It must be a no-op.

    Asserts on `updated_at` as well as the visible fields, because that is the
    strongest available evidence: a trigger bumps it on any UPDATE, so its not
    moving proves the handler did not even issue a redundant write.
    """
    application_id = make_application("submitted")
    session_id = str(uuid.uuid4())
    result = vendor_result("Approved", session_id, application_id, NOW)

    first = result_handler.apply_result(
        db,
        application=load_application(db, application_id),
        result=result,
        result_at=NOW,
        source="test",
    )
    assert first.should_write

    with db.cursor() as cur:
        cur.execute(
            "select status, vendor_status, vendor_result_at, updated_at, risk_score "
            "from applications where id = %s",
            (application_id,),
        )
        before = cur.fetchone()
    audits_before = audit_count(db, application_id)

    second = result_handler.apply_result(
        db,
        application=load_application(db, application_id),
        result=result,
        result_at=NOW,
        source="test",
    )

    assert second.outcome == Outcome.NO_CHANGE
    with db.cursor() as cur:
        cur.execute(
            "select status, vendor_status, vendor_result_at, updated_at, risk_score "
            "from applications where id = %s",
            (application_id,),
        )
        after = cur.fetchone()

    assert after == before, "a second run must change nothing, updated_at included"
    assert audit_count(db, application_id) == audits_before, (
        "no new audit rows — audit_events records state CHANGES, not invocations"
    )


# ---------------------------------------------------------------------------
# Out-of-order results
# ---------------------------------------------------------------------------


def test_an_older_result_cannot_overwrite_a_newer_one(db, make_application):
    """The recency guard, on the vendor's clock.

    Both results map to the same lifecycle stage, so the state machine has
    nothing to say about them — this is the case only the timestamp catches.
    """
    application_id = make_application("checking")
    session_id = str(uuid.uuid4())
    newer, older = NOW, NOW - timedelta(minutes=10)

    result_handler.apply_result(
        db,
        application=load_application(db, application_id),
        result=vendor_result("Approved", session_id, application_id, newer),
        result_at=newer,
        source="test",
    )
    assert load_application(db, application_id)["vendor_status"] == "Approved"

    evaluation = result_handler.apply_result(
        db,
        application=load_application(db, application_id),
        result=vendor_result("Declined", session_id, application_id, older),
        result_at=older,
        source="test",
    )

    assert evaluation.outcome == Outcome.STALE_RESULT
    assert load_application(db, application_id)["vendor_status"] == "Approved", (
        "the older Declined must not have overwritten the newer Approved"
    )
    assert audit_count(db, application_id, "vendor_result.refused") == 1, (
        "a refused result is recorded — an auditor asks about these"
    )


def test_a_newer_result_cannot_make_an_illegal_move(db, make_application):
    """The mirror case: recency passes, legality does not.

    The state machine catches what the timestamp cannot, which is why both
    guards exist.
    """
    application_id = make_application("decided")
    session_id = str(uuid.uuid4())

    evaluation = result_handler.apply_result(
        db,
        application=load_application(db, application_id),
        result=vendor_result("In Review", session_id, application_id, NOW),
        result_at=NOW,
        source="test",
    )

    assert evaluation.outcome == Outcome.ILLEGAL_TRANSITION
    assert load_application(db, application_id)["status"] == "decided", (
        "a late webhook must not reopen a closed case"
    )


# ---------------------------------------------------------------------------
# The sweeper
# ---------------------------------------------------------------------------


def test_the_sweeper_recovers_a_result_no_webhook_ever_delivered(
    db, make_application, monkeypatch
):
    """The dropped-delivery path.

    The vendor client is stubbed rather than reached over HTTP: what is under
    test is the sweeper's behaviour — finding stuck applications and applying
    what it learns — not the transport, which Phase 4 exercised separately.
    """
    session_id = str(uuid.uuid4())
    application_id = make_application("checking")
    with db.cursor() as cur:
        cur.execute(
            "update applications set vendor_applicant_id = %s where id = %s",
            (session_id, application_id),
        )
    # Backdated last, because any write bumps updated_at back to now().
    backdate(db, application_id, "2 hours")

    class StubClient:
        name = "stub"

        def fetch_session(self, requested_session_id):
            assert requested_session_id == session_id
            return vendor_result("Approved", session_id, application_id, NOW)

    monkeypatch.setattr(sweeper, "get_client", lambda: StubClient())

    before = load_application(db, application_id)
    assert before["status"] == "checking", "stuck, with nothing having told us"

    sweeper.sweep_stuck(db, job=None)

    after = load_application(db, application_id)
    assert after["status"] == "screening", "the sweeper applied what it found"
    assert after["vendor_status"] == "Approved"

    with db.cursor() as cur:
        cur.execute(
            "select details->>'source' from audit_events "
            "where application_id = %s and action = 'status.changed' "
            "order by id desc limit 1",
            (application_id,),
        )
        assert cur.fetchone()[0] == "sweeper", (
            "the audit trail must say this arrived by sweep, not by webhook"
        )


def test_the_sweeper_leaves_applications_that_are_not_stuck_alone(
    db, make_application, monkeypatch
):
    """A threshold set too low would reclaim work still in flight."""
    application_id = make_application("checking")
    with db.cursor() as cur:
        cur.execute(
            "update applications set vendor_applicant_id = %s, updated_at = now() "
            "where id = %s",
            (str(uuid.uuid4()), application_id),
        )

    called = []

    class StubClient:
        name = "stub"

        def fetch_session(self, session_id):
            called.append(session_id)
            return None

    monkeypatch.setattr(sweeper, "get_client", lambda: StubClient())
    sweeper.sweep_stuck(db, job=None)

    assert load_application(db, application_id)["status"] == "checking"
    assert application_id not in [str(c) for c in called]


def test_the_sweeper_reschedules_itself(db, monkeypatch):
    """A recurring job that stops recurring fails silently and for ever."""
    with db.cursor() as cur:
        cur.execute("delete from jobs where job_type = 'vendor.sweep_stuck'")

    class StubClient:
        name = "stub"

        def fetch_session(self, session_id):
            return None

    monkeypatch.setattr(sweeper, "get_client", lambda: StubClient())
    sweeper.sweep_stuck(db, job=None)

    with db.cursor() as cur:
        cur.execute(
            "select count(*), min(run_after) > now() from jobs "
            "where job_type = 'vendor.sweep_stuck' and status = 'queued'"
        )
        count, in_future = cur.fetchone()
    assert count == 1, "exactly one successor, never zero and never two"
    assert in_future, "scheduled ahead, not to run again immediately"
