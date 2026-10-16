"""The audit log helper, Python side.

The Python twin of web/src/lib/audit.ts. Two languages, one table, one rule:
the audit write happens in the same transaction as the state change it
describes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb


def log_event(
    conn: psycopg.Connection,
    *,
    application_id: UUID | str | None,
    actor: str,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Append one row to the audit log.

    Takes an open CONNECTION and deliberately does not commit. The caller owns
    the transaction, so the state change and the record of it commit together
    or not at all:

        with db.connection() as conn:
            with conn.transaction():
                cur.execute("update applications set status = %s ...", ...)
                log_event(conn, application_id=app_id, actor="system:worker",
                          action="status.changed", details={...})

    If this function committed on its own, a failure immediately afterwards
    would roll back the state change while leaving an audit row claiming it
    happened — an audit log that lies is worse than none at all.

    Arguments after `conn` are keyword-only (that is what the bare `*` does).
    `actor` and `action` are both short strings that would be easy to pass in
    the wrong order, and this makes that impossible.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into audit_events (application_id, actor, action, details)
            values (%s, %s, %s, %s)
            """,
            # Jsonb() tells psycopg to send this as jsonb. A bare dict would be
            # rejected: psycopg does not guess that a mapping means json.
            (
                str(application_id) if application_id is not None else None,
                actor,
                action,
                Jsonb(details or {}),
            ),
        )


def seeded_marker(
    conn: psycopg.Connection, application_id: UUID | str | None
) -> dict[str, Any]:
    """`{"seeded": True}` if this application was created by a seeder, else `{}`.

    WHY THIS EXISTS

    Seeded applications are marked at creation, and for the bulk seeder that
    was enough: seed.py writes every event of a case itself, so it could mark
    them all. The continuous seeder does not — it puts applicants through the
    REAL pipeline, which is the point of it, so the worker writes the middle of
    every seeded case. The worker has no notion of a seeder, so those events
    went out unmarked and the stats page's claim that every seeded event is
    marked quietly stopped being true.

    Read from `application.created` rather than from a column on the
    application, for the same reason the sweeper's escalation counts audit rows:
    the audit log cannot be rewritten, so a case cannot be made to look seeded
    or unseeded after the fact.

    CALL IT ONCE PER JOB, not once per event. It is one indexed lookup, but a
    handler that writes three events should do one query and reuse the answer —
    the flag cannot change underneath it, because the row it reads is immutable.

    Returns a dict so callers can splat it into `details` without a conditional
    at every write site:

        marker = seeded_marker(conn, application_id)
        log_event(conn, ..., details={"score": 40, **marker})
    """
    if application_id is None:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            select 1 from audit_events
             where application_id = %s
               and action = 'application.created'
               and details->>'seeded' = 'true'
             limit 1
            """,
            (str(application_id),),
        )
        return {"seeded": True} if cur.fetchone() else {}
