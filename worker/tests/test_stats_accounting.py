"""Every application is counted on exactly one line of the stats page.

THE RULE THIS ENFORCES

    Processed + Awaiting review + Still in flight + Verification unavailable
      + Expired  =  applications created

Adding a status without adding a line to the stats page would make applications
disappear from the totals while every individual figure on the page stayed
true — the quietest kind of wrong a dashboard can be, and a close relative of
the defect class this project has recorded four times: something that looks
like a check and is not one.

Three layers, because each catches a different mistake:

  1. the five buckets name every status and name none twice  (pure structure)
  2. the buckets partition the actual table, with no gaps or overlaps  (SQL)
  3. the database's own CHECK constraint, the Python mirror and the stats page
     all agree on what the vocabulary IS

Layer 3 is the one that would have caught migration 027 being written without
the stats page being touched.
"""

from __future__ import annotations

import pathlib
import re
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from lifecycle import ALL_STATUSES  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
STATS_PAGE = REPO_ROOT / "web" / "src" / "app" / "stats" / "page.tsx"

#: The stats page's application-counting figures, as the SQL predicate each
#: one uses. Predicates rather than sets of statuses, because two of the lines
#: split a single status and a set-of-statuses model cannot express that --
#: which is how the gap below went unnoticed in this file's first draft.
#:
#: Mirrors `totals()` and `queueState()` in web/src/app/stats/page.tsx.
REFERRED_AND_PENDING = """
    a.status = 'screening'
      and exists (select 1 from decisions r
                   where r.application_id = a.id and r.outcome = 'referred')
      and not exists (select 1 from decisions t
                       where t.application_id = a.id
                         and t.outcome in ('approved', 'rejected'))
"""

BUCKETS: dict[str, str] = {
    "Processed": "a.status = 'decided'",
    "Awaiting review": REFERRED_AND_PENDING,
    # The complement, within 'screening'. THIS LINE CLOSED A REAL GAP: the
    # webhook moves an application to 'screening' and enqueues the scoring as
    # a SEPARATE job, so between those two transactions it has no decision row
    # and satisfied neither "Awaiting review" nor "Still in flight". Five such
    # applications in the test database are what surfaced it.
    "Being screened": "a.status = 'screening' and not (" + REFERRED_AND_PENDING + ")",
    "Still in flight": "a.status in ('started', 'submitted', 'checking')",
    "Verification unavailable": "a.status = 'verification_unavailable'",
    "Expired": "a.status = 'expired'",
}

#: Which statuses each line can possibly count, for the structural assertions.
BUCKET_STATUSES: dict[str, frozenset[str]] = {
    "Processed": frozenset({"decided"}),
    "Awaiting review": frozenset({"screening"}),
    "Being screened": frozenset({"screening"}),
    "Still in flight": frozenset({"started", "submitted", "checking"}),
    "Verification unavailable": frozenset({"verification_unavailable"}),
    "Expired": frozenset({"expired"}),
}


# ---------------------------------------------------------------------------
# 1. Structure
# ---------------------------------------------------------------------------


def test_the_buckets_cover_every_status():
    """No status is missing a line.

    This is the assertion that fails when someone adds a status to the
    lifecycle and forgets the stats page — which is exactly what the general
    rule for this work was written to prevent.
    """
    covered: set[str] = set()
    for statuses in BUCKET_STATUSES.values():
        covered |= statuses
    missing = set(ALL_STATUSES) - covered
    assert not missing, f"statuses with no line on the stats page: {sorted(missing)}"


def test_only_the_two_screening_lines_share_a_status():
    """Double-counting would make the total exceed the number created.

    'screening' is counted by two lines ON PURPOSE -- they are complements,
    "referred and pending" and everything else -- and the SQL partition test
    below proves they do not overlap. Any OTHER shared status is a plain
    double count.
    """
    seen: dict[str, str] = {}
    for label, statuses in BUCKET_STATUSES.items():
        for status in statuses:
            if status == "screening":
                continue
            assert status not in seen, (
                f"{status!r} is counted by both {seen[status]!r} and {label!r}"
            )
            seen[status] = label

    sharing = sorted(l for l, s in BUCKET_STATUSES.items() if "screening" in s)
    assert sharing == ["Awaiting review", "Being screened"], sharing


def test_the_buckets_invent_no_statuses():
    """A line counting a status that cannot exist is a line that reads zero forever."""
    covered: set[str] = set()
    for statuses in BUCKET_STATUSES.values():
        covered |= statuses
    assert covered <= set(ALL_STATUSES), (
        f"not real statuses: {sorted(covered - set(ALL_STATUSES))}"
    )


# ---------------------------------------------------------------------------
# 2. The actual table
# ---------------------------------------------------------------------------


def test_the_buckets_partition_every_application(db):
    """Summed over the real table, the five figures equal the number created.

    Run against whatever the test database happens to hold, deliberately: a
    partition that only holds for data the test itself created is not a
    partition.
    """
    with db.cursor() as cur:
        cur.execute("select count(*) from applications")
        total = cur.fetchone()[0]

        counted: dict[str, int] = {}
        for label, predicate in BUCKETS.items():
            cur.execute(f"select count(*) from applications a where {predicate}")
            counted[label] = cur.fetchone()[0]

        # EXACTLY ONE line per application. Summing and comparing to the total
        # would pass if one application were double-counted here and another
        # missed there, so this counts the ones matching zero or several.
        matches = " + ".join(
            f"(case when {predicate} then 1 else 0 end)"
            for predicate in BUCKETS.values()
        )
        cur.execute(f"select count(*) from applications a where ({matches}) <> 1")
        not_exactly_once = cur.fetchone()[0]

    summed = sum(counted.values())
    assert not_exactly_once == 0, (
        f"{not_exactly_once} application(s) match zero or several stats lines: "
        f"{counted}"
    )
    assert summed == total, (
        f"{total} applications exist but the stats lines account for {summed}: "
        f"{counted}"
    )


def test_every_line_counts_its_own_state_and_no_other(db):
    """The partition, proved against one application in EVERY state.

    WHY THIS EXISTS SEPARATELY from the whole-table test above.

    The whole-table test passes vacuously on an empty database: with no
    applications, every line reads zero and any partition holds, including a
    broken one. Checked by counterfactual -- deleting a whole line from BUCKETS
    and re-running it on a freshly rebuilt schema still passed, which makes it
    a check that cannot fail when it matters most, the defect class this
    project has recorded five times.

    So this builds one application in each of the six states, asserts each line
    counts exactly its own, and asserts every line is non-empty -- that last
    assertion is what makes the test impossible to satisfy by accident.
    """
    created: dict[str, str] = {}

    with db.transaction() as tx:
        with db.cursor() as cur:

            def fresh(name: str) -> str:
                cur.execute(
                    """
                    insert into applications
                        (status, full_name, date_of_birth, address_line1,
                         address_city, address_postcode, address_country)
                    values ('started', %s, '1990-01-01', '1 Test Street',
                            'Testville', 'T1 1TT', 'GB')
                    returning id
                    """,
                    (f"Partition {name} {uuid.uuid4().hex[:8]}",),
                )
                return cur.fetchone()[0]

            def walk(application_id: str, *statuses: str) -> None:
                for status in statuses:
                    cur.execute(
                        "update applications set status = %s where id = %s",
                        (status, application_id),
                    )

            def decide(application_id: str, outcome: str) -> None:
                cur.execute(
                    """
                    insert into decisions
                        (application_id, outcome, decided_by, reason,
                         risk_score_at_decision, risk_ruleset_version_at_decision)
                    values (%s, %s, 'system', 'partition test', 30, '2026-09-2')
                    """,
                    (application_id, outcome),
                )

            # Still in flight: created, never continued.
            created["Still in flight"] = fresh("in-flight")

            # Being screened: at 'screening' with no decision row yet, which is
            # the real window between the webhook and the scoring job.
            being = fresh("being-screened")
            walk(being, "submitted", "checking", "screening")
            created["Being screened"] = being

            # Awaiting review: referred, no verdict.
            awaiting = fresh("awaiting")
            walk(awaiting, "submitted", "checking", "screening")
            decide(awaiting, "referred")
            created["Awaiting review"] = awaiting

            # Processed: a terminal verdict.
            processed = fresh("processed")
            walk(processed, "submitted", "checking", "screening")
            decide(processed, "approved")
            walk(processed, "decided")
            created["Processed"] = processed

            # Verification unavailable: the vendor disowned the session.
            unavailable = fresh("unavailable")
            walk(unavailable, "submitted", "verification_unavailable")
            created["Verification unavailable"] = unavailable

            # Expired: never began verification.
            expired = fresh("expired")
            walk(expired, "expired")
            created["Expired"] = expired

            assert set(created) == set(BUCKETS), (
                f"this test builds {sorted(created)} but the page has "
                f"{sorted(BUCKETS)} lines -- a new line needs a case here"
            )

            ids = list(created.values())

            # Each line counts exactly its own application, out of the six.
            for label, predicate in BUCKETS.items():
                cur.execute(
                    f"select a.id from applications a "
                    f"where a.id = any(%s) and {predicate}",
                    (ids,),
                )
                matched = [row[0] for row in cur.fetchall()]
                assert matched == [created[label]], (
                    f"{label!r} matched {matched}, expected "
                    f"[{created[label]}] -- the lines overlap or miss"
                )

            # And nothing matches zero lines or several.
            matches = " + ".join(
                f"(case when {predicate} then 1 else 0 end)"
                for predicate in BUCKETS.values()
            )
            cur.execute(
                f"select count(*) from applications a "
                f"where a.id = any(%s) and ({matches}) <> 1",
                (ids,),
            )
            assert cur.fetchone()[0] == 0

        raise psycopg.Rollback(tx)


def test_no_application_holds_a_status_outside_the_vocabulary(db):
    """Belt and braces on the partition: nothing is hiding in an unknown state."""
    with db.cursor() as cur:
        cur.execute(
            "select distinct status from applications where not (status = any(%s))",
            (list(ALL_STATUSES),),
        )
        stray = [row[0] for row in cur.fetchall()]
    assert not stray, f"applications in unknown statuses: {stray}"


def test_the_two_screening_lines_cover_screening_exactly(db):
    """Together they are all of 'screening', and separately they never overlap.

    This replaced an assertion that every 'screening' application carries a
    referral. That was true of production and false in general: scoring runs as
    a job separate from the status change, so there is a real committed window
    in which an application sits at 'screening' with no decision row. The old
    assertion passed on production data and would have begun failing under
    load, which is the worst way to learn a dashboard has a hole.
    """
    with db.cursor() as cur:
        cur.execute("select count(*) from applications where status = 'screening'")
        screening = cur.fetchone()[0]

        cur.execute(
            f"select count(*) from applications a where {BUCKETS['Awaiting review']}"
        )
        awaiting = cur.fetchone()[0]

        cur.execute(
            f"select count(*) from applications a where {BUCKETS['Being screened']}"
        )
        being = cur.fetchone()[0]

    assert awaiting + being == screening, (
        f"{screening} at 'screening' but the two lines account for {awaiting + being}"
    )


# ---------------------------------------------------------------------------
# 3. Three places agreeing on one vocabulary
# ---------------------------------------------------------------------------


def test_the_database_constraint_and_the_python_mirror_agree(db):
    """The CHECK on applications.status lists exactly ALL_STATUSES.

    Read out of the live constraint rather than trusted from the migration
    file, so this reflects what is actually enforced.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            select pg_get_constraintdef(oid) from pg_constraint
             where conname = 'applications_status_valid'
               and conrelid = 'applications'::regclass
            """
        )
        row = cur.fetchone()
    assert row is not None, "applications_status_valid is missing"
    in_constraint = set(re.findall(r"'([a-z_]+)'::text", row[0]))
    assert in_constraint == set(ALL_STATUSES), (
        f"database allows {sorted(in_constraint)}, "
        f"lifecycle.py says {sorted(ALL_STATUSES)}"
    )


def test_the_stats_page_mentions_every_status():
    """Read the page's source and fail if a status never appears in it.

    Crude on purpose, and in the same spirit as the test that fails if the
    desk's `for update` ever becomes `for update skip locked`: it cannot verify
    the page renders the figure correctly, but it does make it impossible to
    add a status and ship a page that has never heard of it. The precise
    accounting is asserted by the partition test above.
    """
    source = STATS_PAGE.read_text(encoding="utf-8")
    missing = [status for status in ALL_STATUSES if status not in source]
    assert not missing, (
        f"{STATS_PAGE.name} never mentions {missing} — a status with no line "
        "removes applications from the totals"
    )
