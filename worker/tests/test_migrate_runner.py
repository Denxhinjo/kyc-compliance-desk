"""Tests for `db/migrate.py --only` — applying one migration at a time.

WHY THIS OPTION EXISTS, AND WHY IT NEEDS TESTS

`migrate.py up` applies every pending migration in numeric order. For migrations
020-025 that is the wrong order: they have to reach a deployed database as 021,
024, 025, then a code deploy, then 022, 023, 020, because three of them start
rejecting writes the currently-deployed code makes and one adds a foreign key
the new code depends on. See docs/deploy.md.

So `--only` exists to apply exactly one. It was written during a rollout
rehearsal, when plain `up` turned out to make the runbook unexecutable — and a
rollout tool that is used once, under pressure, on the live database, is exactly
the kind of thing that should not be trusted on the strength of having worked
once.

HOW THESE RUN

Each test gets its own empty database, migrates part of it, and drops it. They
are slower than the rest of the suite and unavoidably so: the behaviour under
test is "what does the runner do to a database with pending migrations", and
every other database in this project is already fully migrated.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATE = REPO_ROOT / "db" / "migrate.py"


def _admin_url(url: str) -> str:
    """The same server, pointed at `postgres`, so we can create and drop."""
    return re.sub(r"/[^/?]+(\?|$)", r"/postgres\1", url)


def _named(url: str, name: str) -> str:
    return re.sub(r"/[^/?]+(\?|$)", rf"/{name}\1", url)


@pytest.fixture
def empty_database():
    """A fresh database with no migrations applied, dropped afterwards."""
    base = os.environ.get("DATABASE_URL")
    if not base:
        pytest.skip("DATABASE_URL is not set")

    name = f"kyc_migrate_{uuid.uuid4().hex[:10]}"
    try:
        with psycopg.connect(_admin_url(base), autocommit=True, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(f"create database {name}")
    except Exception as err:  # noqa: BLE001 — unreachable server means skip
        pytest.skip(f"cannot create a scratch database: {err}")

    url = _named(base, name)
    try:
        yield url
    finally:
        with psycopg.connect(_admin_url(base), autocommit=True, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                # Terminate anything still attached, or the drop blocks.
                cur.execute(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname = %s and pid <> pg_backend_pid()",
                    (name,),
                )
                cur.execute(f"drop database if exists {name}")


def run_migrate(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MIGRATE), "up", *args],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def applied(url: str) -> list[str]:
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("select version from schema_migrations order by version")
            return [r[0] for r in cur.fetchall()]


def test_only_applies_exactly_one_migration(empty_database):
    """The headline behaviour.

    001 is chosen because it has no predecessors; the point is the COUNT, not
    which one. If this ever applies two, a rollout that depends on a code deploy
    happening between them silently loses that gap.
    """
    result = run_migrate(empty_database, "--only", "001")
    assert result.returncode == 0, result.stdout + result.stderr

    assert applied(empty_database) == ["001"], (
        "expected exactly one migration applied; the whole point of --only is "
        "that the next one does NOT follow automatically"
    )


def test_only_records_the_checksum_like_any_other_migration(empty_database):
    """A migration applied this way must still be bookkept properly.

    If --only skipped the checksum record, a database migrated during a rollout
    would stop agreeing with the repository — which is the failure the checksum
    exists to catch, reintroduced by the tool meant to make rollouts safer.
    """
    run_migrate(empty_database, "--only", "001")

    with psycopg.connect(empty_database, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select filename, checksum from schema_migrations where version = '001'"
            )
            filename, checksum = cur.fetchone()

    assert filename.startswith("001_")
    assert checksum and len(checksum) == 64, "expected a sha256 of the file"

    # And it must match the file on disk, which is what verify_checksums reads.
    sys.path.insert(0, str(REPO_ROOT / "db"))
    from migrate import checksum_of  # noqa: E402

    on_disk = checksum_of((REPO_ROOT / "db" / "migrations" / filename).read_bytes())
    assert checksum == on_disk


def test_only_refuses_an_unknown_version(empty_database):
    """A typo must not silently do nothing, or do something else."""
    result = run_migrate(empty_database, "--only", "999")

    assert result.returncode == 1, "an unknown version must be an error, not a no-op"
    assert "No migration" in result.stdout
    assert applied(empty_database) == [], "nothing may be applied on a bad argument"


def test_only_is_a_no_op_on_an_already_applied_version(empty_database):
    """Re-running a step must be safe.

    Rollouts get interrupted and resumed, and the person resuming may not know
    exactly where they stopped. Re-applying is the obvious thing to try, so it
    had better be harmless rather than an error that sends them looking for a
    problem that is not there.
    """
    run_migrate(empty_database, "--only", "001")
    result = run_migrate(empty_database, "--only", "001")

    assert result.returncode == 0
    assert "already applied" in result.stdout
    assert applied(empty_database) == ["001"]


def test_only_can_skip_ahead_out_of_numeric_order(empty_database):
    """The actual reason this exists.

    The rollout applies 021 before 024 and both before 022 — not numeric order.
    This asserts the runner permits that, by applying a later migration while an
    earlier one is still pending.

    Uses 001 then 003: 002 stays pending in between, which numeric order would
    never allow.
    """
    run_migrate(empty_database, "--only", "001")
    result = run_migrate(empty_database, "--only", "003")
    assert result.returncode == 0, result.stdout + result.stderr

    assert applied(empty_database) == ["001", "003"], (
        "--only must apply the named migration even with an earlier one pending; "
        "if it refuses, the documented rollout order cannot be carried out"
    )


def test_without_only_every_pending_migration_is_applied(empty_database):
    """The counterweight.

    --only must be opt-in. If plain `up` had quietly become one-at-a-time, every
    ordinary deploy would stop half-done and nobody would notice until something
    referenced a table that was not there yet.
    """
    result = run_migrate(empty_database)
    assert result.returncode == 0, result.stdout + result.stderr

    versions = applied(empty_database)
    assert len(versions) > 20, f"expected the full set, got {len(versions)}"
    assert versions == sorted(versions)
