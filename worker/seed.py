"""Generate a synthetic applicant history that does not look generated.

    .venv/Scripts/python seed.py --count 500 --weeks 6

WHAT GIVES SYNTHETIC DATA AWAY

Worth naming the tells, because every choice below is aimed at one of them:

  1. FLAT TIMESTAMPS. random.uniform(start, end) produces a histogram with no
     shape at all. Real arrivals have a daily rhythm and a weekly one, and a
     histogram is the first thing anyone plots.

  2. UNIFORM EVERYTHING. Ages evenly spread across 18-80; every name used
     exactly 16 times out of 500. Real name frequency is closer to Zipf: a few
     very common, a long tail of singletons.

  3. NO CORRELATION BETWEEN FIELDS. Random name crossed with random country
     gives "Wolfgang Schmidt, Vietnam".

  4. EVERY JOURNEY COMPLETE. Real funnels are full of people who started and
     wandered off. A dataset where 100% of applicants finish is fiction, and it
     quietly flatters every percentage computed from it.

  5. TIDY OUTCOME RATIOS. Exactly 33/33/33 is the fingerprint of someone
     generating to a specification rather than observing a process.

  6. MACHINE-EVEN DURATIONS. Everything taking two to four hours, with no tail.

  7. INSERTION ORDER NOT MATCHING TIME ORDER. If rows are generated in random
     order, the sequential id will not be monotonic with created_at — which is
     exactly what a backfill looks like. So this inserts CHRONOLOGICALLY, and
     ids and timestamps agree the way they would if the rows had really arrived.

THE OUTCOMES ARE NOT ASSIGNED, THEY ARE COMPUTED

This script does not decide who gets approved. It generates applicants and
vendor results, then runs the REAL Phase 5 code — the actual SanctionsIndex,
the actual pure score_application() — and routes on the real thresholds. Only
the human decisions on referred cases are simulated.

So the numbers on the stats page measure the actual pipeline rather than
assumptions about it. The outcome mix will be lopsided, because that is what
the scoring produces; an even split would itself be the tell.

A NOTE ON HONESTY

This writes BACKDATED audit_events. That table is the one thing in the system
meant to be the unvarnished truth, and here it is being fabricated wholesale.
So every seeded audit row carries "seeded": true in its details. Anyone can
filter them out and nobody reading a timeline can mistake invented history for
real history.

It also cannot be undone. audit_events rejects DELETE, and that rule binds the
person who wrote it as much as anyone else — so the seeder refuses to run twice
unless forced, and a genuine reset means dropping the schema and re-migrating.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from config import SANCTIONS_SOURCE
from db import connect
from scoring import (
    ApplicantProfile,
    Routing,
    ScreeningHit,
    score_application,
)
from screening.sources import ensure_snapshot, load_index

# ---------------------------------------------------------------------------
# The arrival model
# ---------------------------------------------------------------------------

#: Relative volume by hour, local time. Almost nothing at 04:00, a long evening
#: peak. Deliberately not a neat curve — real ones have a lunchtime bump and an
#: after-work spike, and a smooth sine wave reads as generated.
HOUR_WEIGHTS = [
    0.04, 0.02, 0.01, 0.01, 0.01, 0.02,  # 00-05
    0.06, 0.16, 0.34, 0.52, 0.60, 0.62,  # 06-11
    0.70, 0.66, 0.58, 0.56, 0.62, 0.78,  # 12-17
    0.92, 1.00, 0.96, 0.80, 0.54, 0.24,  # 18-23
]

#: Monday=0. A consumer product gets a weekend lift, not a weekend collapse —
#: people open accounts when they are not at work.
DAY_WEIGHTS = [0.92, 0.95, 0.97, 0.98, 0.90, 1.00, 0.96]

#: When a compliance officer is at their desk. This single detail does more for
#: realism than anything else here: it is what makes the queue build overnight
#: and across the weekend and drain on the next working morning, and it is what
#: makes median time-to-decision a real number rather than a constant.
OFFICE_START_HOUR = 9
OFFICE_END_HOUR = 17.5


def arrival_weight(when: datetime) -> float:
    return HOUR_WEIGHTS[when.hour] * DAY_WEIGHTS[when.weekday()]


def sample_arrivals(count: int, weeks: int, rng: random.Random) -> list[datetime]:
    """Arrival times shaped by hour and weekday, by rejection sampling.

    Propose a uniform instant, keep it with probability proportional to its
    weight. A few lines, and it produces a histogram with genuine structure
    rather than a flat block.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(weeks=weeks)
    span = (now - start).total_seconds()

    times: list[datetime] = []
    while len(times) < count:
        candidate = start + timedelta(seconds=rng.random() * span)
        if rng.random() < arrival_weight(candidate):
            times.append(candidate)

    # Chronological, so ids and timestamps agree — see tell #7.
    times.sort()
    return times


def next_office_moment(after: datetime, rng: random.Random) -> datetime:
    """The next plausible moment an officer would pick a case up.

    Not the next working second: officers work a queue in batches, so this
    lands somewhere inside the working day rather than at 09:00:00 sharp.
    """
    moment = after
    for _ in range(400):  # a fortnight of half-days; far more than needed
        weekday = moment.weekday()
        hour = moment.hour + moment.minute / 60

        if weekday >= 5:  # weekend
            days = 7 - weekday
            moment = (moment + timedelta(days=days)).replace(
                hour=OFFICE_START_HOUR, minute=0, second=0
            )
            continue
        if hour < OFFICE_START_HOUR:
            moment = moment.replace(hour=OFFICE_START_HOUR, minute=0, second=0)
        elif hour >= OFFICE_END_HOUR:
            moment = (moment + timedelta(days=1)).replace(
                hour=OFFICE_START_HOUR, minute=0, second=0
            )
            continue

        # Inside working hours. Officers do not clear the queue instantly.
        delay = rng.lognormvariate(math.log(45 * 60), 1.0)  # median ~45 min
        picked = moment + timedelta(seconds=min(delay, 6 * 3600))
        if picked.hour + picked.minute / 60 < OFFICE_END_HOUR:
            return picked
        moment = (moment + timedelta(days=1)).replace(
            hour=OFFICE_START_HOUR, minute=0, second=0
        )

    return moment


def lognormal_seconds(median_seconds: float, spread: float, rng: random.Random) -> float:
    """A duration with a long right tail.

    Real processing times are not uniform between two bounds; most are quick
    and a few are dramatically not. A lognormal gives that shape for free.
    """
    return rng.lognormvariate(math.log(median_seconds), spread)


# ---------------------------------------------------------------------------
# The people
# ---------------------------------------------------------------------------

GIVEN_NAMES = [
    "James", "Olivia", "Mohammed", "Sofia", "Daniel", "Aisha", "Lukas", "Emma",
    "Mateusz", "Priya", "Chen", "Sarah", "Andrei", "Fatima", "Liam", "Yusuf",
    "Anna", "Carlos", "Mei", "Ibrahim", "Elena", "Noah", "Zainab", "Viktor",
    "Laura", "Hassan", "Ingrid", "Diego", "Nadia", "Tomas", "Marek", "Layla",
    "Kwame", "Amara", "Dmitri", "Sofie", "Omar", "Chinedu", "Ahmed", "Grace",
]
FAMILY_NAMES = [
    "Smith", "Kowalski", "Nowak", "Silva", "Muller", "Rossi", "Dubois", "Khan",
    "Patel", "Wang", "Garcia", "Hansen", "Okafor", "Ivanov", "Haddad", "Novak",
    "Andersson", "Mensah", "Traore", "Sokolov", "Petrov", "Diallo", "Nguyen",
    "Kim", "Osei", "Bakker", "Horvath", "Costa", "Fernandez", "Larsson",
    "O'Brien", "Schmidt", "Jensen", "Moreau", "Vasquez", "Al-Rashid", "Hasan",
]

#: Weighted like a UK-centred consumer book with a broad diaspora mix, and a
#: thin tail of higher-risk jurisdictions. Not uniform: a uniform country
#: distribution is tell #2 in its purest form.
COUNTRIES = (
    ["GB"] * 46 + ["IE"] * 5 + ["DE"] * 8 + ["FR"] * 7 + ["ES"] * 6 +
    ["PL"] * 6 + ["IT"] * 4 + ["NL"] * 4 + ["PT"] * 3 + ["SE"] * 3 +
    ["RO"] * 3 + ["US"] * 3 + ["IN"] * 2 + ["BR"] * 2 + ["ZA"] * 1 +
    # FATF increased monitoring
    ["NG"] * 2 + ["TR"] * 1 + ["AE"] * 1 + ["VN"] * 1 +
    # FATF call for action, genuinely rare here
    ["IR"] * 1
)

CITIES = {
    "GB": [("London", "EC2N 4AJ"), ("Manchester", "M1 4BT"), ("Leeds", "LS1 5RU"),
           ("Bristol", "BS1 4DJ"), ("Glasgow", "G1 3SL")],
    "IE": [("Dublin", "D02 XY45")], "DE": [("Berlin", "10115"), ("Munich", "80331")],
    "FR": [("Paris", "75002"), ("Lyon", "69001")], "ES": [("Madrid", "28013")],
    "PL": [("Warsaw", "00-001"), ("Krakow", "30-001")], "IT": [("Milan", "20121")],
    "NL": [("Amsterdam", "1012 AB")], "PT": [("Lisbon", "1100-148")],
    "SE": [("Stockholm", "111 20")], "RO": [("Bucharest", "010011")],
    "US": [("New York", "10001")], "IN": [("Mumbai", "400001")],
    "BR": [("Sao Paulo", "01310")], "ZA": [("Cape Town", "8001")],
    "NG": [("Lagos", "101001")], "TR": [("Istanbul", "34000")],
    "AE": [("Dubai", "00000")], "VN": [("Hanoi", "100000")], "IR": [("Tehran", "11369")],
}

STREETS = ["High Street", "Station Road", "Church Lane", "Victoria Road",
           "Mill Lane", "Bishopsgate", "Queen Street", "Park Avenue"]


def zipf_choice(items: list[str], rng: random.Random) -> str:
    """Pick with a Zipf-like bias, so a few values dominate and many are rare.

    Uniform sampling over a name pool is tell #2: with 500 applicants and 40
    names, every name appears almost exactly 12 times, which no real population
    has ever done.
    """
    index = min(int(rng.paretovariate(1.2)) - 1, len(items) - 1)
    return items[index]


def sample_date_of_birth(rng: random.Random, today: date) -> date:
    """Skewed young, with a tail into later life. Not uniform 18-80."""
    age = int(min(max(rng.lognormvariate(math.log(33), 0.36), 18), 88))
    day_offset = rng.randrange(365)
    return date(today.year - age, 1, 1) + timedelta(days=day_offset)


@dataclass
class Applicant:
    full_name: str
    date_of_birth: date
    line1: str
    city: str
    postcode: str
    country: str


def make_applicant(rng: random.Random, today: date, listed_names: list[str]) -> Applicant:
    roll = rng.random()
    if roll < 0.03 and listed_names:
        # A few applicants share a name with someone on the list. This is the
        # realistic case and the reason the review desk exists: common names
        # collide, and a human has to tell the two people apart.
        full_name = rng.choice(listed_names)
    else:
        full_name = f"{zipf_choice(GIVEN_NAMES, rng)} {zipf_choice(FAMILY_NAMES, rng)}"

    country = rng.choice(COUNTRIES)
    city, postcode = rng.choice(CITIES.get(country, [("Springfield", "00000")]))
    return Applicant(
        full_name=full_name,
        date_of_birth=sample_date_of_birth(rng, today),
        line1=f"{rng.randrange(1, 240)} {rng.choice(STREETS)}",
        city=city,
        postcode=postcode,
        country=country,
    )


# ---------------------------------------------------------------------------
# The journey
# ---------------------------------------------------------------------------

#: Where applicants stop. Tell #4: a dataset in which everyone finishes is
#: fiction, and it quietly flatters every percentage computed from it.
#:
#: These are the honest shape of an onboarding funnel — a real one loses people
#: at every step, and those losses are the difference between "applications
#: created" and "applications decided".
DROP_NEVER_STARTED = 0.06   # created an application, never opened verification
DROP_ABANDONED = 0.05       # opened the vendor flow, never finished
DROP_STILL_CHECKING = 0.03  # vendor has it, no verdict yet

#: Referrals that are still sitting in the queue, as a function of age.
#:
#: Two wrong models were tried before this one, and both are worth recording.
#:
#: The first assumed officers always clear the queue within the working day.
#: That left one case pending out of 500 — optimistic rather than realistic,
#: since real desks carry a backlog.
#:
#: The second gave every referral a flat 18% chance of still being open,
#: independent of age. That produced a queue whose oldest case had been waiting
#: 32 days, which is not a backlog, it is a compliance failure. The model was
#: wrong: it implies a case from six weeks ago is exactly as likely to be
#: outstanding as one from yesterday, and real queues do not behave that way.
#: Old items get chased, escalated and cleared.
#:
#: So the probability decays with age, plus a small floor for the genuinely
#: stuck ones — the case waiting on a document the applicant will never send.
#: Most of the queue is recent; a couple of stragglers are old, which is both
#: true to life and exactly what the red "waiting" styling on the desk is for.
BACKLOG_RECENT = 0.55      # a referral from today is probably still open
BACKLOG_HALFLIFE_DAYS = 3.0
BACKLOG_FLOOR = 0.015      # the genuinely stuck ones


def still_pending_probability(age_days: float) -> float:
    return BACKLOG_FLOOR + BACKLOG_RECENT * math.exp(-age_days / BACKLOG_HALFLIFE_DAYS)

#: What the vendor says, for applicants who finish.
VENDOR_OUTCOMES = (
    ["Approved"] * 88 + ["Declined"] * 5 + ["In Review"] * 3 +
    ["Abandoned"] * 2 + ["Expired"] * 2
)


def seeded(details: dict) -> Jsonb:
    """Mark every fabricated audit row as fabricated."""
    return Jsonb({**details, "seeded": True})


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic applicants.")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--weeks", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260918)
    parser.add_argument(
        "--force",
        action="store_true",
        help="seed again even though seeded data is already present",
    )
    args = parser.parse_args()

    rng = random.Random(args.seed)
    today = date.today()

    index = load_index(SANCTIONS_SOURCE)
    listed_names = [e.name for e in index.entries if " " in e.name]
    print(f"screening against the {SANCTIONS_SOURCE} list ({len(index)} entries)")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from audit_events where details->>'seeded' = 'true'")
            already = cur.fetchone()[0]
        if already and not args.force:
            print(
                f"\n{already} seeded audit events are already present.\n"
                "Seeding again would double the history, and audit_events cannot\n"
                "be deleted — the append-only rule binds this script too. A real\n"
                "reset means dropping the schema and re-migrating:\n"
                '  docker compose exec -T db psql -U kyc -d kyc '
                '-c "drop schema public cascade; create schema public;"\n'
                "  python db/migrate.py up\n"
                "\nOr pass --force to add another batch on top.",
                file=sys.stderr,
            )
            return 1

        # Seeded screening points at a real snapshot too, so the demo data is
        # traceable in exactly the same way live data is.
        snapshot_id = ensure_snapshot(conn, index)

        arrivals = sample_arrivals(args.count, args.weeks, rng)
        counts = {"approved": 0, "rejected": 0, "referred_pending": 0,
                  "referred_decided": 0, "incomplete": 0}

        for created_at in arrivals:
            applicant = make_applicant(rng, today, listed_names)
            _seed_one(conn, rng, applicant, created_at, index, counts, snapshot_id)

    print(f"\nseeded {args.count} applicants over {args.weeks} weeks")
    for key, value in counts.items():
        print(f"  {key:<20} {value:5d}")
    print("\nEvery audit row is marked \"seeded\": true.")
    return 0


def _seed_one(conn, rng, applicant, created_at, index, counts, snapshot_id) -> None:
    """One applicant's whole history, written in the order it happened."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into applications
                    (status, full_name, date_of_birth, address_line1, address_city,
                     address_postcode, address_country, created_at, updated_at)
                values ('started', %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (applicant.full_name, applicant.date_of_birth, applicant.line1,
                 applicant.city, applicant.postcode, applicant.country,
                 created_at, created_at),
            )
            application_id = cur.fetchone()[0]

        _audit(conn, application_id, created_at, "system:web", "application.created",
               {"channel": "web_form", "country": applicant.country})

        # --- did they even start? ---
        if rng.random() < DROP_NEVER_STARTED:
            counts["incomplete"] += 1
            return

        session_at = created_at + timedelta(
            seconds=lognormal_seconds(90, 0.9, rng)
        )
        session_id = _uuid(rng)
        _set_status(conn, application_id, "submitted", session_at, session_id)
        _audit(conn, application_id, session_at, "system:web",
               "verification.session_created",
               {"vendor": "didit", "session_id": session_id,
                "vendor_status": "Not Started", "status_to": "submitted"})

        if rng.random() < DROP_ABANDONED:
            counts["incomplete"] += 1
            return

        # --- the vendor takes a while, sometimes a long while ---
        vendor_at = session_at + timedelta(
            seconds=min(lognormal_seconds(6 * 60, 1.5, rng), 4 * 86400)
        )
        if vendor_at > datetime.now(timezone.utc):
            counts["incomplete"] += 1
            return

        if rng.random() < DROP_STILL_CHECKING:
            _set_status(conn, application_id, "checking", vendor_at, session_id,
                        vendor_status="In Review", vendor_result_at=vendor_at)
            _audit(conn, application_id, vendor_at, "system:worker", "status.changed",
                   {"from": "submitted", "to": "checking", "source": "webhook",
                    "vendor_status": "In Review"})
            counts["incomplete"] += 1
            return

        vendor_status = rng.choice(VENDOR_OUTCOMES)
        _set_status(conn, application_id, "screening", vendor_at, session_id,
                    vendor_status=vendor_status, vendor_result_at=vendor_at)
        _audit(conn, application_id, vendor_at, "system:worker", "status.changed",
               {"from": "submitted", "to": "screening", "source": "webhook",
                "vendor_status": vendor_status})

        # --- the REAL screening and the REAL scoring ---
        matches = index.search(
            applicant.full_name, date_of_birth=str(applicant.date_of_birth)
        )
        screened_at = vendor_at + timedelta(seconds=rng.uniform(0.4, 4.0))
        _store_matches(
            conn, application_id, index.source, matches, screened_at, snapshot_id
        )

        assessment = score_application(
            ApplicantProfile(
                country=applicant.country,
                vendor_status=vendor_status,
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
        )

        stored = assessment.as_dict()
        stored["source"] = index.source
        stored["candidates_considered"] = len(matches)
        stored["sanctions_snapshot_id"] = snapshot_id
        with conn.cursor() as cur:
            cur.execute(
                """
                update applications
                   set risk_score = %s, risk_signals = %s,
                       risk_scored_at = %s, risk_ruleset_version = %s,
                       updated_at = %s
                 where id = %s
                """,
                (assessment.score, Jsonb(stored), screened_at,
                 assessment.ruleset_version, screened_at, application_id),
            )
        _audit(conn, application_id, screened_at, "system:worker",
               "screening.completed",
               {"score": assessment.score, "routing": assessment.routing,
                "source": index.source, "candidates": len(matches),
                "reasons": list(assessment.reasons)})

        reason = _automatic_reason(assessment)

        if assessment.routing == Routing.REVIEW:
            _decision(conn, application_id, "referred", "system", reason,
                      assessment.score, screened_at)
            _audit(conn, application_id, screened_at, "system:worker",
                   "decision.recorded",
                   {"outcome": "referred", "decided_by": "system",
                    "score": assessment.score, "reasons": list(assessment.reasons)})

            # A human picks it up during office hours — which is what makes the
            # median time-to-decision a real number. A Friday evening referral
            # genuinely waits until Monday.
            decided_at = next_office_moment(screened_at, rng)
            age_days = (datetime.now(timezone.utc) - screened_at).total_seconds() / 86400
            if (
                decided_at > datetime.now(timezone.utc)
                or rng.random() < still_pending_probability(age_days)
            ):
                counts["referred_pending"] += 1
                return

            approve = rng.random() < 0.72
            outcome = "approved" if approve else "rejected"
            _decision(conn, application_id, outcome, "staff:officer@example.com",
                      _human_reason(approve, assessment, rng),
                      assessment.score, decided_at)
            _set_status(conn, application_id, "decided", decided_at, session_id,
                        vendor_status=vendor_status, vendor_result_at=vendor_at)
            _audit(conn, application_id, decided_at, "staff:officer@example.com",
                   "decision.recorded",
                   {"outcome": outcome, "decided_by": "staff:officer@example.com",
                    "automatic": False, "risk_score": assessment.score})
            counts["referred_decided"] += 1
            return

        outcome = "approved" if assessment.routing == Routing.APPROVE else "rejected"
        _decision(conn, application_id, outcome, "system", reason,
                  assessment.score, screened_at)
        _set_status(conn, application_id, "decided", screened_at, session_id,
                    vendor_status=vendor_status, vendor_result_at=vendor_at)
        _audit(conn, application_id, screened_at, "system:worker",
               "decision.recorded",
               {"outcome": outcome, "decided_by": "system", "automatic": True,
                "score": assessment.score, "reasons": list(assessment.reasons)})
        counts[outcome] += 1


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _uuid(rng: random.Random) -> str:
    hexes = "0123456789abcdef"
    part = lambda n: "".join(rng.choice(hexes) for _ in range(n))  # noqa: E731
    return f"{part(8)}-{part(4)}-4{part(3)}-a{part(3)}-{part(12)}"


def _set_status(conn, application_id, status, at, session_id, *,
                vendor_status=None, vendor_result_at=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update applications
               set status = %s, vendor_applicant_id = %s, updated_at = %s,
                   vendor_status = coalesce(%s, vendor_status),
                   vendor_result_at = coalesce(%s, vendor_result_at)
             where id = %s
            """,
            (status, session_id, at, vendor_status, vendor_result_at, application_id),
        )


def _audit(conn, application_id, at, actor, action, details) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into audit_events (application_id, occurred_at, actor, action, details)
            values (%s, %s, %s, %s, %s)
            """,
            (application_id, at, actor, action, seeded(details)),
        )


def _decision(conn, application_id, outcome, by, reason, score, at) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into decisions
                (application_id, outcome, decided_by, reason,
                 risk_score_at_decision, decided_at)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (application_id, outcome, by, reason, score, at),
        )


def _store_matches(conn, application_id, source, matches, at, snapshot_id) -> None:
    for match in matches:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into screening_results
                    (application_id, source, match_type, list_name, matched_name,
                     matched_entity_id, match_score, payload, screened_at,
                     snapshot_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (application_id, source, matched_entity_id)
                    where matched_entity_id is not null
                do nothing
                """,
                (application_id, source, match.entry.entity_type,
                 match.entry.list_name, match.matched_name, match.entry.entity_id,
                 round(match.score, 2),
                 Jsonb({"primary_name": match.entry.name,
                        "aliases": list(match.entry.aliases),
                        "countries": list(match.entry.countries),
                        "listed_date_of_birth": match.entry.date_of_birth,
                        "date_of_birth_conflict": match.date_of_birth_conflict}),
                 at, snapshot_id),
            )


def _automatic_reason(assessment) -> str:
    if not assessment.signals:
        return f"Automatic approval: risk score {assessment.score}, no risk signals found."
    return f"Risk score {assessment.score}. " + "; ".join(assessment.reasons) + "."


#: Written the way an officer writes: what was checked, what was concluded.
#: Varied, because thirty identical sentences is what an auditor looks for.
APPROVE_REASONS = [
    "Match is a different individual — listed date of birth and nationality both differ. Document check passed.",
    "Reviewed the list entry against the passport: different middle name and place of birth. Not the same person.",
    "Common name collision. Applicant's address history and date of birth are inconsistent with the listed entity.",
    "PEP match confirmed but the role ended over a decade ago; enhanced due diligence completed and source of funds evidenced.",
    "Name similarity only. No corroborating identifier matches. Cleared.",
]
REJECT_REASONS = [
    "Identity document could not be verified after two attempts and the applicant did not respond to follow-up.",
    "Listed date of birth and nationality both match the applicant. Treating as a positive match; escalated.",
    "Document check declined and the applicant is resident in a call-for-action jurisdiction. Refusing.",
    "Applicant abandoned verification twice and the name matches a sanctions entry. Insufficient basis to onboard.",
]


def _human_reason(approve: bool, assessment, rng: random.Random) -> str:
    pool = APPROVE_REASONS if approve else REJECT_REASONS
    return f"{rng.choice(pool)} (score {assessment.score})"


if __name__ == "__main__":
    raise SystemExit(main())
