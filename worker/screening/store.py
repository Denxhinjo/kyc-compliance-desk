"""The sanctions list, in Postgres, searched in two stages.

STAGE 1 — Postgres narrows. A trigram GIN index over individual name tokens
finds every entry sharing at least two plausible name parts with the query.
Tens of thousands of candidates become a handful, in milliseconds, without a
byte of list data crossing the wire.

STAGE 2 — Python scores. `compare()` and `aligned_token_count()` from
index.py, unchanged, applied to that handful. Not reimplemented, not
approximated: imported.

That split is what makes this safe to swap in. Stage 2 being literally the same
code means the ONLY way behaviour can change is if stage 1 fails to shortlist
something the old matcher would have caught — a recall question, with a single
number behind it, measured in tests/test_equivalence.py rather than assumed.

WHAT THIS REPLACES

`SanctionsIndex` held 44,017 normalised spellings in memory and scanned all of
them per applicant. Correct, fast enough, and 86MB resident — fine for a
process that runs for weeks, impossible for one that must start, answer and
exit. The output type is deliberately identical (`Match`), so everything
downstream — scoring, screening_results, the case view — is untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

from .index import (
    MIN_ALIGNED_TOKENS,
    RECORD_THRESHOLD,
    ListEntry,
    Match,
    _dates_conflict,
    aligned_token_count,
    compare,
)
from .normalise import normalise_name

log = logging.getLogger("worker.screening.store")

#: How alike two name TOKENS must be, as trigram similarity, to shortlist.
#:
#: This is the one number that decides whether the new matcher can miss
#: something the old one found, and it is set from measurement, not taste.
#: Measured, then lowered until the equivalence test found nothing: 0.35 lost
#: 13 applicants' matches, 0.30 lost one ("jing" against "ying" is 0.25), and
#: 0.25 loses none across 400.
#:
#: Recall is worth far more than shortlist size here. A shortlist that is twice
#: as large costs microseconds of Python; a shortlist that is missing the one
#: true match costs a sanctioned person an account.
TOKEN_TRIGRAM_THRESHOLD = 0.25

#: EVERY token shortlists, however short.
#:
#: There was a MIN_TOKEN_LENGTH = 3 here, on the reasoning that trigram
#: similarity is unreliable on very short strings. True, and irrelevant: it is
#: a filter the in-memory matcher does not have, and every filter stage 1 has
#: that stage 2 does not is a way to lose a match.
#:
#: "Jing Li" is the case that proved it. Dropping "li" left one usable token;
#: "jing" against "ying" is 0.25 trigram and so missed "Ying LI" at 85.7 — but
#: the entry's own token "li" is an EXACT match, and would have shortlisted it
#: instantly. The cheap filter discarded the strongest signal available because
#: it looked weak in isolation.
#:
#: Short tokens are noisy. The ORDER BY below handles that, by ranking
#: candidates that match more query tokens, and more strongly, above those that
#: happen to share an "al".
MIN_TOKEN_LENGTH = 0


@dataclass(frozen=True)
class StoredList:
    """A loaded snapshot, addressed by id rather than held in memory."""

    snapshot_id: int
    source: str
    content_hash: str
    published_at: str | None
    record_count: int

    def __len__(self) -> int:
        return self.record_count


def active_snapshot(conn: psycopg.Connection, source: str) -> StoredList | None:
    """The newest fully-loaded snapshot for a source.

    `entries_loaded_at is not null` is doing real work: a snapshot row exists
    from the moment a list version is registered, but its entries are written
    afterwards and that write can fail halfway. Screening against a half-loaded
    list would under-match silently, which is this system's worst failure mode
    because the result looks clean.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, source, content_hash, published_at::text, record_count
              from sanctions_snapshots
             where source = %s
               and entries_loaded_at is not null
             order by downloaded_at desc
             limit 1
            """,
            (source,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return StoredList(
        snapshot_id=row[0],
        source=row[1],
        content_hash=row[2],
        published_at=row[3],
        record_count=row[4],
    )


#: Stage 1.
#:
#: ONE matched token is enough to shortlist. That is deliberate, and it is the
#: correction to a version that required two.
#:
#: Requiring two looked right — it mirrors MIN_ALIGNED_TOKENS — but it applied
#: that rule using a DIFFERENT similarity metric from the one stage 2 uses, and
#: the two disagree badly on short tokens and substrings:
#:
#:     chen / jicheng     fuzz.ratio 72.7 (aligned)   trigram 0.22
#:     mei / limei        fuzz.ratio 75.0 (aligned)   trigram 0.33
#:     khalil / khani     fuzz.ratio 72.7 (aligned)   trigram 0.22
#:
#: So stage 1 was rejecting entries that stage 2 would have accepted — 13 of
#: 400 applicants lost a real match. The equivalence test caught every one.
#:
#: The lesson generalises past this file: a cheap pre-filter must only ever
#: WIDEN. The moment it reimplements the expensive filter's judgement with a
#: cheaper metric, it can disagree with it, and every disagreement is a miss
#: nobody sees. So this asks the weakest useful question — "does this entry
#: share any plausible name part with the applicant?" — and leaves every
#: actual decision to stage 2.
#:
#: `token % q.token` is the indexable trigram operator. set_limit() below fixes
#: what `%` means for this connection, because pg_trgm's threshold is session
#: state rather than an argument.
_SHORTLIST_SQL = """
with query_tokens as (
    select distinct unnest(%(tokens)s::text[]) as token
)
select e.id,
       e.entity_id,
       e.name,
       e.entity_type,
       e.list_name,
       e.countries,
       e.date_of_birth,
       count(distinct q.token) as matched_tokens
  from query_tokens q
  join sanctions_name_tokens t
    on t.token %% q.token
  join sanctions_entries e
    on e.id = t.entry_id
 where e.snapshot_id = %(snapshot_id)s
 group by e.id, e.entity_id, e.name, e.entity_type, e.list_name,
          e.countries, e.date_of_birth
 order by count(distinct q.token) desc, max(similarity(t.token, q.token)) desc
 limit %(limit)s
"""

_SPELLINGS_SQL = """
select entry_id, spelling
  from sanctions_names
 where entry_id = any(%(entry_ids)s)
"""


def search(
    conn: psycopg.Connection,
    snapshot: StoredList,
    full_name: str,
    *,
    date_of_birth: str | None = None,
    threshold: float = RECORD_THRESHOLD,
    shortlist_limit: int = 4000,
) -> list[Match]:
    """Everything on the list this person might be, strongest first.

    Signature and return type match SanctionsIndex.search deliberately: the
    caller should not be able to tell which implementation it got.
    """
    query = normalise_name(full_name)
    if not query:
        return []

    all_tokens = query.split()
    if len(all_tokens) < MIN_ALIGNED_TOKENS:
        # A one-word name cannot satisfy the two-part alignment rule in stage 2
        # either, so there is genuinely nothing to find.
        return []

    tokens = [t for t in all_tokens if len(t) >= MIN_TOKEN_LENGTH] or all_tokens

    with conn.cursor() as cur:
        # pg_trgm's `%` operator reads its threshold from session state. Set it
        # per call rather than globally so a connection borrowed from a pool
        # cannot inherit somebody else's idea of "similar".
        # ::real because pg_trgm declares set_limit(real) and psycopg sends a
        # Python float as double precision, which matches no overload.
        cur.execute("select set_limit(%s::real)", (TOKEN_TRIGRAM_THRESHOLD,))
        cur.execute(
            _SHORTLIST_SQL,
            {
                "tokens": tokens,
                "snapshot_id": snapshot.snapshot_id,
                "limit": shortlist_limit,
            },
        )
        candidates = cur.fetchall()

    if not candidates:
        return []

    entry_ids = [row[0] for row in candidates]
    with conn.cursor() as cur:
        cur.execute(_SPELLINGS_SQL, {"entry_ids": entry_ids})
        spellings: dict[int, list[str]] = {}
        for entry_id, spelling in cur.fetchall():
            spellings.setdefault(entry_id, []).append(spelling)

    # --- stage 2: identical to the in-memory matcher ------------------------
    best: dict[str, Match] = {}
    for row in candidates:
        (
            entry_pk,
            entity_id,
            name,
            entity_type,
            list_name,
            countries,
            listed_dob,
            _matched,
        ) = row

        entry = ListEntry(
            entity_id=entity_id,
            name=name,
            aliases=tuple(s for s in spellings.get(entry_pk, []) if s != name),
            list_name=list_name,
            entity_type=entity_type,
            countries=tuple(countries or ()),
            date_of_birth=listed_dob,
        )
        conflict = _dates_conflict(date_of_birth, entry.date_of_birth)

        for spelling in spellings.get(entry_pk, [name]):
            score = compare(query, spelling)
            if score < threshold:
                continue
            if aligned_token_count(query, spelling) < MIN_ALIGNED_TOKENS:
                continue
            existing = best.get(entity_id)
            if existing is None or score > existing.score:
                best[entity_id] = Match(
                    entry=entry,
                    score=float(score),
                    matched_name=spelling,
                    date_of_birth_conflict=conflict,
                )

    # Ties broken by entity id — see the note on the same sort in index.py.
    return sorted(best.values(), key=lambda m: (-m.score, m.entry.entity_id))
