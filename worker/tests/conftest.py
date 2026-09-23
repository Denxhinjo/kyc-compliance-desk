"""Shared fixtures for the database-backed tests.

WHY A SEPARATE TEST DATABASE

The lifecycle-trigger tests get away with a transaction per test, rolled back
at the end. That works because everything they do happens on one connection.

It cannot work for most of the tests here. Two workers racing for a job have to
SEE each other's writes, which means committing, which means the rollback trick
is unavailable. And cleaning up afterwards by deleting is not an option either:
`audit_events` refuses DELETE and TRUNCATE, by design, and that rule binds the
test suite exactly as it binds everything else.

So these run against a throwaway database whose schema is dropped and rebuilt
once per session. Pollution stops mattering when the whole thing is disposable.

Rebuilding runs the real `db/migrate.py`, which means every CI run also exercises
the migration runner against an empty database — the path that actually matters
on a deploy and that nothing else covers.

    TEST_DATABASE_URL   used if set
    otherwise           DATABASE_URL with the database name suffixed `_test`

Everything skips cleanly when no database is reachable, so `pytest` still runs
offline on a fresh clone.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest

psycopg = pytest.importorskip("psycopg")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATE = REPO_ROOT / "db" / "migrate.py"


def _test_database_url() -> str | None:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit

    base = os.environ.get("DATABASE_URL")
    if not base:
        return None

    parsed = urlparse(base)
    name = parsed.path.lstrip("/") or "postgres"
    if name.endswith("_test"):
        return base
    return urlunparse(parsed._replace(path=f"/{name}_test"))


def _ensure_database_exists(url: str) -> None:
    """CREATE DATABASE if it is not there yet, from the maintenance database."""
    parsed = urlparse(url)
    target = parsed.path.lstrip("/")
    admin_url = urlunparse(parsed._replace(path="/postgres"))

    # autocommit: CREATE DATABASE cannot run inside a transaction block.
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        with admin.cursor() as cur:
            cur.execute("select 1 from pg_database where datname = %s", (target,))
            if cur.fetchone() is None:
                cur.execute(f'create database "{target}"')


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    if not url:
        pytest.skip("neither TEST_DATABASE_URL nor DATABASE_URL is set")

    try:
        _ensure_database_exists(url)
    except Exception as err:  # noqa: BLE001 — any connection problem means skip
        pytest.skip(f"no database reachable: {err}")

    return url


@pytest.fixture(scope="session")
def schema(database_url: str) -> str:
    """A freshly migrated schema, once per session.

    NOT autouse. Autouse would attach it to all 171 tests, so an unreachable
    database would skip the 121 pure ones too — turning "Postgres is not
    running" into a silently green-looking suite that tested nothing. Only the
    tests that ask for a database get one.

    Dropped and rebuilt rather than cleaned, because audit_events cannot be
    emptied — DROP SCHEMA is the only way back to zero, which is itself a small
    demonstration that the append-only guarantee is real.
    """
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema if exists public cascade")
            cur.execute("create schema public")

    result = subprocess.run(
        [sys.executable, str(MIGRATE), "up"],
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        pytest.fail(
            "migrations failed against the test database:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return database_url


@pytest.fixture(scope="session", autouse=True)
def sanctions_list(schema: str):
    """Put the committed synthetic list into the test database.

    Since the list moved out of the worker's memory and into Postgres, a
    screening job needs a loaded snapshot the way it used to need a file. A
    database with no list is not a neutral starting point any more — it is a
    worker that cannot screen.

    Autouse and session-scoped because it is cheap (25 entries) and because
    every DB-backed test that touches screening would otherwise have to
    remember to ask for it, and the one that forgot would fail confusingly.
    """
    import psycopg

    from screening.sources import load_index, load_into_postgres

    with psycopg.connect(schema, autocommit=True, connect_timeout=5) as conn:
        load_into_postgres(conn, load_index("synthetic"))


@pytest.fixture(scope="session")
def db(schema: str):
    """One committed connection for tests that do not need isolation."""
    conn = psycopg.connect(
        schema, autocommit=True, connect_timeout=5, application_name="kyc_tests"
    )
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def new_connection(schema: str):
    """A factory for extra connections, for tests that need real concurrency.

    Each gets its own connection because that is the only way to have two
    transactions genuinely contend — the thing most of these tests exist to
    prove. Connections are closed when the test finishes.
    """
    opened: list = []

    def _open(name: str = "kyc_tests"):
        conn = psycopg.connect(
            schema, autocommit=True, connect_timeout=5, application_name=name
        )
        opened.append(conn)
        return conn

    yield _open

    for conn in opened:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — closing a broken connection is fine
            pass


# ---------------------------------------------------------------------------
# Building the things under test
# ---------------------------------------------------------------------------


@pytest.fixture
def make_application(db):
    """Create an application, committed, and return its id.

    Committed rather than rolled back, so a second connection can see it.
    """

    def _make(status: str = "started", **overrides) -> str:
        fields = {
            "full_name": "Integration Test",
            "date_of_birth": "1990-01-01",
            "address_line1": "1 Test Street",
            "address_city": "Testville",
            "address_postcode": "T1 1TT",
            "address_country": "GB",
            **overrides,
        }
        with db.cursor() as cur:
            cur.execute(
                """
                insert into applications
                    (status, full_name, date_of_birth, address_line1,
                     address_city, address_postcode, address_country)
                values ('started', %(full_name)s, %(date_of_birth)s,
                        %(address_line1)s, %(address_city)s, %(address_postcode)s,
                        %(address_country)s)
                returning id
                """,
                fields,
            )
            application_id = cur.fetchone()[0]

        # Walk to the requested status using only legal transitions — the
        # trigger from migration 017 would refuse anything else, which is the
        # point of it.
        route = {
            "started": [],
            "submitted": ["submitted"],
            "checking": ["checking"],
            "screening": ["screening"],
            "decided": ["screening", "decided"],
        }[status]
        for step in route:
            with db.cursor() as cur:
                cur.execute(
                    "update applications set status = %s where id = %s",
                    (step, application_id),
                )
        return str(application_id)

    return _make


@pytest.fixture
def audit_watermark(db):
    """The audit log's high-water mark, for scoping a count to one test.

    THE RULE, because it has bitten once and the failure mode is quiet.

    `jobs` can be deleted, so a test that needs a clean slate deletes first and
    its counts mean what they say. `audit_events` CANNOT — it rejects DELETE by
    design, and that rule binds the test suite too. So a test that counts audit
    rows is counting every row every earlier test produced, and is correct only
    for as long as it happens to be the only thing producing them.

    The 500-job concurrency proof was written that way. It was right for
    months, then test_drain.py arrived, ran eighty demo.noop jobs of its own
    first, and the count came to 581. It failed loudly that time. It could just
    as easily have drifted into passing for the wrong reason.

    So: record this first, then count only `id > watermark`.
    """

    def _mark() -> int:
        with db.cursor() as cur:
            cur.execute("select coalesce(max(id), 0) from audit_events")
            return cur.fetchone()[0]

    return _mark


@pytest.fixture
def count_rows(db):
    """Count rows matching a condition, for before/after assertions."""

    def _count(table: str, where: str = "true", params: tuple = ()) -> int:
        with db.cursor() as cur:
            cur.execute(f"select count(*) from {table} where {where}", params)  # noqa: S608
            return cur.fetchone()[0]

    return _count
