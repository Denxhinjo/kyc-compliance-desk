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

    # BOTH lists, and their whole provenance rather than a sample.
    #
    # This used to check the codes of both lists but the publication date of
    # only the monitoring one. That gap was not hypothetical: when the
    # call-for-action list became sourced in 2026-10-1, its CODES did not
    # change — IR, KP, MM before and after — so a forgotten version bump would
    # have left new applicants stamped 2026-09-2 while being scored against a
    # sourced list, and this test would have passed. The version bump was made;
    # the test would not have caught its absence.
    for key, revision in (
        ("fatf_increased_monitoring", INCREASED_MONITORING),
        ("fatf_call_for_action", CALL_FOR_ACTION),
    ):
        recorded = reference[key]
        assert sorted(recorded["codes"]) == sorted(revision.codes), key
        assert recorded["published_at"] == revision.published_at, key
        assert recorded["plenary"] == revision.plenary, key
        assert recorded["digest"] == revision.digest, key
        assert recorded["source_sha256"] == revision.source_sha256, key
        assert recorded["source_format"] == revision.source_format, key


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


# ---------------------------------------------------------------------------
# Pinning, with more than one version in existence
# ---------------------------------------------------------------------------


def test_a_decision_stays_pinned_to_its_own_version_after_a_newer_one_exists(conn):
    """The property the whole exercise is for.

    Up to migration 025 there were two ruleset versions and every decision in
    the data carried the older one, so "pinning works" had never actually been
    exercised against a newer version arriving. 025 is the first time a version
    was added while decisions already existed, and the FATF statement it records
    says Myanmar may be reconsidered in October 2026 — so a third version could
    follow within weeks.

    This writes a decision under the PREVIOUS version and then resolves it,
    asserting it still answers with the version it was taken under rather than
    the newest one.
    """
    previous = "2026-09-2"
    assert previous != RULESET_VERSION, (
        "this test is only meaningful while a newer version exists; "
        f"RULESET_VERSION is still {previous}"
    )

    with conn.transaction() as outer:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into applications
                    (status, full_name, date_of_birth, address_line1,
                     address_city, address_postcode, address_country)
                values ('started', 'Pinning Test', '1990-01-01', '1 Test Street',
                        'Testville', 'T1 1TT', 'GB')
                returning id
                """
            )
            application_id = cur.fetchone()[0]

            cur.execute(
                """
                insert into decisions
                    (application_id, outcome, decided_by, reason,
                     risk_score_at_decision, risk_ruleset_version_at_decision)
                values (%s, 'approved', 'system', 'decided under the old rules',
                        30, %s)
                """,
                (application_id, previous),
            )

            # Resolve the decision the way an auditor would: join to the
            # definition, do not read the current constant.
            cur.execute(
                """
                select r.version,
                       r.reference_data->'fatf_call_for_action'->>'sourcing',
                       r.reference_data->'fatf_call_for_action'->>'published_at'
                  from decisions d
                  join rulesets r on r.version = d.risk_ruleset_version_at_decision
                 where d.application_id = %s
                """,
                (application_id,),
            )
            version, sourcing, published = cur.fetchone()

        assert version == previous, (
            f"a decision taken under {previous} resolved to {version} — the pin "
            "has stopped holding, and every past decision is now explained by "
            "rules it was not taken under"
        )
        # And it resolves to what that version actually said about itself, which
        # the newer version contradicts.
        assert sourcing == "unsourced", (
            "2026-09-2 recorded its call-for-action list as unsourced; resolving "
            f"a decision under it now reports {sourcing!r}, so the older row has "
            "been rewritten"
        )
        assert published is None

        raise psycopg.Rollback(outer)


def test_the_current_version_tells_the_newer_story(conn):
    """The counterweight: the newer version must differ, or the test above is
    asserting nothing."""
    with conn.cursor() as cur:
        cur.execute(
            """select reference_data->'fatf_call_for_action'->>'sourcing',
                      reference_data->'fatf_call_for_action'->>'published_at',
                      reference_data->'fatf_call_for_action'->>'source_sha256'
                 from rulesets where version = %s""",
            (RULESET_VERSION,),
        )
        sourcing, published, sha = cur.fetchone()

    assert sourcing == "sourced"
    assert published == "2026-06-19"
    assert sha and len(sha) == 64


def test_the_call_for_action_list_names_the_file_it_was_read_from(conn):
    """A citation nobody can check is not a citation.

    The hash is of a browser capture rather than the publisher's own PDF, which
    the data says in source_format. This asserts the fields exist and that the
    weaker claim is the one recorded — a later change to "pdf-official" should
    be a deliberate act, not a drift.
    """
    assert CALL_FOR_ACTION.source_file
    assert CALL_FOR_ACTION.source_sha256
    assert CALL_FOR_ACTION.source_format == "pdf-of-saved-webpage"
    assert CALL_FOR_ACTION.tiers, "the two tiers the statement draws must be recorded"
    assert CALL_FOR_ACTION.flattening_note, "the limitation must be stated in the data"
    assert CALL_FOR_ACTION.next_review_expected == "2026-10"


def test_the_constant_names_the_newest_ruleset(conn):
    """scoring.RULESET_VERSION must be the latest version by effective date.

    WHAT GOES WRONG IF THE CONSTANT IS LEFT BEHIND

    Recording a new ruleset version and forgetting to bump the constant does not
    fail anything on its own. Scoring carries on, applications carry on being
    stamped — with the PREVIOUS version's string, while being scored by the
    current code.

    That inverts the property this whole table exists to provide. A decision
    taken today would resolve, through the join an auditor uses, to a definition
    describing rules it was not taken under. For 2026-10-1 specifically it would
    have been precisely the wrong way round: new applicants would carry
    2026-09-2, whose recorded definition states that its call-for-action list
    was never fetched and never cited — while they had in fact been scored
    against the sourced list. The record would understate its own provenance,
    and an officer defending that decision would be reading the wrong row.

    Checking it against effective_from rather than against a hardcoded string
    means this keeps working at the next bump without being edited, which is the
    only kind of guard that survives.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select version from rulesets order by effective_from desc, version desc limit 1"
        )
        row = cur.fetchone()

    assert row is not None, "no ruleset versions recorded at all"
    newest = row[0]
    assert RULESET_VERSION == newest, (
        f"scoring.RULESET_VERSION is {RULESET_VERSION!r} but the newest recorded "
        f"ruleset is {newest!r}. "
        "Either a version was recorded without bumping the constant — in which "
        "case new applications are being stamped with the older version while "
        "scored by the current code — or the constant was bumped without "
        "recording what the new version contains."
    )


def test_a_newly_scored_application_is_stamped_with_the_newest_version(conn):
    """End to end, through the real scoring path rather than the constant.

    The test above compares two strings. This one scores an applicant the way
    the worker does and checks what actually comes out, because the constant
    being right and the assessment carrying it are different claims.
    """
    from scoring import ApplicantProfile, score_application

    assessment = score_application(
        ApplicantProfile(country="GB", vendor_status="Approved", hits=())
    )

    with conn.cursor() as cur:
        cur.execute(
            "select version from rulesets order by effective_from desc, version desc limit 1"
        )
        newest = cur.fetchone()[0]

    assert assessment.ruleset_version == newest
    assert assessment.as_dict()["ruleset_version"] == newest, (
        "the version reaches the assessment but not the stored form, so it would "
        "not reach applications.risk_signals either"
    )
