"""Two officers, one case — and the provenance chain behind the decision.

The desk action itself is TypeScript and is not executed here. What these tests
run is the SQL sequence it performs, on two real connections that genuinely
block on each other, plus the constraint underneath it. Stated plainly because
it matters: a bug introduced in `actions.ts` above the SQL would not be caught
by a Python test, and the README says so.

One exception is asserted textually rather than behaviourally — see
`test_the_desk_lock_has_not_quietly_become_skip_locked`.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

import screening_handler  # noqa: E402
from jobs import Job, enqueue_job  # noqa: E402
from screening import sources  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DESK_ACTION = REPO_ROOT / "web" / "src" / "app" / "desk" / "[id]" / "actions.ts"


def decide_like_the_desk(conn, application_id: str, officer: str, outcome: str) -> dict:
    """The desk action's transaction, in SQL, without the TypeScript around it.

    Kept deliberately close to `actions.ts`: lock, re-read, insert, transition.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "select status from applications where id = %s for update",
                (application_id,),
            )
            if cur.fetchone() is None:
                return {"error": "No such case."}

            cur.execute(
                """
                select decided_by from decisions
                 where application_id = %s and outcome in ('approved', 'rejected')
                 limit 1
                """,
                (application_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                return {"already_decided_by": existing[0]}

            cur.execute(
                """
                insert into decisions
                    (application_id, outcome, decided_by, reason, risk_score_at_decision)
                values (%s, %s, %s, %s, null)
                """,
                (
                    application_id,
                    outcome,
                    officer,
                    "Reviewed the document check and the screening matches.",
                ),
            )
            cur.execute(
                "update applications set status = 'decided' "
                "where id = %s and status = 'screening'",
                (application_id,),
            )
    return {}


# ---------------------------------------------------------------------------
# The race
# ---------------------------------------------------------------------------


def test_two_officers_deciding_one_case_produce_one_decision(
    db, make_application, new_connection
):
    """Both officers act; one decides, the other is told who did.

    The second transaction BLOCKS on `for update` rather than skipping the row,
    so by the time it re-reads there is something to find. That is the whole
    reason the desk uses a different modifier from the queue: the loser here is
    a person, and "nothing happened" would be the worst possible answer.
    """
    application_id = make_application("screening")
    start = threading.Barrier(2)
    outcomes: dict[str, dict] = {}

    def attempt(officer: str, verdict: str):
        conn = new_connection(officer)
        start.wait(timeout=10)
        outcomes[officer] = decide_like_the_desk(conn, application_id, officer, verdict)

    threads = [
        threading.Thread(target=attempt, args=("staff:alice", "approved")),
        threading.Thread(target=attempt, args=("staff:bob", "rejected")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with db.cursor() as cur:
        cur.execute(
            "select decided_by, outcome from decisions where application_id = %s",
            (application_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 1, f"two officers, {len(rows)} decisions: {rows}"
    winner = rows[0][0]

    losers = [o for o in outcomes.values() if "already_decided_by" in o]
    assert len(losers) == 1, f"exactly one officer should be refused: {outcomes}"
    assert losers[0]["already_decided_by"] == winner, (
        "the refused officer must be told WHO decided it, not just that they lost"
    )


def test_the_constraint_refuses_a_second_decision_even_without_the_lock(
    db, make_application
):
    """The backstop, tested with every guard above it removed.

    The lock and the re-read are correctness in the application. This is
    correctness in the database: even with a bug in every layer above,
    `decisions_one_terminal_per_application` makes the second row not exist.
    """
    application_id = make_application("screening")
    with db.cursor() as cur:
        cur.execute(
            "insert into decisions (application_id, outcome, decided_by, reason) "
            "values (%s, 'approved', 'staff:alice', 'Documents and screening checked.')",
            (application_id,),
        )

    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            with db.cursor() as cur:
                cur.execute(
                    "insert into decisions (application_id, outcome, decided_by, reason) "
                    "values (%s, 'rejected', 'staff:bob', 'Documents and screening checked.')",
                    (application_id,),
                )


def test_a_referral_and_a_verdict_can_coexist(db, make_application):
    """The index is PARTIAL, and that is load-bearing.

    Every reviewed case has a system 'referred' decision and then a human
    verdict. An index over all outcomes would make the normal path impossible.
    """
    application_id = make_application("screening")
    with db.cursor() as cur:
        cur.execute(
            "insert into decisions (application_id, outcome, decided_by, reason) "
            "values (%s, 'referred', 'system', 'Score 45: sanctions near-match.')",
            (application_id,),
        )
    decide_like_the_desk(db, application_id, "staff:alice", "approved")

    with db.cursor() as cur:
        cur.execute(
            "select count(*) from decisions where application_id = %s", (application_id,)
        )
        assert cur.fetchone()[0] == 2


def test_the_desk_lock_has_not_quietly_become_skip_locked():
    """A textual guard, and honest about being one.

    Swapping `for update` for `for update skip locked` in the desk action would
    leave every database guarantee intact — one decision, enforced by the
    index — while breaking the human behaviour: the second officer's lock
    returns no row, so they are told "No such case" about a case they are
    looking at.

    No Python test can catch that, because the code is TypeScript and the
    database is not harmed by it. Reading the file is the honest alternative to
    pretending it is covered.
    """
    # Comments stripped first — the file EXPLAINS skip locked at length, so a
    # naive search finds the prose rather than the query.
    code = re.sub(r"//.*", "", DESK_ACTION.read_text(encoding="utf-8")).lower()

    assert "for update" in code, "the desk action no longer locks the row at all"
    assert "skip locked" not in code, (
        "the desk action uses SKIP LOCKED. The database would still permit only "
        "one decision, but the losing officer would be told the case does not "
        "exist instead of who decided it."
    )


# ---------------------------------------------------------------------------
# Snapshot provenance
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_index():
    """The committed fixture, loaded fresh.

    `load_index` caches globally for the life of a process, which is right for a
    worker and wrong for a test that wants to load the same list repeatedly.
    """
    sources._cached = None
    index = sources.load_synthetic()
    yield index
    sources._cached = None


def test_loading_the_same_list_three_times_registers_one_snapshot(
    db, synthetic_index, count_rows
):
    """A worker restart must not look like the list changing.

    Three loads, one row, and the same id read back each time — which is the
    part that could silently break: ON CONFLICT DO NOTHING returns no row, so
    `ensure_snapshot` has to go and fetch the id it collided with. Getting that
    wrong would return None and violate the foreign key on every match.
    """
    ids = [sources.ensure_snapshot(db, synthetic_index) for _ in range(3)]

    assert ids[0] is not None
    assert len(set(ids)) == 1, f"three loads produced different snapshot ids: {ids}"
    assert (
        count_rows(
            "sanctions_snapshots",
            "source = %s and content_hash = %s",
            (synthetic_index.source, synthetic_index.content_hash),
        )
        == 1
    )


def test_different_content_is_a_different_snapshot(db, synthetic_index):
    """The other half: when the list really does change, it must show."""
    first = sources.ensure_snapshot(db, synthetic_index)
    changed = sources.build_index(
        list(synthetic_index.entries)[:5],
        source=synthetic_index.source,
        content_hash="f" * 64,
        published_at="2026-01-01",
    )
    second = sources.ensure_snapshot(db, changed)

    assert second != first, "changed content must register as a new snapshot"
    with db.cursor() as cur:
        cur.execute(
            "select record_count, published_at from sanctions_snapshots where id = %s",
            (second,),
        )
        record_count, published_at = cur.fetchone()
    assert record_count == 5
    assert str(published_at) == "2026-01-01"


def test_the_synthetic_fixture_has_no_invented_publication_date(db, synthetic_index):
    """NULL means unknown, and unknown is the truth here.

    The fixture is fabricated and has no publisher. A plausible date would make
    a made-up list look like a dated authority, which is the opposite of what
    the table is for.
    """
    snapshot_id = sources.ensure_snapshot(db, synthetic_index)
    with db.cursor() as cur:
        cur.execute(
            "select published_at from sanctions_snapshots where id = %s", (snapshot_id,)
        )
        assert cur.fetchone()[0] is None


def test_a_match_points_at_the_list_it_was_found_in(db, make_application, monkeypatch):
    """The chain an auditor walks: decision -> match -> list version.

    Runs the real screening handler against an applicant whose name is on the
    committed fixture, then follows the foreign key back.
    """
    monkeypatch.setattr(screening_handler, "SANCTIONS_SOURCE", "synthetic")
    sources._cached = None
    application_id = make_application("screening", full_name="Ahmed Hassan")

    job_id = enqueue_job(db, "screening.run", {"application_id": application_id})
    with db.cursor() as cur:
        cur.execute("select id, job_type, payload from jobs where id = %s", (job_id,))
        row = cur.fetchone()

    screening_handler.run_screening(
        db, Job(id=row[0], job_type=row[1], payload=row[2], attempts=1, max_attempts=5)
    )

    with db.cursor() as cur:
        cur.execute(
            """
            select r.matched_name, s.source, s.content_hash, s.record_count
              from screening_results r
              join sanctions_snapshots s on s.id = r.snapshot_id
             where r.application_id = %s
            """,
            (application_id,),
        )
        rows = cur.fetchall()

    assert rows, "the fuzzy matcher should have found Ahmad Hasan for Ahmed Hassan"
    assert {r[1] for r in rows} == {"synthetic"}
    assert all(len(r[2]) == 64 for r in rows), "every match carries a full sha256"
    assert all(r[3] == 25 for r in rows), "and the size of the list it was found in"


def test_a_screening_written_before_migration_018_reads_as_unknown(db, make_application):
    """The amber path on the case view.

    Rows written before snapshots existed have snapshot_id NULL and were
    deliberately not backfilled: filling in a plausible value would fabricate
    exactly the provenance the table exists to establish. The query behind the
    case view must survive that rather than dropping the match.
    """
    application_id = make_application("screening")
    with db.cursor() as cur:
        cur.execute(
            """
            insert into screening_results
                (application_id, source, match_type, list_name, matched_name,
                 matched_entity_id, match_score, payload, snapshot_id)
            values (%s, 'ofac', 'sanctions', 'SDN', 'LEGACY MATCH', 'OLD-1', 91.0,
                    '{}'::jsonb, null)
            """,
            (application_id,),
        )
        # A LEFT join, as the case view uses: an unknown snapshot must not make
        # the match itself disappear.
        cur.execute(
            """
            select r.matched_name, s.id
              from screening_results r
              left join sanctions_snapshots s on s.id = r.snapshot_id
             where r.application_id = %s
            """,
            (application_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "LEGACY MATCH"
    assert rows[0][1] is None, "unknown, and the view says so in those words"
