"""Migration 026: `decisions` refuses UPDATE, DELETE and TRUNCATE.

The decision row is the answer to "why was this customer refused". Until
migration 026 it was the *current* answer rather than the only one: every
constraint on the table governed what a decision could SAY, and none stopped it
being rewritten afterwards.

What these tests have to establish is both halves. A blanket lock on the table
would pass every refusal test here and break the system entirely, so the
refusals are paired with a counterweight asserting that both decision-writing
paths still work. An immutability test that a dead table would satisfy proves
nothing — the same reasoning as the applicant-disclosure suite, where a blank
page passes every leak assertion.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")

from psycopg.types.json import Jsonb  # noqa: E402

#: started -> submitted -> checking -> screening: the only route to a state an
#: application may be decided from. Mirrors application_transitions.
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
def decided(conn):
    """An application at 'screening' with one decision, unwound afterwards.

    Rolled back rather than deleted, because deleting is the thing this file
    proves impossible. The rollback is a transaction abort, which no trigger
    sees — the same property that lets the rest of the suite clean up at all.
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
                (f"Append Only {uuid.uuid4().hex[:8]}", date(1990, 1, 1)),
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

            cur.execute(
                """
                insert into decisions
                    (application_id, outcome, decided_by, reason,
                     risk_score_at_decision, risk_ruleset_version_at_decision)
                values (%s, 'approved', 'system', 'original reason', 30,
                        '2026-09-2')
                returning id
                """,
                (application_id,),
            )
            decision_id = cur.fetchone()[0]

            cur.execute(
                "update applications set status = 'decided' where id = %s",
                (application_id,),
            )

        yield application_id, decision_id
        raise psycopg.Rollback(outer)


#: Each column a rewrite would target, with a value that genuinely differs from
#: what the fixture wrote — a no-op UPDATE must not be what makes these pass.
#: `outcome` is first because flipping a refusal to an approval is the whole
#: threat model.
REWRITES = [
    ("outcome", "rejected"),
    ("reason", "rewritten after the fact"),
    ("decided_by", "staff:someone_else"),
    ("risk_score_at_decision", 99),
    ("risk_ruleset_version_at_decision", "2026-10-1"),
]


@pytest.mark.parametrize("column,value", REWRITES, ids=[c for c, _ in REWRITES])
def test_a_recorded_decision_cannot_be_rewritten(conn, decided, column, value):
    """The headline assertion: no column of a decision can be edited."""
    _, decision_id = decided

    with pytest.raises(psycopg.errors.RestrictViolation) as excinfo:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"update decisions set {column} = %s where id = %s",
                    (value, decision_id),
                )
    assert "append-only" in str(excinfo.value)


def test_a_recorded_decision_cannot_be_deleted(conn, decided):
    """A decision that can be deleted is a decision that can be disowned."""
    _, decision_id = decided

    with pytest.raises(psycopg.errors.RestrictViolation) as excinfo:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("delete from decisions where id = %s", (decision_id,))
    assert "append-only" in str(excinfo.value)


def test_truncate_is_refused_by_our_own_trigger(conn):
    """TRUNCATE, and it is worth being exact about what refuses it.

    For `rulesets` the foreign keys reject a TRUNCATE before any BEFORE TRUNCATE
    trigger runs, so that test deliberately asserts only that the statement
    fails. Here nothing holds a foreign key TO decisions, so there is no
    referential error to fire first and the refusal is genuinely ours. Asserting
    the message is therefore meaningful rather than accidental.
    """
    with pytest.raises(psycopg.errors.RestrictViolation) as excinfo:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("truncate decisions")
    assert "append-only" in str(excinfo.value)


@pytest.mark.parametrize(
    "statement",
    [
        "update decisions set outcome = 'rejected' where false",
        "delete from decisions where false",
    ],
    ids=["update", "delete"],
)
def test_a_statement_matching_no_rows_is_still_refused(conn, statement):
    """FOR EACH STATEMENT, not FOR EACH ROW — and this is what proves it.

    A row-level trigger would let `where false` through silently, because it
    never fires. That matters less for the zero-row case itself than for what it
    reveals: a row-level trigger is one `where` clause away from being bypassed,
    and this assertion fails the moment someone converts it.
    """
    with pytest.raises(psycopg.errors.RestrictViolation) as excinfo:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(statement)
    assert "append-only" in str(excinfo.value)


def test_the_decision_survives_a_refused_rewrite(conn, decided):
    """The refusal is not merely an error: the original is still there.

    Asserting the exception alone would pass if the statement raised *after*
    writing, so this reads the row back.
    """
    _, decision_id = decided

    with pytest.raises(psycopg.errors.RestrictViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "update decisions set outcome = 'rejected' where id = %s",
                    (decision_id,),
                )

    with conn.cursor() as cur:
        cur.execute(
            "select outcome, reason from decisions where id = %s", (decision_id,)
        )
        assert cur.fetchone() == ("approved", "original reason")


def test_the_error_says_what_to_do_instead(conn, decided):
    """The hint carries the design, because the error is where someone meets it.

    A refusal that does not say "record a superseding decision" invites the
    reader to conclude the table is broken and reach for a way around it.
    """
    _, decision_id = decided

    with pytest.raises(psycopg.errors.RestrictViolation) as excinfo:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "update decisions set reason = 'x' where id = %s", (decision_id,)
                )
    assert "superseding" in str(excinfo.value.diag.message_hint or "")


def test_inserting_decisions_still_works(conn, decided):
    """THE COUNTERWEIGHT. Every assertion above passes on a dead table.

    A referral alongside an existing verdict is the insert the partial unique
    index deliberately permits, so this exercises the path that must keep
    working rather than the one that must not.
    """
    application_id, _ = decided

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into decisions
                (application_id, outcome, decided_by, reason,
                 risk_score_at_decision, risk_ruleset_version_at_decision)
            values (%s, 'referred', 'staff:reviewer', 'second look', 30,
                    '2026-09-2')
            returning id
            """,
            (application_id,),
        )
        assert cur.fetchone()[0] is not None

        cur.execute(
            "select count(*) from decisions where application_id = %s",
            (application_id,),
        )
        assert cur.fetchone()[0] == 2


def test_the_one_terminal_decision_rule_still_applies(conn, decided):
    """And the older guarantee is undisturbed by the new one.

    Append-only and one-verdict-per-case are different rules; making the table
    immutable must not have replaced the unique index with a trigger that only
    looks like it.
    """
    application_id, _ = decided

    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into decisions
                        (application_id, outcome, decided_by, reason,
                         risk_score_at_decision, risk_ruleset_version_at_decision)
                    values (%s, 'rejected', 'staff:reviewer', 'contradiction', 30,
                            '2026-09-2')
                    """,
                    (application_id,),
                )
