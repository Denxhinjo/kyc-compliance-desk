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

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

#: Bumped whenever the points, bands or signals change -- and when the
#: reference data they read changes materially, which is why this moved to
#: -2 on 2026-09-24.
#:
#: Stored on every scored application so an old score can be reproduced rather
#: than merely believed. The points and bands did NOT change at -2; the
#: increased-monitoring list was replaced wholesale, and 767 decisions already
#: on disk carry -1 with no country_list recorded at all. Leaving the version
#: alone would have meant two decisions stamped -1 having been scored against
#: two different country lists, with nothing on the older ones to tell them
#: apart. The version is the pin a compliance officer relies on to defend a
#: past decision; an ambiguous pin is not a pin.
#:
#: -1 remains resolvable. Nothing rewrites those rows: they say -1, they were
#: scored against the -1 list, and that statement stays true.
RULESET_VERSION = "2026-10-2"

#: Which similarity definition this ruleset scores names with.
#:
#: THIS IS THE SWITCH. There is no feature flag: the ruleset in force selects
#: the definition, so a decision stamped 2026-10-1 can never carry a v2 score
#: and nothing stamped 2026-10-2 can carry a v1 one. A flag would have let the
#: same version mean two different things depending on when it ran, which
#: makes "reconstruct any decision" false.
#:
#: 2026-10-1's recorded row names no definition, because `rulesets` is
#: append-only and cannot be amended. An absent key therefore MEANS v1. See
#: screening/index.py:DEFAULT_SIMILARITY.
NAME_SIMILARITY = "v2-symmetric-coverage"
#:
#: -1 moved to -2 on 2026-09-24 when the increased-monitoring list was replaced
#: wholesale. -2 moved to 2026-10-1 on 2026-10-07 when the call-for-action list
#: stopped being unsourced.
#:
#: That last bump deserves justifying, because the CODES did not change: IR, KP
#: and MM before and after, scoring identically. What changed is what can be
#: proven. 2026-09-2's recorded definition says its call-for-action list was
#: never fetched and never cited, and that statement must stay true of the
#: decisions taken under it. Editing the row to say otherwise would be
#: rewriting history to look better, which is the one thing the rulesets table
#: exists to prevent. So: a new version, identical in effect, different in what
#: it can show.


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
    #: Every secondary identifier, already compared: a tuple of dicts shaped
    #: {identifier, outcome, detail}. Scoring reads `outcome`; the interface
    #: shows `detail`. Empty under 2026-10-1, which compared none.
    comparisons: tuple[Mapping[str, str], ...] = ()

    def conflicting_identifiers(self) -> tuple[str, ...]:
        """Which identifiers CONTRADICT this match.

        Falls back to `date_of_birth_conflict` when no structured comparisons
        were supplied. There are two representations of the same fact here --
        the boolean predates the comparisons and is still what the v1 path
        produces -- and reading only the newer one would quietly drop a
        conflict that a caller DID report, scoring a contradicted match at
        full strength. Two sources for one fact is a trap whichever way it is
        read; this reads both.
        """
        found = tuple(
            c["identifier"] for c in self.comparisons if c.get("outcome") == "conflict"
        )
        if found:
            return found
        return ("date_of_birth",) if self.date_of_birth_conflict else ()


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
class ListRevision:
    """One FATF list, as published at one plenary.

    WHY A REVISION AND NOT JUST A SET OF CODES

    FATF publishes each list as a COMPLETE SET at each plenary, roughly three
    times a year. It is not amended incrementally: a country's presence and
    its absence are both statements as of one plenary date. So the set has
    exactly one valid shape — every code from the SAME plenary, and the date
    beside it that plenary's own.

    Mixing plenaries produces a list wrong in both directions at once: it
    screens against countries already cleared and misses countries added
    since. That is worse than being out of date, because out of date is at
    least a coherent statement about a known moment. This type exists so that
    the codes and the date they belong to cannot drift apart.
    """

    codes: frozenset[str]

    #: code -> the jurisdiction's name AS PRINTED in the statement.
    #:
    #: Carried so the codes have something sourced to be checked against.
    #: Python has no ISO-3166 table and pycountry is a dependency this project
    #: has not taken, so without this a test could only check that a code is
    #: two letters — which passes "UK" (not ISO; Great Britain is GB) and any
    #: other plausible typo. Every name here was read off the same statement
    #: as the code beside it, so the pair is evidence rather than recall.
    names: Mapping[str, str]

    source_url: str

    #: The plenary's own publication date, ISO-8601 — or None when this
    #: revision could not be sourced. Never a date picked because it looks
    #: plausible: a manufactured date claims an authority it does not have and
    #: survives review precisely by looking right.
    published_at: str | None

    #: When the codes were taken from source_url. Distinct from published_at:
    #: a list published in June and read in September is current, and the gap
    #: between the two is how stale it might be.
    retrieved_at: str | None

    #: Human-readable plenary, for the officer reading a case rather than a
    #: machine reading a column.
    plenary: str | None

    provenance: str

    #: The artifact this revision was read from, when one was kept.
    #:
    #: Recorded as a path, a SHA-256 of its bytes and its format, because the
    #: three answer different questions: which file, whether it is the same
    #: file, and how much the hash is worth. See source_format.
    source_file: str | None = None
    source_sha256: str | None = None

    #: WHAT THE HASH IS WORTH, which depends entirely on this.
    #:
    #: "pdf-of-saved-webpage" means someone opened the statement in a browser
    #: and printed it. The hash then proves "this is the artifact I read" and
    #: NOT "these are FATF's canonical bytes": the capture carries navigation,
    #: a retrieval timestamp in the page footer and whatever markup the site
    #: served that minute, all of which change without the statement changing.
    #: Two honest captures of the same statement hash differently.
    #:
    #: "pdf-official" would be the publisher's own file and a stronger claim.
    #: Say which we have rather than letting the field imply the stronger one.
    source_format: str | None = None

    #: Distinctions the statement draws that the scoring does NOT.
    #:
    #: Recorded rather than implemented, deliberately: changing how anyone is
    #: scored is a policy decision and this is a provenance exercise. A
    #: limitation written into the data is one an auditor can find; one left in
    #: a developer's head is not.
    tiers: Mapping[str, tuple[str, ...]] | None = None
    flattening_note: str | None = None

    #: A date the publisher itself says it will revisit this.
    next_review_expected: str | None = None
    next_review_note: str | None = None

    @property
    def digest(self) -> str:
        """A hash of the codes, so an edit cannot pass unnoticed."""
        return hashlib.sha256(
            ",".join(sorted(self.codes)).encode("utf-8")
        ).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "plenary": self.plenary,
            "provenance": self.provenance,
            "digest": self.digest,
            "source_file": self.source_file,
            "source_sha256": self.source_sha256,
            "source_format": self.source_format,
            "tiers": {k: list(v) for k, v in self.tiers.items()} if self.tiers else None,
            "flattening_note": self.flattening_note,
            "next_review_expected": self.next_review_expected,
            "next_review_note": self.next_review_note,
        }


#: Read off the per-country section headings of the 19 June 2026 statement.
_MONITORING_NAMES = {
    "AO": "Angola",
    "BA": "Bosnia and Herzegovina",
    "BG": "Bulgaria",
    "BO": "Bolivia",
    "CD": "Democratic Republic of the Congo",
    "CI": "Cote d'Ivoire",
    "CM": "Cameroon",
    "HT": "Haiti",
    "IQ": "Iraq",
    "KE": "Kenya",
    "KW": "Kuwait",
    "LA": "Lao PDR",
    "LB": "Lebanon",
    "MC": "Monaco",
    "NP": "Nepal",
    "PG": "Papua New Guinea",
    "SS": "South Sudan",
    "SY": "Syria",
    "VE": "Venezuela",
    "VG": "Virgin Islands (UK)",
    "VN": "Vietnam",
    "YE": "Yemen",
}

_CALL_FOR_ACTION_NAMES = {
    "IR": "Iran",
    "KP": "Democratic People's Republic of Korea",
    "MM": "Myanmar",
}

#: The complete set from ONE statement: "Jurisdictions under Increased
#: Monitoring", Paris, 19 June 2026. Twenty-two jurisdictions, taken from the
#: per-country sections above the "Jurisdictions No Longer subject to Increased
#: Monitoring" heading — Algeria and Namibia sit below it and are therefore
#: removals, not members.
#:
#: Replaced WHOLESALE at the next plenary. Never amended country by country:
#: see ListRevision for why a half-updated set is worse than an old one.
INCREASED_MONITORING = ListRevision(
    codes=frozenset(_MONITORING_NAMES),
    names=_MONITORING_NAMES,
    source_url=(
        "https://www.fatf-gafi.org/content/fatf-gafi/en/publications/"
        "High-risk-and-other-monitored-jurisdictions/"
        "increased-monitoring-june-2026.html"
    ),
    published_at="2026-06-19",
    retrieved_at="2026-09-24",
    plenary="June 2026",
    provenance=(
        "Complete set from the FATF statement 'Jurisdictions under Increased "
        "Monitoring', Paris, 19 June 2026, retrieved from source_url on "
        "2026-09-24. All 22 codes come from that one statement; none is "
        "carried over from an earlier one."
    ),
)

#: The complete set from ONE statement: "High-Risk Jurisdictions subject to a
#: Call for Action", Paris, 19 June 2026, from the plenary of 17-19 June 2026.
#:
#: Read from a copy of the statement committed at data/fatf/, not fetched. The
#: previous revision of this list carried a null publication date and said
#: UNSOURCED, because the statement returned HTTP 403 when it was attempted on
#: 2026-09-24. This replaces that with a citation.
#:
#: The codes are UNCHANGED by this — IR, KP and MM before and after. What
#: changed is what can be proven about them. That is still a new ruleset
#: version rather than an edit, because "decisions under 2026-09-2 were scored
#: against an unsourced list" has to stay true of 2026-09-2.
#:
#: WHAT THIS STATEMENT SAYS THAT THE SCORING DOES NOT
#:
#: It draws two tiers. Countermeasures are called for on the DPRK and Iran. For
#: Myanmar the statement says, in terms, "enhanced due diligence — and not
#: countermeasures". Scoring has one set and one number, so a Myanmar applicant
#: and a DPRK applicant attract the same 40 points.
#:
#: That flattening is recorded here and NOT fixed, because changing who scores
#: what is a policy decision and this was a provenance exercise. The tiers are
#: carried in the data so the limitation is discoverable by anyone reading a
#: decision, rather than living in a developer's memory.
CALL_FOR_ACTION = ListRevision(
    codes=frozenset(_CALL_FOR_ACTION_NAMES),
    names=_CALL_FOR_ACTION_NAMES,
    source_url=(
        "https://www.fatf-gafi.org/en/publications/"
        "High-risk-and-other-monitored-jurisdictions/"
        "call-for-action-june-2026.html"
    ),
    published_at="2026-06-19",
    retrieved_at="2026-10-07",
    plenary="17-19 June 2026",
    source_file="data/fatf/call-for-action-june-2026-06-19.pdf",
    source_sha256=(
        "786f2ffa2e3677dc5f764f2d964589540e57a31fd52d915f43c30ae94c540137"
    ),
    #: A browser capture, not FATF's own PDF. See ListRevision.source_format
    #: for why that distinction is the whole value of the field.
    source_format="pdf-of-saved-webpage",
    tiers={
        "countermeasures": ("KP", "IR"),
        "enhanced_due_diligence_only": ("MM",),
    },
    flattening_note=(
        "The statement calls for counter-measures on the DPRK and Iran, and "
        "for enhanced due diligence AND NOT counter-measures on Myanmar. "
        "Scoring flattens this: all three are one set worth "
        "COUNTRY_CALL_FOR_ACTION_POINTS, so a Myanmar applicant scores exactly "
        "what a DPRK applicant scores. Recorded, not fixed."
    ),
    next_review_expected="2026-10",
    next_review_note=(
        "The statement says of Myanmar: 'If no further progress is made by "
        "October 2026, the FATF will consider countermeasures.' This list may "
        "therefore move within weeks of being recorded, which is the case "
        "pinning a ruleset version onto each decision exists to survive."
    ),
    provenance=(
        "Complete set from the FATF statement 'High-Risk Jurisdictions subject "
        "to a Call for Action', Paris, 19 June 2026, read on 2026-10-07 from "
        "the copy committed at data/fatf/call-for-action-june-2026-06-19.pdf. "
        "All three codes come from that one statement: the DPRK and Iran under "
        "the heading calling for countermeasures, Myanmar under the heading "
        "calling for enhanced due diligence. None is carried over from an "
        "earlier revision or from the constant this replaced."
    ),
)


@dataclass(frozen=True)
class CountryListing:
    """Both FATF lists, each carrying its own revision.

    Separate revisions rather than one shared date, because the two were
    sourced at different times and a single published_at would silently
    extend the monitoring list's citation over codes nobody verified.

    WHY THIS IS NOT IN POSTGRES WHEN THE SANCTIONS LIST IS

    The sanctions list moved into Postgres for size and search: 19,393 entries
    needing a trigram index. Provenance rode along in that change but did not
    drive it. Twenty-five country codes have neither problem, so storing them
    the same way would copy the mechanism rather than the reason. A change to
    a legal list should be a reviewable commit, and a diff is a better audit
    artefact than an UPDATE nobody sees.
    """

    call_for_action_revision: ListRevision = CALL_FOR_ACTION
    increased_monitoring_revision: ListRevision = INCREASED_MONITORING

    @property
    def call_for_action(self) -> frozenset[str]:
        return self.call_for_action_revision.codes

    @property
    def increased_monitoring(self) -> frozenset[str]:
        return self.increased_monitoring_revision.codes

    def as_dict(self) -> dict[str, Any]:
        """What gets stored on a decision."""
        return {
            "call_for_action": self.call_for_action_revision.as_dict(),
            "increased_monitoring": self.increased_monitoring_revision.as_dict(),
        }


COUNTRY_LISTING = CountryListing()

#: Kept as module-level names because they read better at the point of use and
#: because the scoring tests import them directly.
FATF_CALL_FOR_ACTION = COUNTRY_LISTING.call_for_action
FATF_INCREASED_MONITORING = COUNTRY_LISTING.increased_monitoring


@dataclass(frozen=True)
class RiskAssessment:
    score: int
    signals: tuple[Signal, ...]
    routing: str
    thresholds: Thresholds
    ruleset_version: str = RULESET_VERSION
    #: Recorded on every assessment, not only the ones a country signal fired
    #: on. "These lists were consulted and this country was on neither" is
    #: itself a finding, and an auditor asking why a jurisdiction was not
    #: flagged needs the same provenance as one asking why it was.
    country_listing: CountryListing = COUNTRY_LISTING

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
            "country_list": self.country_listing.as_dict(),
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
#: Name-strength cut-offs.
#:
#: `exact` is 100 and nothing less. Under v1 that would have been unusable --
#: a subset scored 100 routinely -- which is exactly why it could not be the
#: bar before. Under v2 only an identical normalised name reaches it.
EXACT = 100.0
STRONG = 80.0
#: Everything recorded that is not exact or strong. The floor is the retrieval
#: threshold, not a second opinion about it: if a pair was worth recording, the
#: reviewer sees it with a strength on it.
PARTIAL = 0.0

#: The pre-2026-10-2 cut-offs, kept so a decision taken under an earlier
#: ruleset can still be reproduced. Not used for new scoring.
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
#: The old numbers, moved onto the new bands. Nothing is worth more or less
#: than it was; what changed is which matches land in which band, because the
#: similarity definition stopped handing 100 to a subset.
SANCTIONS_POINTS = {"exact": 60, "strong": 45, "partial": 20}

#: PEPs. Deliberately incapable of causing an automatic rejection.
#:
#: Being a Politically Exposed Person is not illegal and is not grounds for
#: refusal. The law requires ENHANCED DUE DILIGENCE — senior sign-off,
#: source-of-wealth checks, ongoing monitoring — which means a human. Wholesale
#: refusal of PEPs ("de-risking") is something regulators criticise, so the
#: maximum here is set below the rejection threshold on purpose.
PEP_POINTS = {"exact": 30, "strong": 15, "partial": 5}

ADVERSE_MEDIA_POINTS = {"exact": 10, "strong": 10, "partial": 5}


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
    """NAME MATCH STRENGTH. Never a verdict.

    The old names were "confirmed", "probable" and "weak", and the first of
    them was the most misleading word in the system: it announced, from a
    string comparison alone, the conclusion that a compliance reviewer exists
    to reach. In compliance a name match is a POTENTIAL match; whether it is
    the same person depends on secondary identifiers and is decided by a
    human. "Confirmed match" and "false positive" are reviewer outcomes now,
    and the system emits neither.

    `exact` is reserved for a name that is the SAME name: the two normalised
    token multisets are equal. Under the v2 definition only such a pair can
    reach 100, so this threshold and that property agree by construction.
    """
    if score >= EXACT:
        return "exact"
    if score >= STRONG:
        return "strong"
    # STRICTLY greater: a score of 0 means nothing aligned at all, which is no
    # match rather than a weak one. The noise floor that keeps rubbish out of
    # the queue is RECORD_THRESHOLD, applied during retrieval, where it can
    # actually prevent the work -- not a second opinion applied here after the
    # pair has already been found and stored.
    if score > PARTIAL:
        return "partial"
    return None


def _downgrade(band: str) -> str:
    """One band weaker. How 2026-10-1 and earlier expressed a date conflict.

    Kept so an older decision can be reproduced. New scoring uses
    `_conflicted_points`, which drops straight to the weakest band instead of
    stepping down one.
    """
    return {"confirmed": "probable", "probable": "weak", "weak": "weak"}[band]


def _conflicted_points(points_table: Mapping[str, int]) -> int:
    """What a hit scores when a secondary identifier CONTRADICTS it.

    The weakest band for that match type -- 20 for sanctions, which is what
    2026-10-2 specifies. Not zero: the list entry was still found, the
    reviewer still has to look at it, and a conflicting date of birth is the
    list's claim rather than a proven fact. Not a one-band step either: a
    contradicted exact match and a contradicted partial one are in the same
    position, and ranking the first above the second says something the
    evidence does not support.
    """
    return points_table["partial"]


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
    """One list match, turned into points and a sentence.

    THE SENTENCE NEVER SAYS "CONFIRMED". It reports two separate things -- how
    alike the names are, and what the secondary identifiers say -- and leaves
    the conclusion to whoever has to defend it.
    """
    strength = band_of(hit.match_score)
    if strength is None:
        return None

    conflicts = hit.conflicting_identifiers()
    if conflicts:
        points = _conflicted_points(points_table)
        code = f"{code_prefix}_contradicted"
    else:
        points = points_table[strength]
        code = f"{code_prefix}_{strength}"

    reason = (
        f"{label} name match ({strength}, {hit.match_score:.0f}%) against "
        f"\"{hit.matched_name}\" on {hit.list_name}"
    )

    # Identifiers, in the reviewer's words, conflicts first. A reviewer
    # deciding "same person or not" is reading this line, so what the list
    # does NOT say is reported as plainly as what it does.
    if hit.comparisons:
        ordered = sorted(
            hit.comparisons, key=lambda c: c.get("outcome") != "conflict"
        )
        parts = [
            f"{c['identifier'].replace('_', ' ')}: {c['outcome'].replace('_', ' ')}"
            for c in ordered
        ]
        reason += ". " + "; ".join(parts)
    if conflicts:
        reason += (
            f". Scored at the weakest band because "
            f"{' and '.join(c.replace('_', ' ') for c in conflicts)} contradicts it"
        )

    return Signal(
        code=code,
        points=points,
        reason=reason,
        evidence={
            "list_name": hit.list_name,
            "matched_name": hit.matched_name,
            "match_score": round(hit.match_score, 1),
            "entity_id": hit.entity_id,
            "name_match_strength": strength,
            "identifier_comparisons": [dict(c) for c in hit.comparisons],
            "contradicted_by": list(conflicts),
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
    routing = route(total, thresholds)

    # A SANCTIONS HIT ALWAYS GOES TO A HUMAN. 2026-10-2.
    #
    # This withdraws a rule the previous version stated deliberately. Up to
    # 2026-10-1 the argument above SANCTIONS_POINTS read:
    #
    #     "So 'confirmed' is 60: enough to guarantee review, not enough to
    #      refuse anyone by itself. Reaching 80 needs a second, independent
    #      signal -- a failed document check, a call-for-action jurisdiction."
    #
    # That second route to automatic refusal is now closed, for two reasons.
    #
    # A genuine sanctions match must be ESCALATED AND REPORTED, not silently
    # refused. Quietly turning away a listed person is not the cautious
    # outcome; it loses the one case a compliance function exists to act on.
    #
    # And the arithmetic was backwards at the weak end. Enumerating all 720
    # combinations of a recorded sanctions hit against every other signal,
    # 481 of them (66.8%) reached the auto-reject threshold, the smallest
    # being: a hit the list's OWN date of birth contradicts (20) + a
    # call-for-action country (40) + an in-progress document check (20) = 80.
    # That is an automatic refusal of someone the evidence suggests is a
    # different person entirely.
    #
    # Auto-approval was never reachable -- the weakest sanctions band is 20
    # and the threshold is "below 20" -- so only the rejection half needed
    # closing. Both halves are asserted in tests/test_sanctions_routing.py.
    if any(hit.match_type == "sanctions" for hit in profile.hits):
        routing = Routing.REVIEW

    return RiskAssessment(
        score=total,
        signals=tuple(signals),
        routing=routing,
        thresholds=thresholds,
    )
