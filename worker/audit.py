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
