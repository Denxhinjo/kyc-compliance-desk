"""Phase 1 evidence: the Python side of the audit log.

Walks one application through its lifecycle the way the worker will in later
phases, writing an audit event for every transition, then prints the timeline.

    .venv/Scripts/python smoke_audit.py <application-id>

With no id, it picks the most recently created application.
"""

from __future__ import annotations

import sys

import psycopg
from psycopg.types.json import Jsonb

from audit import log_event
from db import connect


def latest_application(conn: psycopg.Connection) -> str | None:
    with conn.cursor() as cur:
        cur.execute("select id from applications order by created_at desc limit 1")
        row = cur.fetchone()
        return str(row[0]) if row else None


def change_status(conn: psycopg.Connection, application_id: str, to_status: str) -> None:
    """Move an application to a new status and record why, atomically.

    conn.transaction() is psycopg's context manager: everything inside commits
    together, or rolls back together if anything raises. The status change and
    the audit event describing it are therefore inseparable.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "update applications set status = %s where id = %s returning status",
                (to_status, application_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"no application {application_id}")

        log_event(
            conn,
            application_id=application_id,
            actor="system:worker",
            action="status.changed",
            details={"to": to_status, "reason": "phase 1 smoke test"},
        )


def record_vendor_event(conn: psycopg.Connection, application_id: str) -> None:
    """Store a pretend webhook, then prove a duplicate cannot land twice."""
    raw_body = (
        '{"eventId":"smoke-evt-0001","type":"applicantReviewed",'
        '"reviewResult":{"reviewAnswer":"GREEN"}}'
    )

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into vendor_events
                    (vendor_event_id, event_type, application_id,
                     signature_verified, raw_body, payload)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (vendor, vendor_event_id) do nothing
                returning id
                """,
                (
                    "smoke-evt-0001",
                    "applicantReviewed",
                    application_id,
                    True,
                    raw_body,
                    Jsonb({"eventId": "smoke-evt-0001", "type": "applicantReviewed"}),
                ),
            )
            inserted = cur.fetchone()

        if inserted is not None:
            log_event(
                conn,
                application_id=application_id,
                actor="vendor:sumsub",
                action="vendor_event.received",
                details={"vendor_event_id": "smoke-evt-0001", "type": "applicantReviewed"},
            )
            print("   stored vendor event smoke-evt-0001")
        else:
            print("   vendor event smoke-evt-0001 already stored — ignored (idempotent)")


def record_screening(conn: psycopg.Connection, application_id: str) -> None:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into screening_results
                    (application_id, match_type, list_name, matched_name,
                     matched_entity_id, match_score, payload)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    application_id,
                    "pep",
                    "EU Consolidated",
                    "Fatima Al Rashid",
                    "eu-cons-88211",
                    87.50,
                    Jsonb({"country": "AE", "role": "regional minister"}),
                ),
            )

        log_event(
            conn,
            application_id=application_id,
            actor="system:worker",
            action="screening.completed",
            details={"matches": 1, "highest_score": 87.5, "match_type": "pep"},
        )


def record_decision(conn: psycopg.Connection, application_id: str) -> None:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into decisions
                    (application_id, outcome, decided_by, reason,
                     risk_score_at_decision)
                values (%s, %s, %s, %s, %s)
                """,
                (
                    application_id,
                    "approved",
                    "staff:demo@example.com",
                    "PEP match reviewed: different date of birth and nationality. "
                    "Not the listed individual.",
                    42,
                ),
            )
            cur.execute(
                "update applications set status = 'decided', risk_score = 42 "
                "where id = %s",
                (application_id,),
            )

        log_event(
            conn,
            application_id=application_id,
            actor="staff:demo@example.com",
            action="decision.recorded",
            details={"outcome": "approved", "risk_score": 42, "override": True},
        )


def print_timeline(conn: psycopg.Connection, application_id: str) -> None:
    with conn.cursor() as cur:
        # Ordered by id, not occurred_at: events written in the same
        # transaction share a timestamp, because now() is the transaction's
        # start time rather than the statement's.
        cur.execute(
            """
            select id, to_char(occurred_at, 'HH24:MI:SS') , actor, action, details
              from audit_events
             where application_id = %s
             order by id
            """,
            (application_id,),
        )
        rows = cur.fetchall()

    print(f"\n   timeline ({len(rows)} events)")
    print(f"   {'ID':<5} {'TIME':<9} {'ACTOR':<24} ACTION")
    for row in rows:
        print(f"   {row[0]:<5} {row[1]:<9} {row[2]:<24} {row[3]}")


def main() -> int:
    with connect() as conn:
        application_id = sys.argv[1] if len(sys.argv) > 1 else latest_application(conn)
        if application_id is None:
            print("No applications exist. Run the web smoke script first.")
            return 1

        print(f"application {application_id}")

        for status in ("submitted", "checking", "screening"):
            change_status(conn, application_id, status)
            print(f"   -> {status}")

        record_vendor_event(conn, application_id)
        record_screening(conn, application_id)
        print("   screening result recorded")
        record_decision(conn, application_id)
        print("   decision recorded -> decided")

        print_timeline(conn, application_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
