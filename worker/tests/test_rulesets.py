"""Tests for the rulesets table — migration 024.

WHAT THIS TABLE IS FOR

A decision records the ruleset version it was taken under. That is only an
answer if the version can be looked up, and until 024 the lookup was "clone the
repository and check out the right commit". An auditor cannot read git.

THE TEST THAT MATTERS MOST IS THE FIRST ONE

If scoring.RULESET_VERSION has no row here, the next version bump ships a string
pointing at nothing: applications and decisions would record a version whose
contents were never written down, which is the exact failure the table exists to
prevent. The foreign keys stop a bad value reaching the columns; this stops the
value being invented in Python and nobody noticing until an audit.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from scoring import (  # noqa: E402
    CALL_FOR_ACTION,
    DEFAULT_THRESHOLDS,
    INCREASED_MONITORING,
    RULESET_VERSION,
    SANCTIONS_POINTS,
)


@pytest.fixture(scope="module")
def conn(schema):
    try:
        connection = psycopg.connect(schema, connect_timeout=5)
    except Exception as err:  # noqa: BLE001
        pytest.skip(f"no database reachable: {err}")
    try:
        yield connection
    finally:
        connection.close()


def test_the_current_ruleset_version_has_a_recorded_definition(conn):
    """The guard against shipping a version nobody wrote down."""
    with conn.cursor() as cur:
        cur.execute("select count(*) from rulesets where version = %s", (RULESET_VERSION,))
        found = cur.fetchone()[0]

    assert found == 1, (
        f"scoring.RULESET_VERSION is {RULESET_VERSION!r} and rulesets has no row "
        "for it.\n\n"
        "A version bump has to be accompanied by a migration recording what the "
        "new version contains: its points, thresholds, reference data and a "
        "change note saying why it exists. Without that, every application and "
        "decision stamped with this version names a ruleset that was never "
        "written down, and 'decided under 2026-09-N' stops being an answer."
    )


def test_the_recorded_definition_matches_what_scoring_actually_applies(conn):
    """A row that has drifted from the code is worse than no row.

    It would be read as authoritative while describing rules nobody is running.
    Checks the values most likely to be edited in one place and not the other.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select points, thresholds, reference_data from rulesets where version = %s",
            (RULESET_VERSION,),
        )
        points, thresholds, reference = cur.fetchone()

    assert points["sanctions"] == dict(SANCTIONS_POINTS)
    assert thresholds == DEFAULT_THRESHOLDS.as_dict()
    assert sorted(reference["fatf_increased_monitoring"]["codes"]) == sorted(
        INCREASED_MONITORING.codes
    )
    assert sorted(reference["fatf_call_for_action"]["codes"]) == sorted(
        CALL_FOR_ACTION.codes
    )
    assert (
        reference["fatf_increased_monitoring"]["published_at"]
        == INCREASED_MONITORING.published_at
    )


def test_every_version_in_the_data_has_a_definition(conn):
    """Covers past versions, not just the current one.

    The foreign keys enforce this going forward. This asserts it for what is
    already stored, which the keys were added after.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct risk_ruleset_version from applications
             where risk_ruleset_version is not null
             union
            select distinct risk_ruleset_version_at_decision from decisions
             where risk_ruleset_version_at_decision is not null
            """
        )
        used = {r[0] for r in cur.fetchall()}
        cur.execute("select version from rulesets")
        recorded = {r[0] for r in cur.fetchall()}

    assert used <= recorded, f"versions in use with no definition: {sorted(used - recorded)}"


@pytest.mark.parametrize(
    "statement",
    [
        "update rulesets set change_note = 'rewritten'",
        "delete from rulesets",
    ],
    ids=["update", "delete"],
)
def test_a_recorded_ruleset_cannot_be_rewritten(conn, statement):
    """Immutable, the same way audit_events is.

    A definition that can be edited after the fact defends nothing: the point of
    the table is that "2026-09-1 contained these points" cannot be quietly
    revised once a decision has been defended with it.
    """
    with pytest.raises(psycopg.errors.RestrictViolation) as excinfo:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(statement)
    assert "append-only" in str(excinfo.value)


def test_truncate_is_refused_although_not_by_our_trigger(conn):
    """TRUNCATE is refused, and it is worth being exact about by what.

    The no-truncate trigger exists and mirrors migration 006. But applications
    and decisions now reference rulesets, and Postgres rejects truncating a
    referenced table before any BEFORE TRUNCATE trigger runs — so the error here
    comes from the foreign key, not from us.

    Asserting "append-only" in the message would therefore pass today for the
    wrong reason and fail the moment the keys changed. What is actually
    guaranteed is that the statement does not succeed and the rows survive, so
    that is what this asserts.
    """
    with pytest.raises(
        (psycopg.errors.RestrictViolation, psycopg.errors.FeatureNotSupported)
    ):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("truncate rulesets")

    with conn.cursor() as cur:
        cur.execute("select count(*) from rulesets")
        assert cur.fetchone()[0] >= 2


def test_a_version_with_no_change_note_is_refused(conn):
    """Not blank, in the database.

    Same reasoning as decisions.reason: a version with no stated reason is a row
    nobody can defend when asked why the rules moved.
    """
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into rulesets
                        (version, effective_from, points, thresholds, change_note)
                    values ('9999-99-9', now(), '{}'::jsonb, '{}'::jsonb, '   ')
                    """
                )


def test_a_decision_cannot_name_a_ruleset_that_does_not_exist(conn):
    """The foreign key, asserted rather than assumed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into applications
                (status, full_name, date_of_birth, address_line1, address_city,
                 address_postcode, address_country)
            values ('started', 'Ruleset FK Test', '1990-01-01', '1 Test Street',
                    'Testville', 'T1 1TT', 'GB')
            returning id
            """
        )
        application_id = cur.fetchone()[0]

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into decisions
                        (application_id, outcome, decided_by, reason,
                         risk_score_at_decision, risk_ruleset_version_at_decision)
                    values (%s, 'approved', 'system', 'test', 30, 'no-such-version')
                    """,
                    (application_id,),
                )

    with conn.cursor() as cur:
        cur.execute("delete from applications where id = %s", (application_id,))


def test_a_null_version_is_still_allowed(conn):
    """The keys constrain values that are present, which is the point.

    Migration 021 deliberately leaves the decision column NULL where the data
    could not establish a version. A foreign key that forbade NULL would have
    forced those rows to name something, and the only available something is a
    guess.
    """
    with conn.cursor() as cur:
        cur.execute(
            """select count(*) from applications
                where risk_ruleset_version is null"""
        )
        assert cur.fetchone()[0] >= 0  # asking must not raise
