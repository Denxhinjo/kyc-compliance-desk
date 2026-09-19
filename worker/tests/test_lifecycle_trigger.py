"""Tests for the lifecycle trigger — migration 017.

THE FIRST DATABASE-BACKED TESTS IN THIS PROJECT.

Everything else in tests/ is pure: no database, no network, milliseconds. That
is right for the scoring and matching rules, which are pure functions. It
cannot work here, because the thing under test IS the database — a rule written
in SQL and enforced by Postgres. Asserting it from Python without Postgres
would only be testing a copy of the rule, which is precisely the arrangement
migration 017 exists to end.

These skip cleanly when no database is reachable, so `pytest` still runs on a
fresh clone with nothing running; the pure suite is unaffected.

    docker compose up -d
    python db/migrate.py up
    .venv/Scripts/python -m pytest
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")

from db import connect  # noqa: E402
from lifecycle import ALLOWED_TRANSITIONS, LIFECYCLE  # noqa: E402

#: Postgres' SQLSTATE for RAISE ... USING errcode = 'restrict_violation'.
RESTRICT_VIOLATION = "23001"

#: A legal path from 'started' to each state, so test setup never has to make an
#: illegal move to reach the state it wants to test from. Written out rather
#: than derived, so a mistake in the derivation cannot quietly weaken a test.
ROUTE_FROM_STARTED = {
    "started": [],
    "submitted": ["submitted"],
    "checking": ["checking"],
    "screening": ["screening"],
    "decided": ["screening", "decided"],
}


@pytest.fixture(scope="module")
def conn():
    """One connection for the module, or skip the whole file.

    A missing database is not a failure — it is the ordinary state of a fresh
    clone, and failing there would train people to ignore a red suite.
    """
    try:
        connection = connect()
    except Exception as err:  # noqa: BLE001 — any connection problem means skip
        pytest.skip(f"no database reachable: {err}")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def application(conn):
    """A throwaway application, unwound afterwards.

    Each test runs inside a transaction that is deliberately never committed, so
    tests leave nothing behind and can run against a database holding real rows.

    Worth noticing: cleaning up by DELETE is not available here even if we
    wanted it, because any audit rows a test caused would refuse to be deleted.
    Rolling back is the only way out — which is a small demonstration that the
    append-only rule binds the test suite as much as anything else.
    """
    with conn.transaction() as outer:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into applications
                    (status, full_name, date_of_birth, address_line1,
                     address_city, address_postcode, address_country)
                values ('started', %s, %s, '1 Test Street', 'Testville',
                        'T1 1TT', 'GB')
                returning id
                """,
                (f"Trigger Test {uuid.uuid4().hex[:8]}", date(1990, 1, 1)),
            )
            application_id = cur.fetchone()[0]

        yield application_id

        # psycopg has no tx.rollback(); raising Rollback is the documented way
        # to unwind a transaction block without the error escaping the test.
        raise psycopg.Rollback(outer)


def set_status(conn, application_id, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update applications set status = %s where id = %s",
            (status, application_id),
        )


def current_status(conn, application_id) -> str:
    with conn.cursor() as cur:
        cur.execute("select status from applications where id = %s", (application_id,))
        return cur.fetchone()[0]


def move_to(conn, application_id, status: str) -> None:
    for step in ROUTE_FROM_STARTED[status]:
        set_status(conn, application_id, step)


# ---------------------------------------------------------------------------
# The mirror
# ---------------------------------------------------------------------------


def test_python_mirror_matches_the_database_exactly(conn):
    """worker/lifecycle.py must agree with application_transitions.

    This is the test that makes "lifecycle.py is a mirror" a fact rather than a
    comment. Add an edge in one place and not the other and this fails — which
    is the whole reason the transitions are a readable table rather than a list
    hardcoded inside the trigger function.
    """
    with conn.cursor() as cur:
        cur.execute("select from_status, to_status from application_transitions")
        in_database = {(row[0], row[1]) for row in cur.fetchall()}

    in_python = {
        (source, target)
        for source, targets in ALLOWED_TRANSITIONS.items()
        for target in targets
    }

    assert in_python == in_database, (
        "lifecycle.py has drifted from the schema.\n"
        f"  only in Python:   {sorted(in_python - in_database)}\n"
        f"  only in database: {sorted(in_database - in_python)}"
    )


def test_every_state_in_the_table_is_a_known_status(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select from_status from application_transitions "
            "union select to_status from application_transitions"
        )
        states = {row[0] for row in cur.fetchall()}
    assert states <= set(LIFECYCLE)


# ---------------------------------------------------------------------------
# Every legal transition is accepted
# ---------------------------------------------------------------------------

LEGAL = sorted(
    (source, target)
    for source, targets in ALLOWED_TRANSITIONS.items()
    for target in targets
)


@pytest.mark.parametrize("source,target", LEGAL)
def test_legal_transitions_are_accepted(conn, application, source, target):
    """Every edge in the table, exercised against the real trigger."""
    move_to(conn, application, source)
    assert current_status(conn, application) == source

    set_status(conn, application, target)
    assert current_status(conn, application) == target


# ---------------------------------------------------------------------------
# Illegal transitions are refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,target,why",
    [
        ("started", "decided", "the rule this migration exists for"),
        ("submitted", "decided", "still not screened"),
        ("checking", "decided", "still not screened"),
        ("decided", "checking", "terminal — a late webhook must not reopen a closed case"),
        ("decided", "screening", "terminal"),
        ("decided", "submitted", "terminal"),
        ("screening", "checking", "backwards"),
        ("screening", "submitted", "backwards"),
        ("checking", "submitted", "backwards"),
        ("submitted", "started", "backwards"),
    ],
)
def test_illegal_transitions_are_refused(conn, application, source, target, why):
    move_to(conn, application, source)
    assert current_status(conn, application) == source

    # A nested transaction() is a SAVEPOINT. The failed statement unwinds only
    # to here, so the outer transaction survives and the fixture can still clean
    # up — and it mirrors what a caller should do around a write that might be
    # refused.
    with pytest.raises(psycopg.errors.RestrictViolation) as raised:
        with conn.transaction():
            set_status(conn, application, target)

    assert raised.value.sqlstate == RESTRICT_VIOLATION, why
    # The message names both ends, so a log line is enough to diagnose it.
    assert f"{source} -> {target}" in str(raised.value)
    # Unchanged.
    assert current_status(conn, application) == source


def test_the_compliance_rule_has_exactly_one_route_in(conn):
    """Nothing reaches 'decided' except from 'screening'.

    Stated as its own test rather than left implicit in the list above, because
    it is the rule with legal weight: you may not decide on a customer you have
    not screened. If someone adds a convenient shortcut, this is the test that
    should stop them.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select from_status from application_transitions where to_status = 'decided'"
        )
        assert sorted(row[0] for row in cur.fetchall()) == ["screening"]


def test_decided_is_terminal(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from application_transitions where from_status = 'decided'"
        )
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# What the trigger must NOT interfere with
# ---------------------------------------------------------------------------


def test_updates_that_do_not_touch_status_are_unaffected(conn, application):
    """Most writes to this table are not transitions.

    Writing a risk score, linking a vendor session, bumping updated_at — the
    lifecycle has nothing to say about any of them, and a trigger that fired on
    them would break the application for no benefit.
    """
    with conn.cursor() as cur:
        cur.execute(
            "update applications set risk_score = 42, vendor_status = 'Approved' "
            "where id = %s",
            (application,),
        )
    assert current_status(conn, application) == "started"


def test_setting_status_to_its_current_value_is_allowed(conn, application):
    """A self-transition is a no-op, not an illegal move.

    Idempotent handlers re-issue the same write; refusing it would turn a
    harmless duplicate into an error.
    """
    set_status(conn, application, "started")
    assert current_status(conn, application) == "started"


def test_an_unknown_status_is_refused_by_the_trigger_before_the_check(conn, application):
    """An UPDATE to a nonsense status is refused — by the TRIGGER, not the CHECK.

    Worth pinning, because the ordering is not obvious and the first version of
    this test asserted the wrong one. Postgres runs BEFORE ROW triggers before
    it evaluates CHECK constraints, so the lifecycle guard sees 'aproved' first,
    finds no edge for it, and raises. The CHECK from migration 001 never gets a
    look on this path.

    Both are still needed. The trigger governs which MOVES are legal and only
    fires on UPDATE; the CHECK governs which VALUES may exist at all and is what
    guards INSERT, where there is no previous row to transition from — see the
    test below.
    """
    with pytest.raises(psycopg.errors.RestrictViolation) as raised:
        with conn.transaction():
            set_status(conn, application, "aproved")
    assert "started -> aproved" in str(raised.value)
    assert current_status(conn, application) == "started"


def test_the_check_constraint_still_guards_insert(conn):
    """Where the trigger does not reach, the CHECK does.

    Migration 017 deliberately does not constrain INSERT: an insert creates an
    application rather than transitioning one, and there is no OLD row to reason
    from. So the CHECK on applications.status is what stops a row being created
    with a status nobody handles.
    """
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into applications
                        (status, full_name, date_of_birth, address_line1,
                         address_city, address_postcode, address_country)
                    values ('aproved', 'Check Test', '1990-01-01', '1 Test Street',
                            'Testville', 'T1 1TT', 'GB')
                    """
                )
