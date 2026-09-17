"""Risk scoring.

PURE FUNCTIONS. Nothing in this module touches a database, opens a socket, or
reads a clock. Give it an ApplicantProfile, get back a score and the reasons
that produced it. Every input arrives as an argument, including "today".

WHY PURE

  Testable without fixtures. Every rule and every boundary is checked directly,
  in milliseconds, with no database and nothing mocked. That is why the tests
  for this file are worth reading.

  Deterministic, therefore reproducible. The same profile yields the same score
  forever. A clock read in here would mean a case scored differently on re-run,
  which for a decision you must be able to defend is disqualifying.

  Replayable. Given the stored profile you can re-derive a score from eighteen
  months ago and show your working. That is what "explain this decision" means
  in practice, and it is not achievable if the scoring reaches out to a world
  that has since moved on.

WHY POINTS AND NOT A MODEL

  Not because a model would score worse — it might well score better. Because
  of what has to happen after the score exists.

  An officer must act on the REASON, not the number. "Score 65" tells them
  nothing; "probable sanctions match at 87, plus a high-risk jurisdiction"
  tells them what to check first.

  A regulator will ask why a particular person was refused, and "the model said
  so" is not an answer. UK and EU rules on automated decision-making give
  individuals a right to an explanation and to contest it.

  The training data does not exist. You reject the risky applicants, so you
  never learn whether they would have been fine. The feedback loop that makes
  supervised learning work is structurally absent here.

  Proxy discrimination. A model trained on past decisions learns past bias,
  and name and nationality are excellent proxies for ethnicity. A points table
  makes every such input visible and arguable.

  Rules change by law, not by retraining. When FATF adds a country you edit a
  list, and you can state exactly which decisions change and why.

  The point is not accuracy. It is that the output has to be an ARGUMENT
  rather than a prediction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: Bumped whenever the points, bands or signals change. Stored on every scored
#: application so an old score can be reproduced rather than merely believed.
RULESET_VERSION = "2026-09-1"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreeningHit:
    """One candidate match, already found. Scoring does no searching."""

    match_type: str  # 'sanctions' | 'pep' | 'adverse_media'
    list_name: str
    matched_name: str
    match_score: float  # 0-100
    entity_id: str | None = None
    #: The list publishes a date of birth and it disagrees with the applicant's.
    date_of_birth_conflict: bool = False


@dataclass(frozen=True)
class ApplicantProfile:
    """Everything scoring is allowed to know.

    Assembled by the caller from database rows. Frozen, so a rule cannot
    accidentally mutate the thing it is judging.
    """

    country: str  # ISO 3166-1 alpha-2
    #: The vendor's own verdict on the identity documents, verbatim.
    #: None means we have no result — which is itself a finding.
    vendor_status: str | None
    hits: tuple[ScreeningHit, ...] = ()


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    """One reason, with its cost.

    `reason` is written for the compliance officer, not for a log file. It is
    what appears on the case in Phase 6, so it says what was found and why it
    matters — never "rule SANCTIONS_HIGH fired".
    """

    code: str
    points: int
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Thresholds:
    """Where the lines are drawn. Passed in, never read from a global.

    Recorded alongside every score, because a stored 65 is unexplainable once
    these move: you would still know the number and the reasons, but no longer
    why it was routed as it was.
    """

    auto_approve_below: int = 20
    auto_reject_at_or_above: int = 80

    def as_dict(self) -> dict[str, int]:
        return {
            "auto_approve_below": self.auto_approve_below,
            "auto_reject_at_or_above": self.auto_reject_at_or_above,
        }


DEFAULT_THRESHOLDS = Thresholds()


class Routing:
    APPROVE = "approve"
    REVIEW = "review"
    REJECT = "reject"


@dataclass(frozen=True)
class RiskAssessment:
    score: int
    signals: tuple[Signal, ...]
    routing: str
    thresholds: Thresholds
    ruleset_version: str = RULESET_VERSION

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(signal.reason for signal in self.signals)

    def as_dict(self) -> dict[str, Any]:
        """The shape stored in applications.risk_signals.

        Includes the thresholds and the ruleset version, so the row is
        self-contained: everything needed to explain the routing is in it, and
        nothing has to be looked up in a version of the code that no longer
        exists.
        """
        return {
            "score": self.score,
            "routing": self.routing,
            "ruleset_version": self.ruleset_version,
            "thresholds": self.thresholds.as_dict(),
            "signals": [
                {
                    "code": s.code,
                    "points": s.points,
                    "reason": s.reason,
                    "evidence": dict(s.evidence),
                }
                for s in self.signals
            ],
        }


# ---------------------------------------------------------------------------
# The bands
# ---------------------------------------------------------------------------

#: Name-match strength to confidence.
#:
#: Bands rather than one threshold, because the measured data does not support
#: a single line. Across the test set, true matches and false positives BOTH
#: occur at 80-81.5 with identical scores: "Mohammed Al-Sayed" against
#: "Muhammad Al Sayyid" (the same person) scores 80.0, and so do "John Smith"
#: against "Jane Smith" and "Ahmed Hassan" against "Ahmed Hussein" (different
#: people). The information needed to separate them is not in the strings.
#:
#: A single threshold forces a binary decision on data that cannot support one.
#: Bands do not pretend to: the ambiguous range contributes points without ever
#: being decisive on its own, which routes it to a human. That is not a fudge,
#: it is the correct handling of genuine uncertainty.
CONFIRMED = 92.0
PROBABLE = 85.0
WEAK = 80.0

#: Sanctions.
#:
#: A listing is a legal prohibition rather than a risk factor — providing
#: services to a listed person is a criminal offence with strict liability — so
#: the instinct is to make a confirmed match auto-reject on its own.
#:
#: MEASUREMENT SAYS OTHERWISE, and this is the most important number in the
#: phase. Scored against the real OFAC SDN list, 0.58% of applicants who were
#: on no list at all reached the 'confirmed' band purely through having a
#: common name: "Carlos Garcia" against "Carlos Alberto GAXIOLA GARCIA",
#: "Andrei Petrov" against "Andrei Yuvenalyevich PETROV". At 80 points those
#: were auto-rejected. On a book of 100,000 that is 580 real customers refused
#: by a string comparison.
#:
#: So 'confirmed' is 60: enough to guarantee review, not enough to refuse
#: anyone by itself. Reaching 80 needs a second, independent signal — a failed
#: document check, a call-for-action jurisdiction.
#:
#: This also matches actual practice. Firms do not refuse customers on a fuzzy
#: name match; a potential match is escalated, and a human confirms identity
#: before the firm acts. Automated rejection on name similarity alone is not
#: the cautious option, it is an untested one.
#:
#: Recall is unaffected: the match is still found, still recorded, still shown
#: to an officer. Only the automatic refusal is withdrawn.
SANCTIONS_POINTS = {"confirmed": 60, "probable": 45, "weak": 20}

#: PEPs. Deliberately incapable of causing an automatic rejection.
#:
#: Being a Politically Exposed Person is not illegal and is not grounds for
#: refusal. The law requires ENHANCED DUE DILIGENCE — senior sign-off,
#: source-of-wealth checks, ongoing monitoring — which means a human. Wholesale
#: refusal of PEPs ("de-risking") is something regulators criticise, so the
#: maximum here is set below the rejection threshold on purpose.
PEP_POINTS = {"confirmed": 30, "probable": 15, "weak": 5}

ADVERSE_MEDIA_POINTS = {"confirmed": 10, "probable": 10, "weak": 5}

#: FATF listings, as at 2026-09. MUST be re-checked against fatf-gafi.org
#: before any real use — this is a legal list with a publication date, not a
#: constant. It is here in the code rather than in a database so that a change
#: to it is a reviewable commit.
FATF_CALL_FOR_ACTION = frozenset({"IR", "KP", "MM"})
FATF_INCREASED_MONITORING = frozenset(
    {"BF", "CM", "HR", "CD", "HT", "ML", "MZ", "MC", "NA", "NG",
     "PH", "SN", "ZA", "SS", "SY", "TZ", "TR", "UG", "AE", "VN", "YE"}
)

COUNTRY_CALL_FOR_ACTION_POINTS = 40
COUNTRY_MONITORING_POINTS = 15

#: The vendor's verdict on the identity documents.
#:
#: "Approved" scores nothing: passing a document check is the baseline, not a
#: credit. Failing to complete one at all scores heavily, because an
#: unverifiable identity is the single most common shape of fraud.
DOCUMENT_POINTS = {
    "Approved": 0,
    "In Review": 10,
    "In Progress": 20,
    "Awaiting User": 20,
    "Resubmitted": 20,
    "Abandoned": 30,
    "Expired": 30,
    "Kyc Expired": 30,
    "Declined": 40,
}
DOCUMENT_MISSING_POINTS = 30


def band_of(score: float) -> str | None:
    if score >= CONFIRMED:
        return "confirmed"
    if score >= PROBABLE:
        return "probable"
    if score >= WEAK:
        return "weak"
    return None


def _downgrade(band: str) -> str:
    """One band weaker. How a conflicting date of birth is expressed.

    Dropping a band rather than scaling the points, because the result has to
    be sayable in a sentence: "the name matched at 93, but the listed date of
    birth is 1944 and this applicant was born in 1983, so it is treated as
    probable rather than confirmed." A multiplier produces a number nobody can
    defend in those terms.
    """
    return {"confirmed": "probable", "probable": "weak", "weak": "weak"}[band]


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def _strongest(hits: tuple[ScreeningHit, ...], match_type: str) -> ScreeningHit | None:
    """The best hit of one type, or None.

    The STRONGEST, never the sum.

    Adding up several weak matches would be backwards. Five weak hits against
    "Ahmed Hassan" are evidence that the name is common, not that the person is
    five times more likely to be sanctioned — and summing them would push an
    ordinary applicant with an ordinary name past the rejection threshold purely
    for having an ordinary name. The count is still recorded, as context for the
    officer, worth nothing.
    """
    candidates = [h for h in hits if h.match_type == match_type]
    return max(candidates, key=lambda h: h.match_score) if candidates else None


def _match_signal(
    hit: ScreeningHit, points_table: Mapping[str, int], code_prefix: str, label: str
) -> Signal | None:
    band = band_of(hit.match_score)
    if band is None:
        return None

    effective = _downgrade(band) if hit.date_of_birth_conflict else band
    points = points_table[effective]

    reason = (
        f"{label} match against \"{hit.matched_name}\" on {hit.list_name} "
        f"at {hit.match_score:.0f}% name similarity ({effective})"
    )
    if hit.date_of_birth_conflict:
        reason += "; downgraded because the listed date of birth conflicts"

    return Signal(
        code=f"{code_prefix}_{effective}",
        points=points,
        reason=reason,
        evidence={
            "list_name": hit.list_name,
            "matched_name": hit.matched_name,
            "match_score": round(hit.match_score, 1),
            "entity_id": hit.entity_id,
            "band": effective,
            "date_of_birth_conflict": hit.date_of_birth_conflict,
        },
    )


def score_sanctions(profile: ApplicantProfile) -> list[Signal]:
    hit = _strongest(profile.hits, "sanctions")
    if hit is None:
        return []
    signal = _match_signal(hit, SANCTIONS_POINTS, "sanctions", "Sanctions")
    return [signal] if signal else []


def score_pep(profile: ApplicantProfile) -> list[Signal]:
    hit = _strongest(profile.hits, "pep")
    if hit is None:
        return []
    signal = _match_signal(hit, PEP_POINTS, "pep", "PEP")
    return [signal] if signal else []


def score_adverse_media(profile: ApplicantProfile) -> list[Signal]:
    hit = _strongest(profile.hits, "adverse_media")
    if hit is None:
        return []
    signal = _match_signal(hit, ADVERSE_MEDIA_POINTS, "adverse_media", "Adverse media")
    return [signal] if signal else []


def score_country(profile: ApplicantProfile) -> list[Signal]:
    country = (profile.country or "").upper()
    if country in FATF_CALL_FOR_ACTION:
        return [
            Signal(
                code="country_call_for_action",
                points=COUNTRY_CALL_FOR_ACTION_POINTS,
                reason=(
                    f"Address country {country} is subject to a FATF call for "
                    f"action — the highest-risk category"
                ),
                evidence={"country": country, "fatf_list": "call_for_action"},
            )
        ]
    if country in FATF_INCREASED_MONITORING:
        return [
            Signal(
                code="country_increased_monitoring",
                points=COUNTRY_MONITORING_POINTS,
                reason=(
                    f"Address country {country} is on the FATF list of "
                    f"jurisdictions under increased monitoring"
                ),
                evidence={"country": country, "fatf_list": "increased_monitoring"},
            )
        ]
    return []


def score_document_check(profile: ApplicantProfile) -> list[Signal]:
    if profile.vendor_status is None:
        return [
            Signal(
                code="document_check_missing",
                points=DOCUMENT_MISSING_POINTS,
                reason="No identity verification result was available",
                evidence={"vendor_status": None},
            )
        ]

    points = DOCUMENT_POINTS.get(profile.vendor_status, DOCUMENT_MISSING_POINTS)
    if points == 0:
        return []

    return [
        Signal(
            code=f"document_check_{profile.vendor_status.lower().replace(' ', '_')}",
            points=points,
            reason=f"Identity verification returned \"{profile.vendor_status}\"",
            evidence={"vendor_status": profile.vendor_status},
        )
    ]


def score_match_context(profile: ApplicantProfile) -> list[Signal]:
    """Zero-point context: how many other candidates there were.

    Worth no points and worth a great deal of attention. Thirty weak hits mean
    the applicant has a common name; one hit at 97 means something quite
    different. The officer needs to know which situation they are in, and the
    score cannot tell them.
    """
    if len(profile.hits) <= 1:
        return []
    return [
        Signal(
            code="multiple_candidates",
            points=0,
            reason=(
                f"{len(profile.hits)} list entries were close enough to record; "
                f"only the strongest of each type was scored"
            ),
            evidence={
                "candidate_count": len(profile.hits),
                "scores": sorted(
                    (round(h.match_score, 1) for h in profile.hits), reverse=True
                )[:10],
            },
        )
    ]


#: Order matters only for presentation: the officer reads these top to bottom,
#: so the legally decisive ones come first.
RULES = (
    score_sanctions,
    score_pep,
    score_adverse_media,
    score_document_check,
    score_country,
    score_match_context,
)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def route(score: int, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> str:
    """Score to routing. Separate from scoring so the line can move without
    touching a single rule."""
    if score < thresholds.auto_approve_below:
        return Routing.APPROVE
    if score >= thresholds.auto_reject_at_or_above:
        return Routing.REJECT
    return Routing.REVIEW


def score_application(
    profile: ApplicantProfile, thresholds: Thresholds = DEFAULT_THRESHOLDS
) -> RiskAssessment:
    """Score one applicant and say why.

    The whole of the risk logic, in fifteen lines you can read without
    following a call chain.
    """
    signals: list[Signal] = []
    for rule in RULES:
        signals.extend(rule(profile))

    total = sum(signal.points for signal in signals)

    return RiskAssessment(
        score=total,
        signals=tuple(signals),
        routing=route(total, thresholds),
        thresholds=thresholds,
    )
