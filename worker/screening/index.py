"""Searching a sanctions/PEP list by name.

Holds the list in memory and answers "who on this list could this person be?"
with a strength for each candidate, never a yes/no. The decision about what a
given strength MEANS belongs to scoring.py, and keeping that boundary sharp is
what lets the threshold be argued about without touching the matching code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from rapidfuzz import fuzz, process

from .identifiers import (
    Comparison,
    compare_dates_of_birth,
    compare_nationality,
    compare_suffix,
)
from .normalise import normalise_name, suffix_of

#: Below this, a pair is not recorded at all.
#:
#: Not a decision threshold — a noise floor. Everything at or above it is
#: written to screening_results so the officer can see the near-misses; what
#: each band is WORTH is decided in scoring.py. An officer who cannot see the
#: weak matches cannot judge whether the strong one is a coincidence.
RECORD_THRESHOLD = 80.0

#: Two name parts must correspond before a pair is considered at all.
#:
#: This exists because of a measured failure, not a theory. token_set_ratio
#: returns 100 whenever one name's tokens are a SUBSET of the other's, which is
#: what makes "Vladimir Putin" match "Vladimir Vladimirovich Putin" correctly —
#: and also makes "Ibrahim Osei" match a list entry reading "DR. IBRAHIM" at
#: 100, and "Sarah Khan" match an entry reading "KHAN".
#:
#: OFAC's SDN list contains 951 single-token entries. Against a 400-applicant
#: population that one flaw produced a 31.5% confirmed-sanctions rate and a
#: 31.8% automatic rejection rate. It would have rejected a third of a real
#: customer book.
#:
#: Requiring two corresponding parts is also how a human compares two names:
#: sharing a first name is not a match, sharing a first name and a surname is.
MIN_ALIGNED_TOKENS = 2

#: How alike two name PARTS must be to count as the same part.
#:
#: Loose on purpose — this is per-token, and transliteration differences live
#: inside words: "ahmed"/"ahmad" is 80, "mohammed"/"muhammad" is 75,
#: "sayed"/"sayyid" is 72. Set it at 80 and those stop aligning. Meanwhile
#: genuinely different parts score far below: "hassan"/"hussein" is 46,
#: "john"/"jane" is 50. There is a wide gap here, unlike at the whole-name
#: level, because a token comparison is not diluted by the parts that DO match.
TOKEN_ALIGNMENT_THRESHOLD = 70.0


@dataclass(frozen=True)
class ListEntry:
    """One person or organisation on a list."""

    entity_id: str
    name: str
    #: Alternative spellings the list itself publishes. These matter more than
    #: any threshold: "Mohammed Al-Sayed" against "Muhammad Al Sayyid" scores
    #: 80 by string similarity alone, which no sane threshold treats as
    #: confirmed. If the list carries both spellings as aliases, the match
    #: becomes exact and the problem disappears.
    aliases: tuple[str, ...] = ()
    list_name: str = "unknown"
    #: 'sanctions' | 'pep' | 'adverse_media' — matches screening_results.match_type.
    entity_type: str = "sanctions"
    countries: tuple[str, ...] = ()
    #: ISO date, or a partial year like "1975". Used to CONTRADICT a match, not
    #: to confirm one: a conflicting date of birth is strong evidence of a
    #: different person, while a matching one is weak evidence of the same one.
    #:
    #: The FIRST listed date, kept for the v1 scoring path exactly as it
    #: behaved before 2026-10-2. New code reads `dates_of_birth`.
    date_of_birth: str | None = None
    #: EVERY date the list publishes for this entity, in list order.
    #:
    #: 532 OFAC SDN entries publish more than one. Keeping only the first let
    #: the system declare a conflict with a date the list itself also gives.
    #: Defaults to (date_of_birth,) so every existing construction keeps
    #: working and means what it used to.
    dates_of_birth: tuple[str, ...] = ()
    #: Secondary identifiers, straight from the snapshot. Empty means the list
    #: is silent, which is shown as "not available" and never inferred.
    nationalities: tuple[str, ...] = ()
    places_of_birth: tuple[str, ...] = ()
    citizenships: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.dates_of_birth and self.date_of_birth:
            object.__setattr__(self, "dates_of_birth", (self.date_of_birth,))
        elif self.dates_of_birth and self.date_of_birth is None:
            object.__setattr__(self, "date_of_birth", self.dates_of_birth[0])


@dataclass(frozen=True)
class Match:
    entry: ListEntry
    #: 0-100.
    score: float
    #: Which spelling actually matched — the primary name or a specific alias.
    matched_name: str
    #: True when the entry publishes a date of birth and it disagrees with the
    #: applicant's. Recorded here, weighed in scoring.py.
    date_of_birth_conflict: bool = False
    #: The score under the ruleset's own similarity definition. Equal to
    #: `score` under v1, lower under v2 wherever parts went unmatched.
    name_score: float = 0.0
    #: Every secondary identifier, compared. See screening/identifiers.py.
    comparisons: tuple[Comparison, ...] = ()

    def comparison(self, identifier: str) -> Comparison | None:
        for item in self.comparisons:
            if item.identifier == identifier:
                return item
        return None


def aligned_token_count(left: str, right: str) -> int:
    """How many name parts correspond between two names.

    Greedy one-to-one: each token of the first name claims its best unclaimed
    partner in the second. Greedy rather than optimal assignment because the
    difference only shows up in contrived cases and the Hungarian algorithm is
    not worth explaining to a compliance officer.

    Order-independent, so it works for "Xi Jinping" against "Jinping Xi".
    """
    _, _, pairs = align_tokens(left, right)
    return len(pairs)


def align_tokens(left: str, right: str) -> tuple[list[str], list[str], list[float]]:
    """The alignment itself: both token lists and the score of each pair.

    Split out from `aligned_token_count` so the v2 scorer can see HOW WELL the
    parts matched and how many were left over on each side, which is the whole
    of the difference between the two definitions.
    """
    left_tokens = normalise_name(left).split()
    right_tokens = normalise_name(right).split()
    if not left_tokens or not right_tokens:
        return left_tokens, right_tokens, []

    claimed: set[int] = set()
    pairs: list[float] = []
    for token in left_tokens:
        best_score, best_index = 0.0, None
        for index, other in enumerate(right_tokens):
            if index in claimed:
                continue
            score = fuzz.ratio(token, other)
            if score > best_score:
                best_score, best_index = score, index
        if best_index is not None and best_score >= TOKEN_ALIGNMENT_THRESHOLD:
            claimed.add(best_index)
            pairs.append(best_score)
    return left_tokens, right_tokens, pairs


def compare(left: str, right: str) -> float:
    """Similarity between two names, 0-100.

    max() of two scorers because they fail in opposite directions:

      token_set_ratio ignores word order and extra tokens, which is what makes
      "Xi Jinping" match "Jinping Xi" at 100 and "Vladimir Putin" match
      "Vladimir Vladimirovich Putin" at 100. It is blind to spelling drift
      inside a word.

      ratio compares the strings character by character, which catches
      "Sergey Ivanov" against "Sergei Ivanoff". It is punished heavily by
      reordering.

    Taking the higher of the two means a pair only has to be similar in one of
    those senses. That raises recall and, deliberately, raises false positives
    with it — which is the trade this whole phase is about, and is why the
    result is a strength rather than a verdict.
    """
    a, b = normalise_name(left), normalise_name(right)
    if not a or not b:
        return 0.0
    return max(fuzz.token_set_ratio(a, b), fuzz.ratio(a, b))


def compare_symmetric(left: str, right: str) -> float:
    """Similarity that charges for unmatched parts on BOTH sides. (v2)

    THE DEFECT THIS REPLACES

    `compare` above takes max(token_set_ratio, ratio), and token_set_ratio
    returns 100 whenever one name's tokens are a SUBSET of the other's. It is
    blind to what the list entry has and the applicant does not. Measured on
    the demo's own queue, 28 pairs sat at 100% while being different names:

        Mohammed Nasser  ->  Abdal Nasser Sharif Mohammed Said JAMOOS   100%
        Carlos Fernandez ->  Carlos Ariel FERNANDEZ CONCEPCION          100%

    Two of six parts, announced as a perfect match. Common partial names are
    the classic source of false positives in sanctions screening, and a
    reviewer reading "100%" has been told the opposite of the truth.

    HOW IT SCORES

        precision = aligned / applicant parts   how much of the APPLICANT matched
        recall    = aligned / list parts        how much of the LIST ENTRY matched
        coverage  = harmonic mean of the two    an unmatched part EITHER side costs
        quality   = mean similarity of the aligned pairs

        score = 100 * coverage * quality

    The harmonic mean rather than the average because it punishes the lopsided
    case, which is exactly the failure above: precision 1.0 with recall 0.33
    averages to a respectable 0.67 and harmonises to 0.5.

    `quality` keeps transliteration drift costing something -- "Sergei Ivanoff"
    against "Sergey Ivanov" scores 80.1 rather than 100 -- while the existing
    per-token alignment threshold keeps it from costing too much.

    PROPERTY, TESTED: the score is 100 if and only if the two normalised token
    multisets are equal. So a reordered name ("KIM Tong-Myo'ng" against
    "Tong-Myo'ng KIM") still scores 100, because it is the same name written
    the other way round, and no strict subset ever can.
    """
    left_tokens, right_tokens, pairs = align_tokens(left, right)
    if not left_tokens or not right_tokens or not pairs:
        return 0.0

    aligned = len(pairs)
    precision = aligned / len(left_tokens)
    recall = aligned / len(right_tokens)
    coverage = 2 * precision * recall / (precision + recall)
    quality = sum(pairs) / aligned / 100.0
    return 100.0 * coverage * quality


#: The similarity definitions a ruleset can name.
#:
#: A ruleset row selects one of these by key. `rulesets` is append-only, so
#: 2026-10-1's row cannot be amended to name its own definition -- which is
#: why an ABSENT key means v1. That is not a default for convenience: it is
#: what makes "reconstruct any decision" true. A decision stamped 2026-10-1
#: can never be replayed under v2, and nothing stamped 2026-10-2 under v1.
SIMILARITY_DEFINITIONS = {
    "v1-max-token-set": compare,
    "v2-symmetric-coverage": compare_symmetric,
}

#: What a ruleset that names no definition gets. See above.
DEFAULT_SIMILARITY = "v1-max-token-set"


def similarity_for(name: str | None):
    """The scorer a ruleset names, or v1 where it names none."""
    key = name or DEFAULT_SIMILARITY
    try:
        return SIMILARITY_DEFINITIONS[key]
    except KeyError:
        raise ValueError(
            f"unknown name similarity definition {key!r}; "
            f"a ruleset names one of {sorted(SIMILARITY_DEFINITIONS)}"
        ) from None


@dataclass
class SanctionsIndex:
    """An in-memory list, searchable by name."""

    entries: tuple[ListEntry, ...]
    source: str = "synthetic"

    #: sha256 of the file this was loaded from. The identity of the list: two
    #: runs with the same hash screened against exactly the same content.
    content_hash: str = ""
    #: The publisher's own version stamp, where they publish one. None for the
    #: synthetic fixture, which is fabricated and has no publisher.
    published_at: str | None = None

    #: Every searchable spelling, flattened: (normalised, original, entry).
    #: Built once so a screening run does not renormalise the whole list.
    _searchable: list[tuple[str, str, ListEntry]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        for entry in self.entries:
            for spelling in (entry.name, *entry.aliases):
                normalised = normalise_name(spelling)
                if normalised:
                    self._searchable.append((normalised, spelling, entry))

    def __len__(self) -> int:
        return len(self.entries)

    def search(
        self,
        full_name: str,
        *,
        date_of_birth: str | None = None,
        threshold: float = RECORD_THRESHOLD,
        similarity: str | None = None,
        applicant_date_of_birth: object | None = None,
        nationality: str | None = None,
    ) -> list[Match]:
        """Everything on the list this person might be, strongest first.

        One row per ENTITY, not per spelling: an entry with six aliases must not
        appear six times, and the score kept is the best any of its spellings
        achieved.
        """
        query = normalise_name(full_name)
        if not query:
            return []

        # RETRIEVAL IS NOT RESCORED. The two-pass extract below stays exactly
        # as it was under v1, so every candidate that surfaced before still
        # surfaces: changing the definition must not quietly reduce recall.
        # What the ruleset selects is how a surfaced candidate is SCORED.
        score_name = similarity_for(similarity)
        applicant_suffix = suffix_of(full_name)

        choices = [item[0] for item in self._searchable]

        # Two passes rather than a Python callable, because a custom scorer
        # drops rapidfuzz out of its C implementation and onto the interpreter —
        # roughly two orders of magnitude on a twenty-thousand-entry list.
        scored: dict[int, float] = {}
        for scorer in (fuzz.token_set_ratio, fuzz.ratio):
            for _, score, position in process.extract(
                query, choices, scorer=scorer, score_cutoff=threshold, limit=None
            ):
                if score > scored.get(position, 0.0):
                    scored[position] = score

        best: dict[str, Match] = {}
        for position, score in scored.items():
            _, original_spelling, entry = self._searchable[position]

            # Two name parts must correspond. See MIN_ALIGNED_TOKENS above —
            # without this, single-token list entries match a third of an
            # ordinary customer book at 100%.
            #
            # An earlier version required one EXACTLY shared token, which was
            # worse than useless: "Ahmed Hassan" and "Ahmad Hasan" share no
            # exact token at all, so the headline transliteration case would
            # have been silently discarded.
            if aligned_token_count(query, original_spelling) < MIN_ALIGNED_TOKENS:
                continue

            name_score = score_name(query, original_spelling)

            dob = compare_dates_of_birth(applicant_date_of_birth, entry.dates_of_birth)
            comparisons = (
                dob,
                compare_suffix(applicant_suffix, suffix_of(original_spelling)),
                compare_nationality(nationality, entry.nationalities),
            )

            # v1 keeps its own notion of a conflict, which knows nothing about
            # multiple dates or imprecise ones. A decision taken under an
            # earlier ruleset has to stay reproducible, so this is not
            # "improved" for the v1 path.
            conflict = (
                _dates_conflict(date_of_birth, entry.date_of_birth)
                if similarity in (None, DEFAULT_SIMILARITY)
                else dob.conflicts
            )

            existing = best.get(entry.entity_id)
            # Ranked by the ruleset's own score: under v2 a different alias may
            # be the best spelling, and the recorded one must be the one that
            # actually scored best under the definition in force.
            if existing is None or name_score > existing.name_score:
                best[entry.entity_id] = Match(
                    entry=entry,
                    score=float(score),
                    matched_name=original_spelling,
                    date_of_birth_conflict=conflict,
                    name_score=float(name_score),
                    comparisons=comparisons,
                )

        # Sorted by score, then by entity id to break ties.
        #
        # The tiebreak is not cosmetic. scoring.py takes the STRONGEST hit of
        # each type, which with `max()` means the first of several equals — and
        # equals are common: "Sa'ad Muhammad Yunis AL-AHMAD" matches three OFAC
        # entities at exactly 100.00, two with no date-of-birth conflict and
        # one with. Whichever is seen first decides whether the band is
        # downgraded, which is a 15-point swing in the risk score.
        #
        # Before this, that order came from dict iteration over rapidfuzz's
        # result positions: stable within a run, arbitrary between
        # implementations, and not a property anybody had chosen. Sorting by
        # entity id makes it a decision rather than an accident.
        return sorted(
            best.values(), key=lambda m: (-m.name_score, -m.score, m.entry.entity_id)
        )


def _dates_conflict(applicant: str | None, listed: str | None) -> bool:
    """Whether two dates of birth positively disagree.

    Asymmetric on purpose. A CONFLICT is strong evidence of two different
    people; agreement is only weak evidence of one, because a common name plus
    a common birth year is not rare. So this answers "do these contradict",
    never "do these confirm".

    Compared by year only. Lists carry partial dates ("1975", "1975-00-00"),
    day and month are frequently wrong or transposed between sources, and a
    year mismatch is the part that actually means something.
    """
    if not applicant or not listed:
        return False
    applicant_year, listed_year = applicant[:4], listed[:4]
    if not (applicant_year.isdigit() and listed_year.isdigit()):
        return False
    return applicant_year != listed_year


def build_index(
    entries: Iterable[ListEntry],
    source: str,
    *,
    content_hash: str = "",
    published_at: str | None = None,
) -> SanctionsIndex:
    return SanctionsIndex(
        entries=tuple(entries),
        source=source,
        content_hash=content_hash,
        published_at=published_at,
    )
