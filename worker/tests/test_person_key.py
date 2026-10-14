"""Linking repeat applications from the same person (migration 028).

WHAT THIS IS FOR

On 2026-09-23 one person submitted the public form five times in 47 minutes.
Every submission was handled exactly once — five application ids, five vendor
sessions, five separate screenings, one decision each — but the review queue
showed four rows with the same name, the same date of birth, the same score and
the same sanctions match, with nothing to say they were five applications
rather than one processed five times.

A visitor who notices that concludes the queue is displaying the
duplicate-processing bug this project claims to prevent. So the fix is to say
"5 applications from this person" out loud.

EXACT MATCH ONLY, AND THE DIRECTION OF THE ERROR MATTERS

Normalised name AND date of birth, both exactly equal. The output is shown to a
reviewer as a statement of fact — "these are the same person" — with no second
check in front of it, unlike a sanctions hit, which a human always judges. So a
false positive would assert in the case file that two different applicants are
one person.

The rule therefore errs the other way: two records that really are one person
but spell the name differently simply do not link. Several tests below assert
that failure direction deliberately, because it is the safe one and someone
will eventually be tempted to "improve" it with fuzzy matching.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")


@pytest.fixture(scope="module")
def conn(schema):
    try:
        connection = psycopg.connect(schema, connect_timeout=5)
    except Exception as err:  # noqa: BLE001
        pytest.skip(f"no database reachable: {err}")
    try:
        yield connection
    finally:
        connection.close()


def key(conn, name: str, dob: str) -> str:
    with conn.cursor() as cur:
        cur.execute("select person_key(%s, %s::date)", (name, dob))
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# What normalisation covers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b,why",
    [
        ("Sergey Ivanov", "sergey ivanov", "case"),
        ("Sergey Ivanov", "  Sergey   Ivanov  ", "leading, trailing and repeated whitespace"),
        ("Sergey Ivanov", "SERGEY IVANOV", "upper case"),
        ("Jose Garcia", "José García", "acute accents"),
        ("Renee Zuniga", "Renée Zúñiga", "accents and a tilde"),
        ("Soren Kierkegaard", "Søren Kierkegaard", "a slashed o"),
        ("Lukasz Nowak", "Łukasz Nowak", "a stroked L, Latin Extended-A"),
        ("Dorde Petrovic", "Đorđe Petrović", "a stroked D and an acute c"),
        ("Istanbul Resident", "ıstanbul Resident", "a Turkish dotless i"),
    ],
)
def test_these_are_the_same_person(conn, a, b, why):
    """Case, whitespace and Latin diacritics are normalised away.

    The stroked letters are here because they caught a real gap. NFKD splits a
    letter into a base plus a combining mark, so it handles e-acute and
    u-umlaut — and does nothing whatever to a letter that IS its own base
    character. o-with-stroke and l-with-stroke are separate Unicode letters,
    not decorated ones, so the generated map missed every one of them and
    "Soren"/"Søren" did not link until they were added by hand.
    """
    assert key(conn, a, "1975-03-02") == key(conn, b, "1975-03-02"), why


@pytest.mark.parametrize(
    "a,b,why",
    [
        ("Weiss", "Weiß", "eszett expands to two letters"),
        ("Aegir Jonsson", "Ægir Jonsson", "ae-ligature expands to two letters"),
        ("Thorvaldur", "Þorvaldur", "thorn expands to two letters"),
    ],
)
def test_two_character_expansions_are_a_known_gap(conn, a, b, why):
    """translate() maps one character to one character, so these do not link.

    Asserted rather than left to be discovered. The failure direction is the
    safe one — these read as two different people, so nothing untrue is shown
    to a reviewer — and closing it would need a regexp_replace chain per
    character for a case synthetic data has never produced. If a real applicant
    ever hits it, this test is where the decision was recorded.
    """
    assert key(conn, a, "1975-03-02") != key(conn, b, "1975-03-02"), why


@pytest.mark.parametrize(
    "a,b,why",
    [
        ("Sergey Ivanov", "Sergei Ivanov", "a transliteration difference is not normalisation"),
        ("Petrov-Ivanoff", "Petrov Ivanoff", "punctuation is deliberately NOT stripped"),
        ("Sergey Ivanov", "Sergey Ivanov Jr", "an extra name part"),
        ("Sergey Ivanov", "Ivanov Sergey", "name order: no token reordering here"),
        ("Sergey Ivanov", "S Ivanov", "an initial is not a name"),
    ],
)
def test_these_are_not_linked(conn, a, b, why):
    """THE FAILURE DIRECTION, asserted on purpose.

    Each of these pairs might well be one person, and none of them links. That
    is the safe answer for a claim nobody reviews: a missing link shows a
    reviewer less than it could, a false link tells them something untrue. The
    sanctions matcher is allowed to be approximate precisely because a human
    then judges every hit; this is not.
    """
    assert key(conn, a, "1975-03-02") != key(conn, b, "1975-03-02"), why


def test_the_date_of_birth_has_to_match_too(conn):
    """Name alone is not identity. Two Sergey Ivanovs are two people."""
    assert key(conn, "Sergey Ivanov", "1975-03-02") != key(
        conn, "Sergey Ivanov", "1975-03-03"
    )


def test_the_key_contains_the_date_in_a_stable_form(conn):
    """Why extract() and lpad() rather than to_char() or a ::text cast.

    Both of those read the DateStyle setting, which makes them STABLE rather
    than IMMUTABLE — so a generated column built on one would be a lie, and
    two sessions with different DateStyle could disagree about whether two
    applications are the same person.
    """
    assert key(conn, "A Person", "1975-03-02").endswith("|1975-03-02")
    assert key(conn, "A Person", "0999-01-09").endswith("|0999-01-09"), (
        "years and months are zero-padded, so the form is fixed-width"
    )


def test_the_function_really_is_immutable(conn):
    """Required for the generated column, and asserted rather than assumed."""
    with conn.cursor() as cur:
        # identity_arguments includes the parameter NAMES
        # ("full_name text, date_of_birth date"), so match on proname and
        # assert there is exactly one.
        cur.execute(
            "select provolatile from pg_proc where proname = 'person_key'"
        )
        rows = cur.fetchall()
    assert len(rows) == 1, f"expected one person_key function, found {len(rows)}"
    assert rows[0][0] == "i", "person_key must be IMMUTABLE for the generated column"


# ---------------------------------------------------------------------------
# The column, and what reads it
# ---------------------------------------------------------------------------


def _insert(conn, cur, name: str, dob: str) -> str:
    cur.execute(
        """
        insert into applications
            (status, full_name, date_of_birth, address_line1, address_city,
             address_postcode, address_country)
        values ('started', %s, %s, '1 Test Street', 'Testville', 'T1 1TT', 'GB')
        returning id, person_key
        """,
        (name, dob),
    )
    return cur.fetchone()


def test_the_column_is_generated_from_the_row(conn):
    """Stored, computed by the database, and not writable by a caller."""
    with conn.transaction() as tx:
        with conn.cursor() as cur:
            _id, generated = _insert(conn, cur, "Generated Column", "1988-02-29")
            assert generated == "generated column|1988-02-29"
        raise psycopg.Rollback(tx)


def test_a_caller_cannot_write_the_column(conn):
    """GENERATED ALWAYS, so "same person" cannot be asserted by hand.

    If a caller could set it, the definition would no longer live in one place
    and a bug or a convenience could link two people who are not.
    """
    with pytest.raises(psycopg.errors.GeneratedAlways):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into applications
                        (status, full_name, date_of_birth, address_line1,
                         address_city, address_postcode, address_country, person_key)
                    values ('started', 'Forged Key', '1990-01-01', '1 Test Street',
                            'Testville', 'T1 1TT', 'GB', 'someone else|1990-01-01')
                    """
                )


def test_repeat_applications_share_a_key_and_distinct_people_do_not(conn):
    """The query the queue and the case page actually run."""
    marker = uuid.uuid4().hex[:8]
    same = f"Repeat Person {marker}"
    other = f"Different Person {marker}"
    with conn.transaction() as tx:
        with conn.cursor() as cur:
            first = _insert(conn, cur, same, "1980-06-15")[0]
            _insert(conn, cur, same.upper(), "1980-06-15")
            _insert(conn, cur, f"  {same}  ", "1980-06-15")
            _insert(conn, cur, other, "1980-06-15")

            cur.execute(
                """
                select count(*) from applications p
                 where p.person_key = (select person_key from applications where id = %s)
                """,
                (first,),
            )
            assert cur.fetchone()[0] == 3, "three spellings of one person"

            cur.execute(
                """
                select count(*) from applications other
                  join applications self on other.person_key = self.person_key
                 where self.id = %s and other.id <> self.id
                """,
                (first,),
            )
            assert cur.fetchone()[0] == 2, "two OTHERS, which is what the page lists"
        raise psycopg.Rollback(tx)


def test_the_lookup_is_indexed(conn):
    """It runs on every queue render, once per row. Unindexed it is a scan."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select indexdef from pg_indexes
             where tablename = 'applications' and indexname = 'applications_person_key_idx'
            """
        )
        row = cur.fetchone()
    assert row is not None, "applications_person_key_idx is missing"
    assert "person_key" in row[0]


# ---------------------------------------------------------------------------
# Display only
# ---------------------------------------------------------------------------


def test_scoring_never_reads_the_person_key():
    """DISPLAY ONLY, asserted by reading the source.

    Making a repeat application a risk factor is a policy change: the points
    would have to be written into the rulesets table and every decision stamped
    with the version that applied them. Doing it by quietly referencing this
    column would change outcomes with nothing in the audit record to explain
    them, and no way to reconstruct a past decision.

    Recorded as a proposal in docs/decisions.md. Until it is a rules version,
    this fails if scoring so much as mentions the column.
    """
    import pathlib

    worker = pathlib.Path(__file__).resolve().parent.parent
    for name in ("scoring.py", "screening_handler.py"):
        source = (worker / name).read_text(encoding="utf-8")
        assert "person_key" not in source, (
            f"{name} references person_key — if repeat applications are now a "
            "risk factor that needs a new rules version, not a column read"
        )
