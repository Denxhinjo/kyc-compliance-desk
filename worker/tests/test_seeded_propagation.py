"""Every event a seeded application causes carries `"seeded": true`.

THE GAP THIS CLOSES

Seeded applications have always been marked at creation. For the bulk seeder
that was enough: `seed.py` writes every event of a case itself, so it could
mark them all. The continuous seeder does not — it puts applicants through the
REAL pipeline, which is the entire point of it, so the WORKER writes the middle
of every seeded case: `status.changed`, `screening.completed`, the automatic
`decision.recorded`, and the sweeper's closures.

The worker has no notion of a seeder, so those events went out unmarked, and
the stats page's claim that every seeded audit event is marked quietly stopped
being true the moment the trickle started.

WHY THIS TEST IS BEHAVIOURAL AND NOT A SCAN OF HISTORY

The obvious test — "no seeded application has an unmarked event" — would be
wrong. `audit_events` is append-only, so the rows written before this fix can
never be corrected, and a history-wide assertion would fail forever on data
nobody is allowed to touch.

So this drives a seeded application through the real handlers and asserts the
events THEY write are marked. It fails the moment someone adds a write site
that forgets, which is the thing worth catching.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

import result_handler  # noqa: E402
import screening_handler  # noqa: E402
import sweeper  # noqa: E402
from screening import sources  # noqa: E402
from audit import log_event, seeded_marker  # noqa: E402
from jobs import Job  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from vendor import VerificationResult  # noqa: E402

NOW = datetime.now(timezone.utc)

#: Events with no application attached. These are about a RUN, not about a
#: case, so "was the application seeded" is not a question they can answer.
#: Listed rather than silently skipped, so adding one is a decision.
APPLICATION_LESS = {"sweep.completed", "trickle.completed", "job.executed"}


def mark_created_as_seeded(db, application_id: str) -> None:
    """Make an application look like a seeder created it.

    Writes the `application.created` event the seeder writes. The fixture's own
    creation event is left alone — append-only, and in production an
    application has exactly one of these anyway.
    """
    with db.cursor() as cur:
        cur.execute(
            "delete from applications where false"  # no-op; keeps the shape obvious
        )
    log_event(
        db,
        application_id=application_id,
        actor="system:seed",
        action="application.created",
        details={"seeded": True, "channel": "web_form", "country": "GB"},
    )


def events_for(db, application_id: str) -> list[tuple[str, dict]]:
    with db.cursor() as cur:
        cur.execute(
            "select action, details from audit_events where application_id = %s "
            "order by occurred_at, id",
            (application_id,),
        )
        return cur.fetchall()


def unmarked(db, application_id: str, written_after_id: int) -> list[str]:
    """Actions written after a watermark that lack the flag."""
    with db.cursor() as cur:
        cur.execute(
            """
            select action from audit_events
             where application_id = %s
               and id > %s
               and coalesce(details->>'seeded', '') <> 'true'
             order by id
            """,
            (application_id, written_after_id),
        )
        return [row[0] for row in cur.fetchall()]


def watermark(db) -> int:
    with db.cursor() as cur:
        cur.execute("select coalesce(max(id), 0) from audit_events")
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


def test_the_marker_reads_the_creation_event(db, make_application):
    """It is derived from immutable history, not from a mutable column.

    That matters: a column could be flipped afterwards to make a real
    application look seeded, and the synthetic reviewer's "only seeded cases"
    restriction is built on this same signal.
    """
    plain = make_application("started")
    assert seeded_marker(db, plain) == {}

    seeded = make_application("started")
    mark_created_as_seeded(db, seeded)
    assert seeded_marker(db, seeded) == {"seeded": True}


def test_the_marker_is_empty_for_no_application(db):
    """Run-level events pass None and must not acquire a flag."""
    assert seeded_marker(db, None) == {}


# ---------------------------------------------------------------------------
# The worker's own write sites
# ---------------------------------------------------------------------------


def test_the_vendor_result_path_marks_its_events(db, make_application):
    """`status.changed`, written by result_handler."""
    application_id = make_application("submitted")
    session_id = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(
            "update applications set vendor_applicant_id = %s where id = %s",
            (session_id, application_id),
        )
    mark_created_as_seeded(db, application_id)
    mark = watermark(db)

    with db.cursor() as cur:
        cur.execute(
            "select id, status, vendor_status, vendor_result_at, vendor_applicant_id "
            "from applications where id = %s",
            (application_id,),
        )
        row = cur.fetchone()
    application = {
        "id": row[0],
        "status": row[1],
        "vendor_status": row[2],
        "vendor_result_at": row[3],
        "vendor_applicant_id": row[4],
    }

    result_handler.apply_result(
        db,
        application=application,
        result=VerificationResult(
            session_id=session_id,
            status="Approved",
            vendor_data=str(application_id),
            result_at=NOW,
            raw={"status": "Approved"},
        ),
        result_at=NOW,
        source="test",
    )

    actions = [a for a, _ in events_for(db, application_id)]
    assert "status.changed" in actions, actions
    assert unmarked(db, application_id, mark) == []


def test_screening_marks_its_events(db, make_application, monkeypatch):
    """`screening.completed` and the automatic `decision.recorded`.

    Driven through the real handler, so the marking is exercised on the path
    the trickle actually takes rather than on a stub.

    Pointed at the committed synthetic list rather than OFAC: the real list is
    gitignored and a 6 MB download, and what is under test is which fields the
    handler puts on its audit rows, not who it matches.
    """
    monkeypatch.setattr(screening_handler, "SANCTIONS_SOURCE", "synthetic")
    sources._cached = None
    application_id = make_application("started")
    with db.cursor() as cur:
        for step in ("submitted", "checking", "screening"):
            cur.execute(
                "update applications set status = %s where id = %s",
                (step, application_id),
            )
    mark_created_as_seeded(db, application_id)
    mark = watermark(db)

    screening_handler.run_screening(
        db,
        Job(
            id=0,
            job_type="screening.run",
            payload={"application_id": str(application_id)},
            attempts=1,
            max_attempts=5,
        ),
    )

    actions = [a for a, _ in events_for(db, application_id)]
    assert "screening.completed" in actions, actions
    assert "decision.recorded" in actions, actions
    assert unmarked(db, application_id, mark) == []


def test_the_sweepers_closures_are_marked(db, make_application):
    """`application.expired`, written by the sweeper.

    The sweeper is the one writer that handles many applications per job, so
    its marker lookup is per application rather than per job — and it has to
    get the right answer for each.
    """
    seeded_id = make_application("started")
    plain_id = make_application("started")
    mark_created_as_seeded(db, seeded_id)

    with db.cursor() as cur:
        cur.execute(
            "alter table applications disable trigger applications_set_updated_at"
        )
        try:
            cur.execute(
                "update applications set created_at = now() - interval '40 days', "
                "updated_at = now() - interval '40 days' where id = any(%s)",
                ([seeded_id, plain_id],),
            )
        finally:
            cur.execute(
                "alter table applications enable trigger applications_set_updated_at"
            )

    mark = watermark(db)
    sweeper.expire_abandoned(db)

    seeded_actions = [a for a, _ in events_for(db, seeded_id)]
    assert sweeper.EXPIRED_ACTION in seeded_actions, seeded_actions
    assert unmarked(db, seeded_id, mark) == []

    # And the counterweight: an application nobody seeded must NOT be marked.
    # A marker that returns True for everything would pass every test above.
    with db.cursor() as cur:
        cur.execute(
            "select details from audit_events where application_id = %s and action = %s",
            (plain_id, sweeper.EXPIRED_ACTION),
        )
        row = cur.fetchone()
    assert row is not None, "the unseeded application should still have expired"
    assert "seeded" not in row[0], "an unseeded application must not be marked"


# ---------------------------------------------------------------------------
# The structural guard
# ---------------------------------------------------------------------------


def test_every_web_write_site_consults_the_marker():
    """The same guard, on the TypeScript side.

    The worker was fixed first and the web was left, so a visitor approving a
    seeded case at the desk still wrote an unmarked event — the synthetic
    reviewer three lines away in the same file wrote a marked one. That is the
    second time this invariant broke, and both times it broke at a write site
    nobody had enumerated.

    So both languages are now read. Python cannot execute the TypeScript, but
    it can insist that every audit write naming an application consults the
    marker, which is the property that was missing.

    `applications.ts` is the one exception and is listed explicitly: it writes
    `application.created`, which is where the mark ORIGINATES. There is no
    earlier event to read it from, so it takes the flag from its caller.
    """
    import pathlib as _p
    import re as _re

    web = _p.Path(__file__).resolve().parent.parent.parent / "web" / "src"
    assert web.is_dir(), f"web source not found at {web}"

    ORIGIN = {"applications.ts"}  # writes the creation event itself

    checked = 0
    for path in sorted(web.rglob("*.ts")):
        source = path.read_text(encoding="utf-8")
        if "logEvent(" not in source:
            continue
        # Each logEvent call and its argument object.
        for call in _re.findall(r"logEvent\(\s*\w+,\s*\{(.*?)\n\s*\}\)", source, _re.S):
            if "applicationId: null" in call:
                continue  # run-level event, e.g. trickle.completed
            if "applicationId" not in call:
                continue
            checked += 1
            if path.name in ORIGIN:
                assert "seeded" in call, (
                    f"{path.name} writes the creation event and must still set "
                    "the mark explicitly"
                )
                continue
            assert "seededMarker(" in call or "marker" in call, (
                f"{path.relative_to(web)} writes an audit event about an "
                f"application without consulting seededMarker(): {call[:200]}"
            )

    assert checked >= 3, (
        f"only found {checked} web audit writes naming an application — the "
        "pattern probably stopped matching rather than the sites disappearing"
    )


def test_the_two_languages_agree_on_what_seeded_means():
    """One definition, written down in both, keyed on the creation event.

    The mark describes the APPLICATION, not who acted. Getting that backwards
    is what produced both failures, so the rule is stated in each language's
    helper and this checks neither drifts into keying on the actor.
    """
    import pathlib as _p

    root = _p.Path(__file__).resolve().parent.parent.parent
    python_helper = (root / "worker" / "audit.py").read_text(encoding="utf-8")
    ts_helper = (root / "web" / "src" / "lib" / "seeded.ts").read_text(encoding="utf-8")

    for name, source in (("worker/audit.py", python_helper), ("lib/seeded.ts", ts_helper)):
        assert "application.created" in source, f"{name} must read the creation event"
        assert "'seeded'" in source or '"seeded"' in source, name
        # Neither may decide by looking at who is acting.
        assert "actor =" not in source.replace("actor = \"", "") or "seeded" in source, name


def test_every_worker_write_site_consults_the_marker():
    """Read the source and fail if a write site forgets.

    The behavioural tests above cover the paths they drive. This covers the
    ones nobody has written a test for yet — a new `log_event` on an
    application, added later, that does not splat the marker.

    Deliberately crude, in the same spirit as the test that fails if the review
    desk's `for update` ever becomes `for update skip locked`. It cannot prove
    the marker is correct; it can make forgetting it impossible to miss.
    """
    import pathlib
    import re

    worker = pathlib.Path(__file__).resolve().parent.parent
    # The runtime modules that write audit events about an application.
    # seed.py and smoke_audit.py are excluded: they mark their own rows and do
    # not run in production.
    for name in ("result_handler.py", "screening_handler.py", "sweeper.py"):
        source = (worker / name).read_text(encoding="utf-8")

        # Every audit write that names an application_id other than null.
        writes = re.findall(
            r"(?:log_event\(|insert into audit_events)(.{0,600}?)(?:\n\s*\)\s*\n|\n\s*\)\s*$)",
            source,
            re.S,
        )
        for block in writes:
            names_application = (
                "application_id=" in block
                or "application_id," in block
                or "values (%s," in block
            )
            if not names_application:
                continue  # run-level event, e.g. sweep.completed
            assert "seeded_marker(" in block, (
                f"{name} writes an audit event about an application without "
                f"seeded_marker():\n{block[:300]}"
            )
