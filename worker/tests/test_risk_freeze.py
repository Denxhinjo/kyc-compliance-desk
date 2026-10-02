"""Tests for the risk-column freeze — migration 020.

WHY THESE GO THROUGH SQL AND NOT THROUGH THE HANDLER

The rule under test is a database guarantee, and the whole argument for putting
it in the schema rather than in a WHERE clause is that it must hold for writers
that never consulted it — including a second language, and including psql. A
test that drove worker/screening_handler.py would prove only that one function
behaves, which is the thing migration 020 deliberately does NOT rely on.

So every assertion below issues the UPDATE directly. If the guard is ever
weakened to a predicate inside one handler, these fail.

THE CASE THAT MATTERS MOST IS NOT THE OBVIOUS ONE

An application at status 'decided' being re-scored is the easy case to imagine
and the one no current code path reaches. The reachable one is a REFERRED case:
it has a decision row and still sits at 'screening', so run_screening's
`status == 'screening'` guard lets it through, and at-least-once delivery means
a redelivered screening.run will eventually try. That is why the trigger keys on
the existence of a decision row rather than on status, and why there is a test
for it here.

These skip cleanly when no database is reachable, like the other DB-backed
tests, and run against the throwaway `_test` schema rather than DATABASE_URL.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")

from psycopg.types.json import Jsonb  # noqa: E402

#: started -> submitted -> checking -> screening, the only route to a state from
#: which an application may be decided. Mirrors application_transitions; the
#: lifecycle test is what proves that table and lifecycle.py agree.
ROUTE_TO_SCREENING = ("submitted", "checking", "screening")


@pytest.fixture(scope="module")
def conn(schema):
    """One connection for the module, or skip the whole file."""
    try:
        connection = psycopg.connect(schema, connect_timeout=5)
    except Exception as err:  # noqa: BLE001 — any connection problem means skip
        pytest.skip(f"no database reachable: {err}")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def application(conn):
    """A throwaway application at 'screening' with a score, unwound afterwards.

    Scored before any decision exists, which is the order the real code uses and
    the only order the trigger permits.
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
                (f"Freeze Test {uuid.uuid4().hex[:8]}", date(1990, 1, 1)),
            )
            application_id = cur.fetchone()[0]

            for step in ROUTE_TO_SCREENING:
                cur.execute(
                    "update applications set status = %s where id = %s",
                    (step, application_id),
                )

            cur.execute(
                """
                update applications
                   set risk_score = 30,
                       risk_signals = %s,
                       risk_ruleset_version = '2026-09-2',
                       risk_scored_at = now()
                 where id = %s
                """,
                (Jsonb({"score": 30, "signals": []}), application_id),
            )

        yield application_id
        raise psycopg.Rollback(outer)


def decide(conn, application_id, outcome="approved") -> None:
    """Write a decision row, exactly as the handlers do."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into decisions
                (application_id, outcome, decided_by, reason,
                 risk_score_at_decision, risk_ruleset_version_at_decision)
            values (%s, %s, 'system', 'test', 30, '2026-09-2')
            """,
            (application_id, outcome),
        )
        if outcome in ("approved", "rejected"):
            cur.execute(
                "update applications set status = 'decided' where id = %s",
                (application_id,),
            )


#: The four columns the freeze covers, with a value that genuinely differs from
#: the one the fixture wrote — a no-op UPDATE must not be what makes this pass.
FROZEN_COLUMNS = [
    ("risk_score", 99),
    ("risk_signals", Jsonb({"tampered": True})),
    ("risk_ruleset_version", "9999-99-9"),
    ("risk_scored_at", "2030-01-01T00:00:00+00:00"),
]


@pytest.mark.parametrize(
    "column,value", FROZEN_COLUMNS, ids=[c for c, _ in FROZEN_COLUMNS]
)
def test_a_decided_application_refuses_every_risk_column(conn, application, column, value):
    """The headline assertion, issued as raw SQL."""
    decide(conn, application)

    with pytest.raises(psycopg.errors.RestrictViolation) as excinfo:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"update applications set {column} = %s where id = %s",
                    (value, application),
                )
    assert "risk columns are frozen" in str(excinfo.value)


def test_a_referred_case_is_frozen_although_it_is_still_screening(conn, application):
    """The reachable path, and the reason the trigger keys on decisions.

    A referral leaves the application at 'screening' on purpose. If this guard
    had been written as `status = 'decided'` it would have closed the case no
    code can reach and left open the one a redelivered job reaches by design.
    """
    decide(conn, application, outcome="referred")

    with conn.cursor() as cur:
        cur.execute("select status from applications where id = %s", (application,))
        assert cur.fetchone()[0] == "screening", "a referral must not move status"

    with pytest.raises(psycopg.errors.RestrictViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "update applications set risk_score = 45 where id = %s",
                    (application,),
                )


def test_scoring_is_allowed_before_any_decision_exists(conn, application):
    """The counterweight.

    Without this, a trigger that refused every risk write would pass the test
    above while breaking scoring entirely — the classic way a guard stops
    distinguishing between the thing it bans and everything else.
    """
    with conn.cursor() as cur:
        cur.execute(
            "update applications set risk_score = 55 where id = %s", (application,)
        )
        cur.execute("select risk_score from applications where id = %s", (application,))
        assert cur.fetchone()[0] == 55


def test_a_decided_application_can_still_be_updated_in_other_ways(conn, application):
    """The freeze is narrow on purpose.

    A decided application still receives vendor references and updated_at
    bumps. If this fails, the early-return in the trigger has stopped
    distinguishing a re-score from an ordinary write.
    """
    decide(conn, application)

    with conn.cursor() as cur:
        cur.execute(
            "update applications set vendor_applicant_id = %s, updated_at = now() where id = %s",
            ("vendor-session-123", application),
        )
        cur.execute(
            "select vendor_applicant_id from applications where id = %s", (application,)
        )
        assert cur.fetchone()[0] == "vendor-session-123"


def test_rewriting_a_risk_column_to_its_current_value_is_not_an_error(conn, application):
    """A write that changes nothing is not a re-score.

    This matters in practice: _store_assessment short-circuits when the score is
    unchanged, but a caller that issues the UPDATE anyway should not be refused
    for a write with no effect. IS NOT DISTINCT FROM in the trigger is what makes
    this true, and this test is what keeps it true.
    """
    decide(conn, application)

    with conn.cursor() as cur:
        cur.execute(
            "update applications set risk_score = 30 where id = %s", (application,)
        )  # 30 is what the fixture wrote


def test_a_decision_written_by_the_worker_carries_its_ruleset_version(conn, application):
    """The other half of migration 021: the version lands ON the decision.

    Goes through _insert_decision rather than raw SQL, because here the thing
    under test IS the Python — whether the handler passes the version through,
    not whether the column accepts one.
    """
    from scoring import ApplicantProfile, score_application
    from screening_handler import _insert_decision

    assessment = score_application(
        ApplicantProfile(country="GB", vendor_status="Approved", hits=())
    )
    _insert_decision(conn, application, "approved", "system", "test reason", assessment)

    with conn.cursor() as cur:
        cur.execute(
            """select risk_score_at_decision, risk_ruleset_version_at_decision
                 from decisions where application_id = %s""",
            (application,),
        )
        score, version = cur.fetchone()

    assert score == assessment.score
    assert version == assessment.ruleset_version, (
        "the decision must record the ruleset version it was taken under; "
        "without it the row depends on applications still saying what it said"
    )
