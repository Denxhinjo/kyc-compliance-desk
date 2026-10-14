"""The sweeper's third answer: "the vendor has no record of that session".

Before 2026-10-14 the sweeper could apply a result and could note that the
vendor was unreachable, but a vendor that disowned a session produced a log line
and nothing else. 38 applications on production were asked about on every sweep
for three weeks, got the same answer every time, and stayed "in flight" with no
audit trail and no terminal state.

These tests cover the escalation that closes them, the two conditions that gate
it, the concurrency case, the starvation fix in the batch ordering, and the
separate expiry path for applications that never started verification at all.

A NOTE ON SETTING UP HISTORY. Several tests insert `vendor.session_not_found`
rows with a backdated `occurred_at` to stand in for earlier sweeps. That is an
INSERT, which audit_events permits — it refuses UPDATE, DELETE and TRUNCATE, not
appends — and it is the only way to express "we asked about this an hour ago"
without making the suite take an hour. The closing decision itself is always
taken by the real sweeper against that history, never simulated.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

psycopg = pytest.importorskip("psycopg")

import sweeper  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402


class DisownedByVendor:
    """A vendor that has no record of anything. 404 for every session."""

    name = "stub-404"

    def __init__(self):
        self.asked: list[str] = []

    def fetch_session(self, session_id):
        self.asked.append(str(session_id))
        return None


def stuck_with_session(db, make_application, status="submitted", age="2 hours"):
    """An application waiting on the vendor, old enough for the sweeper."""
    session_id = str(uuid.uuid4())
    application_id = make_application(status)
    with db.cursor() as cur:
        cur.execute(
            "update applications set vendor_applicant_id = %s where id = %s",
            (session_id, application_id),
        )
    _backdate_updated_at(db, application_id, age)
    return application_id, session_id


def _backdate_updated_at(db, application_id, interval: str) -> None:
    """Make an application look older than it is.

    `applications_set_updated_at` overwrites updated_at on every write, so this
    disables that one trigger for a single statement and restores it. Same
    approach and the same reasoning as test_integration_pipeline.backdate: in
    production it means updated_at cannot be forged through the write path.
    """
    with db.cursor() as cur:
        cur.execute(
            "alter table applications disable trigger applications_set_updated_at"
        )
        try:
            cur.execute(
                "update applications set updated_at = now() - %s::interval where id = %s",
                (interval, application_id),
            )
        finally:
            cur.execute(
                "alter table applications enable trigger applications_set_updated_at"
            )


def _backdate_created_at(db, application_id, interval: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "alter table applications disable trigger applications_set_updated_at"
        )
        try:
            cur.execute(
                "update applications set created_at = now() - %s::interval, "
                "updated_at = now() - %s::interval where id = %s",
                (interval, interval, application_id),
            )
        finally:
            cur.execute(
                "alter table applications enable trigger applications_set_updated_at"
            )


def plant_not_found(db, application_id, session_id, ago: str) -> None:
    """Stand in for an earlier sweep that got a 404. An append, not an edit."""
    with db.cursor() as cur:
        cur.execute(
            """
            insert into audit_events (application_id, occurred_at, actor, action, details)
            values (%s, now() - %s::interval, 'system:worker', %s, %s)
            """,
            (
                application_id,
                ago,
                sweeper.NOT_FOUND_ACTION,
                Jsonb({"session_id": session_id, "planted_by": "test setup"}),
            ),
        )


def status_of(db, application_id) -> str:
    with db.cursor() as cur:
        cur.execute("select status from applications where id = %s", (application_id,))
        return cur.fetchone()[0]


def count_action(db, application_id, action: str) -> int:
    with db.cursor() as cur:
        cur.execute(
            "select count(*) from audit_events where application_id = %s and action = %s",
            (application_id, action),
        )
        return cur.fetchone()[0]


def latest_sweep_report(db) -> dict:
    with db.cursor() as cur:
        cur.execute(
            "select details from audit_events where action = 'sweep.completed' "
            "order by occurred_at desc, id desc limit 1"
        )
        row = cur.fetchone()
    return row[0] if row else {}


# ---------------------------------------------------------------------------
# The event
# ---------------------------------------------------------------------------


def test_a_404_writes_an_audit_event(db, make_application, monkeypatch):
    """The answer is recorded, with the application and session it concerns.

    This is the whole fix in miniature: the information used to exist only in a
    log line on an ephemeral function, where nothing could count it and nobody
    would ever read it.
    """
    application_id, session_id = stuck_with_session(db, make_application)
    monkeypatch.setattr(sweeper, "get_client", lambda: DisownedByVendor())

    sweeper.sweep_stuck(db, job=None)

    assert count_action(db, application_id, sweeper.NOT_FOUND_ACTION) == 1
    with db.cursor() as cur:
        cur.execute(
            "select details from audit_events where application_id = %s and action = %s",
            (application_id, sweeper.NOT_FOUND_ACTION),
        )
        details = cur.fetchone()[0]
    assert details["session_id"] == session_id, "the session asked about is named"


def test_one_404_does_not_close_the_application(db, make_application, monkeypatch):
    """One answer is not a pattern. The application stays where it is."""
    application_id, _ = stuck_with_session(db, make_application)
    monkeypatch.setattr(sweeper, "get_client", lambda: DisownedByVendor())

    sweeper.sweep_stuck(db, job=None)

    assert status_of(db, application_id) == "submitted"
    assert count_action(db, application_id, sweeper.CLOSED_ACTION) == 0


# ---------------------------------------------------------------------------
# The two conditions
# ---------------------------------------------------------------------------


def test_the_third_answer_an_hour_apart_closes_the_application(
    db, make_application, monkeypatch
):
    """The headline case. Two prior answers, the sweeper supplies the third.

    The closure is decided by the real sweeper reading the real audit history —
    only the first two answers are planted.
    """
    application_id, session_id = stuck_with_session(db, make_application)
    plant_not_found(db, application_id, session_id, "3 hours")
    plant_not_found(db, application_id, session_id, "90 minutes")
    monkeypatch.setattr(sweeper, "get_client", lambda: DisownedByVendor())

    sweeper.sweep_stuck(db, job=None)

    assert status_of(db, application_id) == "verification_unavailable"
    assert count_action(db, application_id, sweeper.NOT_FOUND_ACTION) == 3
    assert count_action(db, application_id, sweeper.CLOSED_ACTION) == 1

    with db.cursor() as cur:
        cur.execute(
            "select details from audit_events where application_id = %s and action = %s",
            (application_id, sweeper.CLOSED_ACTION),
        )
        details = cur.fetchone()[0]
    assert details["not_found_answers"] == 3
    assert details["from_status"] == "submitted"
    assert details["first_asked"] and details["last_asked"], (
        "the closure cites the window it was decided over, so the escalation "
        "can be re-derived from history"
    )


def test_three_answers_inside_the_hour_do_not_close(
    db, make_application, monkeypatch
):
    """The SPAN condition.

    Three 404s a few minutes apart is one broken deploy of the simulator, not a
    vendor that has lost the session. Without this condition a transient fault
    would close applications in the time it takes to notice it.
    """
    application_id, session_id = stuck_with_session(db, make_application)
    plant_not_found(db, application_id, session_id, "4 minutes")
    plant_not_found(db, application_id, session_id, "2 minutes")
    monkeypatch.setattr(sweeper, "get_client", lambda: DisownedByVendor())

    sweeper.sweep_stuck(db, job=None)

    assert count_action(db, application_id, sweeper.NOT_FOUND_ACTION) == 3, "three answers"
    assert status_of(db, application_id) == "submitted", "but inside one hour"
    assert count_action(db, application_id, sweeper.CLOSED_ACTION) == 0


def test_two_answers_an_hour_apart_do_not_close(db, make_application, monkeypatch):
    """The COUNT condition. A wide window is not a substitute for repetition."""
    application_id, session_id = stuck_with_session(db, make_application)
    plant_not_found(db, application_id, session_id, "5 hours")
    monkeypatch.setattr(sweeper, "get_client", lambda: DisownedByVendor())

    sweeper.sweep_stuck(db, job=None)

    assert count_action(db, application_id, sweeper.NOT_FOUND_ACTION) == 2
    assert status_of(db, application_id) == "submitted"


def test_a_checking_application_closes_the_same_way(
    db, make_application, monkeypatch
):
    """Both states the sweeper examines can reach the off-ramp.

    'checking' means the vendor said its own humans were reviewing. If the
    session then evaporates, the case is as unresolvable as from 'submitted'.
    """
    application_id, session_id = stuck_with_session(
        db, make_application, status="checking"
    )
    plant_not_found(db, application_id, session_id, "3 hours")
    plant_not_found(db, application_id, session_id, "90 minutes")
    monkeypatch.setattr(sweeper, "get_client", lambda: DisownedByVendor())

    sweeper.sweep_stuck(db, job=None)

    assert status_of(db, application_id) == "verification_unavailable"


def test_closing_is_not_a_rejection(db, make_application, monkeypatch):
    """THE DISTINCTION THE STATUS EXISTS TO MAKE.

    A rejection asserts a risk finding about a person. Nothing here found
    anything about anybody — the evidence could not be obtained. So: no
    decision row, and a status that is not 'decided'. If someone later
    "simplifies" this by routing to a rejection, this fails.
    """
    application_id, session_id = stuck_with_session(db, make_application)
    plant_not_found(db, application_id, session_id, "3 hours")
    plant_not_found(db, application_id, session_id, "90 minutes")
    monkeypatch.setattr(sweeper, "get_client", lambda: DisownedByVendor())

    sweeper.sweep_stuck(db, job=None)

    assert status_of(db, application_id) == "verification_unavailable"
    with db.cursor() as cur:
        cur.execute(
            "select count(*) from decisions where application_id = %s", (application_id,)
        )
        assert cur.fetchone()[0] == 0, "no decision was recorded, because none was made"

    with db.cursor() as cur:
        cur.execute(
            "select details from audit_events where application_id = %s and action = %s",
            (application_id, sweeper.CLOSED_ACTION),
        )
        details = cur.fetchone()[0]
    assert "not_a_rejection" in details, "the record says so in its own terms"


def test_an_already_closed_application_is_not_examined_again(
    db, make_application, monkeypatch
):
    """Terminal means terminal: it drops out of the sweeper's selection."""
    application_id, session_id = stuck_with_session(db, make_application)
    plant_not_found(db, application_id, session_id, "3 hours")
    plant_not_found(db, application_id, session_id, "90 minutes")
    client = DisownedByVendor()
    monkeypatch.setattr(sweeper, "get_client", lambda: client)

    sweeper.sweep_stuck(db, job=None)
    assert status_of(db, application_id) == "verification_unavailable"
    assert client.asked.count(session_id) == 1

    client.asked.clear()
    sweeper.sweep_stuck(db, job=None)

    # Scoped to THIS session rather than to the whole batch: the test database
    # is session-scoped and other tests in this file leave their own stuck
    # applications behind, which the sweeper is right to examine.
    assert session_id not in client.asked, "a closed application is not asked about again"
    assert count_action(db, application_id, sweeper.NOT_FOUND_ACTION) == 3, "no fourth answer"
    assert count_action(db, application_id, sweeper.CLOSED_ACTION) == 1, "closed once"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_two_concurrent_sweeps_do_not_close_the_same_application_twice(
    db, make_application, new_connection
):
    """Two sweeps in flight at once must produce one closure, not two.

    Nothing stops an operator triggering a drain while the scheduled one runs,
    so both can select the same application and both can reach the escalation.
    Covers both interleavings:

      1. while the first transaction is open and holding the row, the second
         must decline — `for update skip locked`
      2. after the first has committed, the second must decline because the
         status is no longer closable — the re-read inside the lock
    """
    application_id, session_id = stuck_with_session(db, make_application)
    plant_not_found(db, application_id, session_id, "3 hours")
    plant_not_found(db, application_id, session_id, "90 minutes")
    plant_not_found(db, application_id, session_id, "1 minute")

    first = new_connection("sweep-A")
    second = new_connection("sweep-B")

    with first.transaction():
        assert sweeper._close_if_confirmed_missing(first, application_id, session_id)
        # Interleaving 1: the row is locked and uncommitted.
        with second.transaction():
            assert not sweeper._close_if_confirmed_missing(
                second, application_id, session_id
            ), "the second sweep must not escalate a row the first is holding"

    # Interleaving 2: the first has committed; the lock is free but the work is done.
    with second.transaction():
        assert not sweeper._close_if_confirmed_missing(
            second, application_id, session_id
        ), "the second sweep must notice the status already changed"

    assert count_action(db, application_id, sweeper.CLOSED_ACTION) == 1
    assert status_of(db, application_id) == "verification_unavailable"


# ---------------------------------------------------------------------------
# Evidence that the sweeper ran
# ---------------------------------------------------------------------------


def test_sweep_completed_is_written_when_nothing_applied(
    db, make_application, monkeypatch
):
    """The defect this replaces.

    `sweep.completed` used to be written only `if applied or unreachable`, so
    the commonest outcome — every session came back 404 — wrote nothing at all.
    Three weeks of correct sweeps left zero rows, and from outside a working
    sweeper was indistinguishable from one that had never run.
    """
    application_id, _ = stuck_with_session(db, make_application)
    monkeypatch.setattr(sweeper, "get_client", lambda: DisownedByVendor())

    sweeper.sweep_stuck(db, job=None)

    report = latest_sweep_report(db)
    assert report, "a sweep that applied nothing still leaves evidence it ran"
    assert report["applied"] == 0
    assert report["not_found"] >= 1, "and says what it actually found"
    assert "closed_verification_unavailable" in report
    assert "expired_abandoned" in report


def test_sweep_completed_is_written_when_there_is_nothing_to_examine(db, monkeypatch):
    """An idle sweep is still a sweep.

    The health page reads this row to show when the sweeper last ran, so a quiet
    period must not look identical to a stopped scheduler.
    """
    monkeypatch.setattr(sweeper, "get_client", lambda: DisownedByVendor())
    with db.cursor() as cur:
        cur.execute("select count(*) from audit_events where action = 'sweep.completed'")
        before = cur.fetchone()[0]

    sweeper.sweep_stuck(db, job=None)

    with db.cursor() as cur:
        cur.execute("select count(*) from audit_events where action = 'sweep.completed'")
        assert cur.fetchone()[0] == before + 1


# ---------------------------------------------------------------------------
# The starvation fix
# ---------------------------------------------------------------------------


def test_the_batch_asks_about_the_least_recently_asked_first(
    db, make_application, monkeypatch
):
    """The ordering bug that made the escalation unreachable for most cases.

    The batch is capped, and the order used to be `updated_at` — but a sweep
    that resolves nothing never touches `updated_at`, so the same oldest N were
    examined on every run and the remainder were never examined once. On
    production that was 25 of 38 forever and 13 never.

    Ordering by when we last asked, from the audit log, makes the rotation fair:
    with a batch of two and three candidates, two sweeps must reach all three.
    """
    monkeypatch.setattr(sweeper, "SWEEP_BATCH_SIZE", 2)
    client = DisownedByVendor()
    monkeypatch.setattr(sweeper, "get_client", lambda: client)

    sessions = {}
    for _ in range(3):
        application_id, session_id = stuck_with_session(db, make_application)
        sessions[session_id] = application_id

    sweeper.sweep_stuck(db, job=None)
    first_round = set(client.asked)
    assert len(first_round) == 2, "the cap is respected"

    client.asked.clear()
    sweeper.sweep_stuck(db, job=None)
    second_round = set(client.asked)

    assert set(sessions) - first_round <= second_round, (
        "the application missed by the first sweep must be asked about by the "
        "second — otherwise it is starved forever"
    )


# ---------------------------------------------------------------------------
# (b2) Abandoned applications
# ---------------------------------------------------------------------------


def test_an_abandoned_application_expires_after_the_window(db, make_application):
    """'started' with no session, past the window: a process timeout.

    The sweeper cannot help these and correctly ignores them — with no session
    id there is nothing to ask the vendor. Production had 47, aged up to 53
    days, with nothing examining them at all.
    """
    application_id = make_application("started")
    _backdate_created_at(db, application_id, "40 days")

    expired = sweeper.expire_abandoned(db)

    assert expired >= 1
    assert status_of(db, application_id) == "expired"
    assert count_action(db, application_id, sweeper.EXPIRED_ACTION) == 1


def test_a_recent_abandoned_application_is_left_alone(db, make_application):
    """Inside the window it is an applicant who may still come back."""
    application_id = make_application("started")
    _backdate_created_at(db, application_id, "3 days")

    sweeper.expire_abandoned(db)

    assert status_of(db, application_id) == "started"


def test_an_application_with_a_session_never_expires(db, make_application):
    """Expiry is only for applications that never began verification.

    One with a session belongs to the vendor-sweep path, which can learn a
    result or escalate. Expiring it would discard a case that still has
    somewhere to go.
    """
    application_id, _ = stuck_with_session(db, make_application)
    _backdate_created_at(db, application_id, "40 days")

    sweeper.expire_abandoned(db)

    assert status_of(db, application_id) == "submitted"


def test_expiring_is_not_a_rejection(db, make_application):
    """Same distinction as the other off-ramp, for the same reason."""
    application_id = make_application("started")
    _backdate_created_at(db, application_id, "40 days")

    sweeper.expire_abandoned(db)

    with db.cursor() as cur:
        cur.execute(
            "select count(*) from decisions where application_id = %s", (application_id,)
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "select details from audit_events where application_id = %s and action = %s",
            (application_id, sweeper.EXPIRED_ACTION),
        )
        assert "not_a_rejection" in cur.fetchone()[0]


def test_expiry_is_reported_in_the_sweep_row(db, make_application, monkeypatch):
    """So a reader can tell which recovery path did the work."""
    application_id = make_application("started")
    _backdate_created_at(db, application_id, "40 days")
    monkeypatch.setattr(sweeper, "get_client", lambda: DisownedByVendor())

    sweeper.sweep_stuck(db, job=None)

    assert latest_sweep_report(db)["expired_abandoned"] >= 1


# ---------------------------------------------------------------------------
# The database has the last word
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("started", "verification_unavailable"),
        ("screening", "verification_unavailable"),
        ("decided", "verification_unavailable"),
        ("submitted", "expired"),
        ("checking", "expired"),
    ],
)
def test_the_database_refuses_an_off_ramp_from_the_wrong_state(
    db, make_application, from_status, to_status
):
    """The permitted edges are data, and the trigger enforces them.

    'started' has no session, so "the vendor has no record of it" cannot be
    said. 'screening' means the evidence arrived. 'decided' is finished. And an
    application with a session has somewhere to go, so it does not expire.

    Asserted against the database rather than against the Python mirror,
    because the mirror is advisory — the trigger is what actually refuses.
    """
    application_id = make_application("started")
    route = {
        "started": [],
        "submitted": ["submitted"],
        "checking": ["submitted", "checking"],
        "screening": ["submitted", "checking", "screening"],
        "decided": ["submitted", "checking", "screening", "decided"],
    }[from_status]
    with db.cursor() as cur:
        for step in route:
            cur.execute(
                "update applications set status = %s where id = %s",
                (step, application_id),
            )

    with pytest.raises(psycopg.errors.RestrictViolation):
        with db.transaction():
            with db.cursor() as cur:
                cur.execute(
                    "update applications set status = %s where id = %s",
                    (to_status, application_id),
                )

    assert status_of(db, application_id) == from_status
