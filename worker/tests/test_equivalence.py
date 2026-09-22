"""Does the database matcher agree with the in-memory one? Exactly?

THE POINT OF THIS FILE

Moving the sanctions list from RAM into Postgres changes how candidates are
FOUND. It must not change who gets flagged. A silent shift in that is the worst
outcome available here: nobody would see it, the tests would stay green, and
the only evidence would be a different set of people being refused accounts.

So both matchers are run over the same applicants and compared on everything
that can affect a decision:

    the set of matched entities      who was flagged
    the strength of each match       how strongly
    the matched spelling             which alias did it
    the date-of-birth conflict flag  whether it was downgraded
    the resulting risk score         what the system decided
    the routing                      approve / review / reject

WHY EQUIVALENCE IS EVEN POSSIBLE

The two share stage 2. `store.search` imports `compare` and
`aligned_token_count` from `index` rather than reimplementing them, so the
scoring arithmetic is not merely equivalent, it is the same code. That reduces
the whole question to recall: does the SQL shortlist ever fail to offer up a
candidate the in-memory scan would have scored?

Which is why a divergence here is reported as a RECALL failure with the
specific name pair and both scores, rather than as a generic mismatch.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from config import SANCTIONS_SOURCE  # noqa: E402
from scoring import ApplicantProfile, ScreeningHit, score_application  # noqa: E402
from screening import store  # noqa: E402
from screening.sources import load_index, load_into_postgres  # noqa: E402

#: How many seeded applicants to compare. Every one costs a full in-memory
#: scan of 44,017 spellings, which is the slow half.
SAMPLE_SIZE = 400


@pytest.fixture(scope="module")
def db():
    """The DEVELOPMENT database, not the throwaway one — and deliberately.

    This test needs two things the `_test` database does not have: the OFAC
    list actually loaded (19,393 entries, four minutes to write) and a few
    hundred seeded applicants with real transliterations and collisions in
    their names. Rebuilding both per test session would cost minutes and buy
    nothing, because neither is modified here.

    It is effectively read-only. `load_into_postgres` is idempotent by
    snapshot: it finds `entries_loaded_at` already set and returns without
    writing. The only write is one throwaway row in the half-loaded test at the
    bottom, which is inert by construction — nothing will ever search it.
    """
    from db import database_url

    try:
        conn = psycopg.connect(database_url(), autocommit=True, connect_timeout=5)
    except Exception as err:  # noqa: BLE001
        pytest.skip(f"no database reachable: {err}")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def memory_index():
    """The old matcher, loaded from disk exactly as the worker used to."""
    return load_index(SANCTIONS_SOURCE)


@pytest.fixture(scope="module")
def loaded(db, memory_index):
    """The same list, in Postgres, guaranteed searchable."""
    load_into_postgres(db, memory_index)
    snapshot = store.active_snapshot(db, SANCTIONS_SOURCE)
    assert snapshot is not None, "no fully-loaded snapshot to search"
    return snapshot


@pytest.fixture(scope="module")
def applicants(db):
    """Real seeded people, not invented names.

    Invented test names would be chosen — consciously or not — from the cases
    already understood. The seeded population contains the awkward ones nobody
    thought to write down: double surnames, patronymics, transliterations, and
    the deliberate collisions with listed names.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            select full_name, date_of_birth::text, address_country
              from applications
             order by id
             limit %s
            """,
            (SAMPLE_SIZE,),
        )
        rows = cur.fetchall()
    if len(rows) < 50:
        pytest.skip(
            f"only {len(rows)} applicants seeded; run worker/seed.py for a "
            "meaningful comparison"
        )
    return rows


def as_comparable(matches):
    """A match, reduced to everything that can change a decision."""
    return {
        m.entry.entity_id: (
            round(m.score, 6),
            m.matched_name,
            m.date_of_birth_conflict,
            m.entry.entity_type,
        )
        for m in matches
    }


def assess(matches, country, vendor_status="Approved"):
    """Run the real scorer over a match set, so routing is compared too."""
    profile = ApplicantProfile(
        country=country,
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
    return score_application(profile)


def test_the_two_matchers_agree_on_every_applicant(
    db, memory_index, loaded, applicants
):
    """The headline. Same people in, same matches and same decisions out."""
    divergences = []
    matched_applicants = 0

    for full_name, dob, country in applicants:
        old = memory_index.search(full_name, date_of_birth=dob)
        new = store.search(db, loaded, full_name, date_of_birth=dob)

        old_map, new_map = as_comparable(old), as_comparable(new)
        if old_map:
            matched_applicants += 1

        if old_map != new_map:
            missed = set(old_map) - set(new_map)
            extra = set(new_map) - set(old_map)
            changed = {
                k for k in set(old_map) & set(new_map) if old_map[k] != new_map[k]
            }
            divergences.append(
                {
                    "applicant": full_name,
                    "dob": dob,
                    # A MISS is the serious direction: the database matcher
                    # failed to shortlist something the scan found.
                    "missed_by_shortlist": {k: old_map[k] for k in missed},
                    "found_only_by_database": {k: new_map[k] for k in extra},
                    "scored_differently": {
                        k: {"memory": old_map[k], "database": new_map[k]}
                        for k in changed
                    },
                }
            )
            continue

        # Same matches. Do they also produce the same decision?
        old_score = assess(old, country)
        new_score = assess(new, country)
        if (old_score.score, old_score.routing) != (new_score.score, new_score.routing):
            divergences.append(
                {
                    "applicant": full_name,
                    "same_matches_different_decision": {
                        "memory": (old_score.score, old_score.routing),
                        "database": (new_score.score, new_score.routing),
                    },
                }
            )

    assert matched_applicants > 0, (
        "no applicant in the sample matched anything — this test would pass "
        "trivially and prove nothing about recall"
    )

    assert not divergences, (
        f"{len(divergences)} of {len(applicants)} applicants disagree "
        f"(of {matched_applicants} that matched anything):\n"
        + "\n".join(f"  {d}" for d in divergences[:12])
    )


def test_the_shortlist_is_smaller_than_the_list(db, loaded, applicants):
    """The narrowing has to actually narrow, or none of this was worth doing.

    Not a correctness property — recall is what matters and the test above
    covers it — but a shortlist that returned everything would be equivalent
    AND pointless, and that would be worth knowing.
    """
    from screening.normalise import normalise_name
    from screening.store import MIN_TOKEN_LENGTH, TOKEN_TRIGRAM_THRESHOLD

    sizes = []
    for full_name, _dob, _country in applicants[:120]:
        tokens = [
            t for t in normalise_name(full_name).split() if len(t) >= MIN_TOKEN_LENGTH
        ]
        if len(tokens) < 2:
            continue
        with db.cursor() as cur:
            cur.execute("select set_limit(%s::real)", (TOKEN_TRIGRAM_THRESHOLD,))
            cur.execute(
                """
                with q as (select distinct unnest(%s::text[]) as token)
                select count(*) from (
                    select t.entry_id
                      from q join sanctions_name_tokens t on t.token %% q.token
                      join sanctions_entries e on e.id = t.entry_id
                     where e.snapshot_id = %s
                     group by t.entry_id
                    having count(distinct q.token) >= 2
                ) x
                """,
                (tokens, loaded.snapshot_id),
            )
            sizes.append(cur.fetchone()[0])

    assert sizes, "no applicant had two usable tokens"
    average = sum(sizes) / len(sizes)
    worst = max(sizes)
    print(
        f"\n  shortlist: avg {average:.1f}, worst {worst}, "
        f"from {loaded.record_count} entries "
        f"({average / loaded.record_count * 100:.3f}% of the list)"
    )
    assert average < loaded.record_count * 0.02, (
        f"shortlist averages {average:.0f} of {loaded.record_count} entries — "
        "the index is not narrowing anything"
    )


def test_a_half_loaded_snapshot_is_never_searched(db):
    """The failure that looks like success.

    A snapshot row exists as soon as a version is registered; its entries are
    written afterwards. If the matcher would search a snapshot whose load died
    halfway, it would return fewer matches and report them as a clean result.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            insert into sanctions_snapshots
                (source, content_hash, record_count, published_at)
            values ('halfloaded', repeat('a', 64), 10, null)
            on conflict (source, content_hash) do nothing
            """
        )
    assert store.active_snapshot(db, "halfloaded") is None, (
        "a snapshot with no entries_loaded_at was offered for searching"
    )
