#!/usr/bin/env python
"""Migration runner for the shared schema.

Applies the numbered .sql files in db/migrations that have not run yet, in
order, and records what it did in a schema_migrations table.

    python db/migrate.py status
    python db/migrate.py up --dry-run
    python db/migrate.py up

This script belongs to /db, not to either service. It deliberately re-declares
its own DATABASE_URL loading rather than importing worker/db.py: if it imported
from the worker, the shared schema would start depending on one of the two
services, which is the exact thing /db exists to prevent. A few duplicated
lines are a cheap price for that.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
MIGRATIONS_DIR = HERE / "migrations"
REPO_ROOT = HERE.parent

load_dotenv(REPO_ROOT / ".env")

# Arbitrary but fixed. Any number works; it just has to be the same in every
# process that runs migrations.
ADVISORY_LOCK_KEY = 4_727_001

FILENAME_PATTERN = re.compile(r"^(\d+)_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: str
    filename: str
    path: Path
    checksum: str
    sql: str


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set. Copy .env.example to .env at the repo root."
        )
    return url


def checksum_of(raw: bytes) -> str:
    """Hash a migration file's contents.

    Line endings are normalised first. Git can check the same file out with
    CRLF on Windows and LF elsewhere, and without this the identical migration
    would hash differently on two machines — which would look exactly like
    someone had edited an already-applied file.
    """
    normalised = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(normalised).hexdigest()


def discover() -> list[Migration]:
    if not MIGRATIONS_DIR.is_dir():
        raise SystemExit(f"No migrations directory at {MIGRATIONS_DIR}")

    found: list[Migration] = []
    seen_versions: dict[str, str] = {}

    for path in sorted(MIGRATIONS_DIR.iterdir()):
        if path.suffix != ".sql":
            continue
        match = FILENAME_PATTERN.match(path.name)
        if not match:
            raise SystemExit(
                f"Migration filename not understood: {path.name}\n"
                "Expected NNN_lower_snake_case.sql, e.g. 007_add_thing.sql"
            )
        version = match.group(1)
        if version in seen_versions:
            raise SystemExit(
                f"Two migrations share version {version}: "
                f"{seen_versions[version]} and {path.name}"
            )
        seen_versions[version] = path.name

        raw = path.read_bytes()
        found.append(
            Migration(
                version=version,
                filename=path.name,
                path=path,
                checksum=checksum_of(raw),
                sql=raw.decode("utf-8"),
            )
        )

    return found


def ensure_bookkeeping_table(conn: psycopg.Connection) -> None:
    """Create schema_migrations if it does not exist.

    This one table is created by the runner rather than by a migration, for the
    obvious reason: the runner needs somewhere to record that migration 001 ran
    before it can run 001.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            create table if not exists schema_migrations (
                version    text primary key,
                filename   text        not null,
                checksum   text        not null,
                applied_at timestamptz not null default now()
            )
            """
        )
    conn.commit()


def applied_migrations(conn: psycopg.Connection) -> dict[str, tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("select version, filename, checksum from schema_migrations")
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def verify_checksums(
    migrations: list[Migration], applied: dict[str, tuple[str, str]]
) -> None:
    """Refuse to continue if an already-applied file has been edited.

    This footgun bites everyone once: you tweak 003.sql, it has already run, so
    the runner skips it — and now the database and the repository silently
    disagree about what the schema is. Editing an applied migration is never
    the fix; adding a new one is.
    """
    problems: list[str] = []
    for migration in migrations:
        record = applied.get(migration.version)
        if record is None:
            continue
        _, applied_checksum = record
        if applied_checksum != migration.checksum:
            problems.append(
                f"  {migration.filename} has changed since it was applied\n"
                f"    applied: {applied_checksum[:16]}...\n"
                f"    on disk: {migration.checksum[:16]}..."
            )

    known_versions = {m.version for m in migrations}
    for version, (filename, _) in sorted(applied.items()):
        if version not in known_versions:
            problems.append(
                f"  {filename} is recorded as applied but is missing from "
                f"{MIGRATIONS_DIR.name}/"
            )

    if problems:
        raise SystemExit(
            "Migration history does not match the files on disk:\n"
            + "\n".join(problems)
            + "\n\nAdd a new migration rather than editing an applied one."
        )


def cmd_status(conn: psycopg.Connection) -> int:
    migrations = discover()
    ensure_bookkeeping_table(conn)
    applied = applied_migrations(conn)

    if not migrations:
        print("No migrations found.")
        return 0

    print(f"{'':2} {'VERSION':<9} {'STATUS':<9} FILE")
    pending = 0
    for migration in migrations:
        is_applied = migration.version in applied
        if not is_applied:
            pending += 1
        mark = "*" if not is_applied else " "
        status = "applied" if is_applied else "PENDING"
        print(f"{mark:2} {migration.version:<9} {status:<9} {migration.filename}")

    print()
    print(f"{len(migrations) - pending} applied, {pending} pending.")
    verify_checksums(migrations, applied)
    return 0


def cmd_up(conn: psycopg.Connection, dry_run: bool) -> int:
    migrations = discover()
    ensure_bookkeeping_table(conn)

    # Stop two runners racing. Irrelevant on a laptop; essential on a platform
    # that might start two instances of a deploy at once. The lock is released
    # when the connection closes.
    with conn.cursor() as cur:
        cur.execute("select pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    conn.commit()

    applied = applied_migrations(conn)
    verify_checksums(migrations, applied)

    pending = [m for m in migrations if m.version not in applied]
    if not pending:
        print("Nothing to do — the database is up to date.")
        return 0

    if dry_run:
        print(f"Would apply {len(pending)} migration(s):")
        for migration in pending:
            print(f"  {migration.filename}")
        print("\n(dry run — nothing was changed)")
        return 0

    for migration in pending:
        print(f"applying {migration.filename} ... ", end="", flush=True)
        try:
            # Postgres has TRANSACTIONAL DDL: CREATE TABLE, ALTER and INSERT
            # can share one transaction and roll back together. Many databases
            # cannot do this. It is why a migration here either applies
            # completely or not at all — never half.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(migration.sql)
                    cur.execute(
                        """
                        insert into schema_migrations (version, filename, checksum)
                        values (%s, %s, %s)
                        """,
                        (migration.version, migration.filename, migration.checksum),
                    )
        except psycopg.Error as err:
            print("FAILED")
            print(f"\n{migration.filename} was rolled back. Nothing was applied.\n")
            print(f"{err.__class__.__name__}: {err}")
            if err.diag.sqlstate:
                print(f"SQLSTATE: {err.diag.sqlstate}")
            return 1
        print("ok")

    print(f"\nApplied {len(pending)} migration(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate.py", description="Apply the shared SQL migrations."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show which migrations have been applied")
    up = sub.add_parser("up", help="apply pending migrations")
    up.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be applied without applying it",
    )
    args = parser.parse_args(argv)

    try:
        with psycopg.connect(
            database_url(), connect_timeout=5, application_name="kyc_migrate"
        ) as conn:
            if args.command == "status":
                return cmd_status(conn)
            return cmd_up(conn, dry_run=args.dry_run)
    except psycopg.OperationalError as err:
        print(f"Could not connect to the database:\n{err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
