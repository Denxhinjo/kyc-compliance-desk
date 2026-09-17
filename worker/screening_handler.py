"""The screening job: search the lists, score, route, decide.

All the I/O of Phase 5 lives here so that scoring.py can stay pure. The split
is the point: this file is allowed to be dull.

Idempotent, because Phase 2 gives at-least-once delivery and this job WILL run
twice. Three separate guards, one per thing it writes:

  screening_results  a unique index on (application, source, entity), so a
                     second run finds the same entities and inserts nothing
  applications       written only when the score or signals actually differ
  decisions          a terminal decision is refused by the partial unique index
                     from migration 007; a referral is guarded by a query
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from audit import log_event
from config import SANCTIONS_SOURCE
from handlers import PermanentError, handler
from jobs import Job
from scoring import (
    ApplicantProfile,
    RiskAssessment,
    Routing,
    ScreeningHit,
    score_application,
)
from screening.sources import load_index

log = logging.getLogger("worker.screening")

JOB_TYPE = "screening.run"


@handler(JOB_TYPE)
def run_screening(conn: psycopg.Connection, job: Job) -> None:
    application_id = job.payload.get("application_id")
    if not application_id:
        raise PermanentError("job payload has no application_id")

    application = _load_application(conn, application_id)
    if application is None:
        raise PermanentError(f"no application {application_id}")

    if application["status"] != "screening":
        # Screening only happens in the screening stage. A job arriving for an
        # already-decided case is a duplicate delivery, not an error.
        log.info(
            "application %s is %s, not screening — nothing to do",
            application_id,
            application["status"],
        )
        return

    index = load_index(SANCTIONS_SOURCE)

    matches = index.search(
        application["full_name"],
        date_of_birth=str(application["date_of_birth"]),
    )

    _store_matches(conn, application_id, index.source, matches)

    profile = ApplicantProfile(
        country=application["address_country"],
        vendor_status=application["vendor_status"],
        hits=tuple(
            ScreeningHit(
                match_type=m.entry.entity_type,
                list_name=m.entry.list_name,
                matched_name=m.matched_name,
                match_score=m.score,
                entity_id=m.entry.entity_id,
                date_of_birth_conflict=m.date_of_birth_conflict,
            )
            for m in matches
        ),
    )

    # The only line in this file that decides anything. Everything above
    # gathers facts; everything below records consequences.
    assessment = score_application(profile)

    _store_assessment(conn, application, assessment, len(matches), index.source)
    _route(conn, application, assessment)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _load_application(
    conn: psycopg.Connection, application_id: str
) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select id, status, full_name, date_of_birth, address_country,
                   vendor_status, risk_score, risk_signals
              from applications
             where id = %s
            """,
            (application_id,),
        )
        return cur.fetchone()


def _store_matches(
    conn: psycopg.Connection, application_id: str, source: str, matches: list
) -> None:
    """Write every candidate, including the weak ones.

    Nothing found above the noise floor is discarded. An officer who cannot see
    the near-misses cannot tell a lone convincing match from one of thirty, and
    an auditor asking "what did you consider?" needs an answer that is not
    "whatever the threshold let through".
    """
    if not matches:
        return

    with conn.cursor() as cur:
        for match in matches:
            cur.execute(
                """
                insert into screening_results
                    (application_id, source, match_type, list_name, matched_name,
                     matched_entity_id, match_score, payload)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (application_id, source, matched_entity_id)
                    where matched_entity_id is not null
                do nothing
                """,
                (
                    application_id,
                    source,
                    match.entry.entity_type,
                    match.entry.list_name,
                    match.matched_name,
                    match.entry.entity_id,
                    round(match.score, 2),
                    Jsonb(
                        {
                            "primary_name": match.entry.name,
                            "aliases": list(match.entry.aliases),
                            "countries": list(match.entry.countries),
                            "listed_date_of_birth": match.entry.date_of_birth,
                            "date_of_birth_conflict": match.date_of_birth_conflict,
                        }
                    ),
                ),
            )


def _store_assessment(
    conn: psycopg.Connection,
    application: dict[str, Any],
    assessment: RiskAssessment,
    candidate_count: int,
    source: str,
) -> None:
    """Record the score, the signals, the thresholds and the ruleset version.

    Written only when something actually changed, so a re-run does not bump
    updated_at and does not append an audit row.
    """
    payload = assessment.as_dict()
    payload["source"] = source
    payload["candidates_considered"] = candidate_count

    unchanged = (
        application["risk_score"] == assessment.score
        and application["risk_signals"] is not None
        and application["risk_signals"].get("signals") == payload["signals"]
    )
    if unchanged:
        log.info(
            "application %s: score unchanged at %d — nothing rewritten",
            application["id"],
            assessment.score,
        )
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            update applications
               set risk_score = %s,
                   risk_signals = %s,
                   risk_scored_at = now(),
                   risk_ruleset_version = %s
             where id = %s
            """,
            (
                assessment.score,
                Jsonb(payload),
                assessment.ruleset_version,
                application["id"],
            ),
        )

    log_event(
        conn,
        application_id=application["id"],
        actor="system:worker",
        action="screening.completed",
        details={
            "score": assessment.score,
            "routing": assessment.routing,
            "ruleset_version": assessment.ruleset_version,
            "source": source,
            "candidates": candidate_count,
            # The reasons, not just the number. Phase 6 reads these.
            "reasons": list(assessment.reasons),
        },
    )
    log.info(
        "application %s scored %d -> %s (%d candidate(s))",
        application["id"],
        assessment.score,
        assessment.routing,
        candidate_count,
    )


def _route(
    conn: psycopg.Connection, application: dict[str, Any], assessment: RiskAssessment
) -> None:
    """Turn a routing into a decision.

    approve / reject are terminal: a decision row plus screening -> decided.
    review writes a 'referred' decision and leaves the application in
    'screening', because a referral is a decision about PROCESS, not about the
    applicant — the case is not decided until a human decides it in Phase 6.
    """
    application_id = application["id"]
    reason = _decision_reason(assessment)

    if assessment.routing == Routing.REVIEW:
        if _has_decision(conn, application_id, "referred"):
            log.info("application %s already referred — no second referral", application_id)
            return

        _insert_decision(conn, application_id, "referred", "system", reason, assessment)
        log_event(
            conn,
            application_id=application_id,
            actor="system:worker",
            action="decision.recorded",
            details={
                "outcome": "referred",
                "decided_by": "system",
                "score": assessment.score,
                "reasons": list(assessment.reasons),
            },
        )
        log.info("application %s referred to a human", application_id)
        return

    outcome = "approved" if assessment.routing == Routing.APPROVE else "rejected"

    if _has_terminal_decision(conn, application_id):
        log.info("application %s already has a verdict — leaving it alone", application_id)
        return

    _insert_decision(conn, application_id, outcome, "system", reason, assessment)

    with conn.cursor() as cur:
        # screening -> decided, the only transition the state machine permits
        # into a terminal state. Guarded in the WHERE clause so a concurrent
        # change cannot be trampled.
        cur.execute(
            "update applications set status = 'decided' where id = %s and status = 'screening'",
            (application_id,),
        )

    log_event(
        conn,
        application_id=application_id,
        actor="system:worker",
        action="decision.recorded",
        details={
            "outcome": outcome,
            "decided_by": "system",
            "score": assessment.score,
            "automatic": True,
            "reasons": list(assessment.reasons),
        },
    )
    log.info("application %s auto-%s", application_id, outcome)


def _decision_reason(assessment: RiskAssessment) -> str:
    """The written reason stored on the decision.

    decisions.reason is NOT NULL and constrained non-blank, so an automatic
    decision has to justify itself in words just as a human one does. A row
    saying only "score 84" would satisfy the constraint and defeat its purpose.
    """
    if not assessment.signals:
        return (
            f"Automatic approval: risk score {assessment.score}, "
            f"no risk signals found."
        )
    reasons = "; ".join(assessment.reasons)
    return f"Risk score {assessment.score}. {reasons}."


def _has_terminal_decision(conn: psycopg.Connection, application_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            select 1 from decisions
             where application_id = %s and outcome in ('approved', 'rejected')
             limit 1
            """,
            (application_id,),
        )
        return cur.fetchone() is not None


def _has_decision(conn: psycopg.Connection, application_id: str, outcome: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from decisions where application_id = %s and outcome = %s limit 1",
            (application_id, outcome),
        )
        return cur.fetchone() is not None


def _insert_decision(
    conn: psycopg.Connection,
    application_id: str,
    outcome: str,
    decided_by: str,
    reason: str,
    assessment: RiskAssessment,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into decisions
                (application_id, outcome, decided_by, reason, risk_score_at_decision)
            values (%s, %s, %s, %s, %s)
            """,
            (application_id, outcome, decided_by, reason, assessment.score),
        )
