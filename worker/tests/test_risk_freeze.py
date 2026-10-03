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

from scoring import RULESET_VERSION  # noqa: E402

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


def test_a_decision_without_a_ruleset_version_is_refused(conn, application):
    """Migration 022, asserted as raw SQL.

    Four sites insert decisions, in two languages. Three are covered by the
    Python tests here; the fourth is the officer's action in
    web/src/app/desk/[id]/actions.ts, and /web has a typechecker but no test
    runner, so there is no place to assert it from that side. The constraint is
    what covers it — which is the reason for putting the rule in the database
    rather than teaching four callers to remember it.
    """
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into decisions
                        (application_id, outcome, decided_by, reason,
                         risk_score_at_decision)
                    values (%s, 'approved', 'system', 'no version supplied', 30)
                    """,
                    (application,),
                )
    assert "decisions_ruleset_version_present" in str(excinfo.value)


def test_the_constraint_does_not_disturb_rows_that_predate_it(conn):
    """NOT VALID means "from here on", and that is deliberate.

    021 left the column NULL wherever the data could not establish a version.
    If this constraint had been added validating, those rows would have had to
    be filled with a guess — the one outcome the NULL was chosen to avoid. A
    pre-existing NULL must therefore still be readable and updatable in ways
    that do not touch the column.
    """
    with conn.cursor() as cur:
        cur.execute(
            """select count(*) from decisions
                where risk_ruleset_version_at_decision is null"""
        )
        unknown = cur.fetchone()[0]

    # Not an assertion about the number: on a freshly migrated test schema it is
    # zero. The assertion is that asking does not raise, i.e. the constraint is
    # not validating rows it was told not to validate.
    assert unknown >= 0


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


# ---------------------------------------------------------------------------
# Migration 023: no officer decision while evidence is stuck
# ---------------------------------------------------------------------------


def park_screening_job(conn, application_id, error="boom") -> int:
    """A parked screening.run job, shaped exactly as fail_job leaves one."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, payload, status, last_error)
            values ('screening.run', %s, 'parked', %s)
            returning id
            """,
            (Jsonb({"application_id": str(application_id)}), error),
        )
        return cur.fetchone()[0]


def test_an_officer_cannot_decide_while_screening_evidence_is_parked(conn, application):
    """The interim guard.

    A parked screening.run means the system found something it could not write
    down — migration 020 refuses to overwrite the evidence a referral was made
    on, so the job raises and parks. Deciding now would be deciding without it.
    """
    decide(conn, application, outcome="referred")
    job_id = park_screening_job(conn, application)

    with pytest.raises(psycopg.errors.RestrictViolation) as excinfo:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into decisions
                        (application_id, outcome, decided_by, reason,
                         risk_score_at_decision, risk_ruleset_version_at_decision)
                    values (%s, 'approved', 'staff:alice', 'Looks fine to me.',
                            30, '2026-09-2')
                    """,
                    (application,),
                )
    assert "could not be recorded" in str(excinfo.value)
    assert str(job_id) in str(excinfo.value)


def test_the_automatic_path_is_not_blocked_by_a_parked_job(conn, application):
    """The exemption, and why it is not an oversight.

    A parked job on an application with no decision yet would otherwise stop the
    worker recording the screening that just succeeded. The system writing down
    what it found is never what this rule wants to prevent; a human signing off
    over unrecorded evidence is.
    """
    park_screening_job(conn, application)

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into decisions
                (application_id, outcome, decided_by, reason,
                 risk_score_at_decision, risk_ruleset_version_at_decision)
            values (%s, 'referred', 'system', 'Score 45: sanctions near-match.',
                    45, '2026-09-2')
            """,
            (application,),
        )
        cur.execute(
            "select count(*) from decisions where application_id = %s", (application,)
        )
        assert cur.fetchone()[0] == 1


def test_an_officer_can_decide_normally_when_nothing_is_parked(conn, application):
    """The normal path — the counterweight.

    Without this, a guard that refused every officer decision would satisfy the
    test above while making the desk useless.
    """
    decide(conn, application, outcome="referred")

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into decisions
                (application_id, outcome, decided_by, reason,
                 risk_score_at_decision, risk_ruleset_version_at_decision)
            values (%s, 'approved', 'staff:alice', 'Different date of birth.',
                    30, '2026-09-2')
            """,
            (application,),
        )
        cur.execute(
            """select count(*) from decisions
                where application_id = %s and decided_by like 'staff:%%'""",
            (application,),
        )
        assert cur.fetchone()[0] == 1


def test_a_job_that_is_merely_retrying_does_not_block_the_desk(conn, application):
    """'queued' is not 'parked', and the difference is deliberate.

    A retrying job goes back to 'queued' with last_error set. Blocking on that
    would freeze the desk for every transient blip, including first deliveries
    about to succeed. The documented cost is a window: a job carrying new
    evidence that has not parked yet. The case view warns on the broader
    condition; the database refuses only on the unambiguous one.
    """
    decide(conn, application, outcome="referred")
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, payload, status, last_error)
            values ('screening.run', %s, 'queued', 'transient')
            """,
            (Jsonb({"application_id": str(application)}),),
        )
        cur.execute(
            """
            insert into decisions
                (application_id, outcome, decided_by, reason,
                 risk_score_at_decision, risk_ruleset_version_at_decision)
            values (%s, 'approved', 'staff:alice', 'Nothing blocking this.',
                    30, '2026-09-2')
            """,
            (application,),
        )


def test_a_parked_job_for_a_different_application_is_irrelevant(conn, application):
    """The predicate reads the payload, so it had better read it correctly."""
    decide(conn, application, outcome="referred")
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, payload, status, last_error)
            values ('screening.run', %s, 'parked', 'someone else')
            """,
            (Jsonb({"application_id": str(uuid.uuid4())}),),
        )
        cur.execute(
            """
            insert into decisions
                (application_id, outcome, decided_by, reason,
                 risk_score_at_decision, risk_ruleset_version_at_decision)
            values (%s, 'approved', 'staff:alice', 'Not my parked job.',
                    30, '2026-09-2')
            """,
            (application,),
        )


# ---------------------------------------------------------------------------
# The case view's two conditions: one blocks, one only warns
# ---------------------------------------------------------------------------

#: The predicates the case view queries on, copied from
#: web/src/app/desk/[id]/page.tsx.
#:
#: WHAT THESE TESTS DO AND DO NOT COVER, SINCE IT MATTERS
#:
#: They assert the CONDITIONS, not the rendering. /web has a typechecker and no
#: test runner, so there is nowhere to assert that a <div> appears; the server
#: actions and pages are only covered by the compiler and by the live-page
#: fetches in test_applicant_disclosure.py, which run unauthenticated and cannot
#: reach a case view behind requireStaff().
#:
#: So what is proven here is that the data distinguishes the three states the
#: page branches on, and that the branch which must not block does not. A
#: regression in the JSX itself would not be caught, and that is a real gap
#: rather than an oversight — it is the same gap migration 023 exists to make
#: survivable, by refusing in the database rather than relying on the page.
BLOCKING_SQL = """
    select id from jobs
     where job_type = 'screening.run' and status = 'parked'
       and payload->>'application_id' = %s
"""
WARNING_SQL = """
    select id from jobs
     where job_type = 'screening.run' and status = 'queued'
       and last_error is not null
       and payload->>'application_id' = %s
"""


def queue_retrying_job(conn, application_id, error="transient failure") -> int:
    """A screening job that failed once and is waiting to try again."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, payload, status, last_error, run_after)
            values ('screening.run', %s, 'queued', %s, now() + interval '30 seconds')
            returning id
            """,
            (Jsonb({"application_id": str(application_id)}), error),
        )
        return cur.fetchone()[0]


def matches(conn, sql, application_id) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(sql, (str(application_id),))
        return [r[0] for r in cur.fetchall()]


def test_a_retrying_job_raises_the_warning_and_not_the_block(conn, application):
    """The warning condition is true and the blocking condition is not.

    These are the two branches the case view chooses between. If a retrying job
    satisfied both, the page would show the blocking message and the officer
    would be stopped by something that may yet resolve itself.
    """
    decide(conn, application, outcome="referred")
    job_id = queue_retrying_job(conn, application)

    assert matches(conn, WARNING_SQL, application) == [job_id]
    assert matches(conn, BLOCKING_SQL, application) == []


def test_the_warning_does_not_prevent_a_decision(conn, application):
    """The half that matters: warned, not stopped.

    Deliberately asserts the INSERT succeeds rather than that no exception is
    raised in the page, because the database is what decides this.
    """
    decide(conn, application, outcome="referred")
    queue_retrying_job(conn, application)

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into decisions
                (application_id, outcome, decided_by, reason,
                 risk_score_at_decision, risk_ruleset_version_at_decision)
            values (%s, 'approved', 'staff:alice', 'Decided with the retry noted.',
                    30, %s)
            """,
            (application, RULESET_VERSION),
        )
        cur.execute(
            """select count(*) from decisions
                where application_id = %s and decided_by like 'staff:%%'""",
            (application,),
        )
        assert cur.fetchone()[0] == 1


def test_a_parked_job_raises_the_block_and_not_the_warning(conn, application):
    """The other branch, so the two cannot quietly become the same condition."""
    decide(conn, application, outcome="referred")
    job_id = park_screening_job(conn, application)

    assert matches(conn, BLOCKING_SQL, application) == [job_id]
    assert matches(conn, WARNING_SQL, application) == []


def test_a_clean_case_raises_neither(conn, application):
    """The counterweight: no job, no warning, no block, form shown."""
    decide(conn, application, outcome="referred")

    assert matches(conn, BLOCKING_SQL, application) == []
    assert matches(conn, WARNING_SQL, application) == []


def test_a_succeeded_job_raises_neither(conn, application):
    """'done' with no error is the ordinary end state and must be silent."""
    decide(conn, application, outcome="referred")
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, payload, status)
            values ('screening.run', %s, 'done')
            """,
            (Jsonb({"application_id": str(application)}),),
        )

    assert matches(conn, BLOCKING_SQL, application) == []
    assert matches(conn, WARNING_SQL, application) == []
