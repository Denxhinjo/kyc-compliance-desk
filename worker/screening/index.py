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

from .normalise import normalise_name

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
    date_of_birth: str | None = None


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


def aligned_token_count(left: str, right: str) -> int:
    """How many name parts correspond between two names.

    Greedy one-to-one: each token of the first name claims its best unclaimed
    partner in the second. Greedy rather than optimal assignment because the
    difference only shows up in contrived cases and the Hungarian algorithm is
    not worth explaining to a compliance officer.

    Order-independent, so it works for "Xi Jinping" against "Jinping Xi".
    """
    left_tokens = normalise_name(left).split()
    right_tokens = normalise_name(right).split()
    if not left_tokens or not right_tokens:
        return 0

    claimed: set[int] = set()
    aligned = 0
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
            aligned += 1
    return aligned


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
    ) -> list[Match]:
        """Everything on the list this person might be, strongest first.

        One row per ENTITY, not per spelling: an entry with six aliases must not
        appear six times, and the score kept is the best any of its spellings
        achieved.
        """
        query = normalise_name(full_name)
        if not query:
            return []

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

            conflict = _dates_conflict(date_of_birth, entry.date_of_birth)
            existing = best.get(entry.entity_id)
            if existing is None or score > existing.score:
                best[entry.entity_id] = Match(
                    entry=entry,
                    score=float(score),
                    matched_name=original_spelling,
                    date_of_birth_conflict=conflict,
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
        return sorted(best.values(), key=lambda m: (-m.score, m.entry.entity_id))


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
