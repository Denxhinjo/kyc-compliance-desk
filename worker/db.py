"""Database access for the worker.

Deliberately thin. Everything is raw SQL against psycopg — the schema is shared
with the TypeScript service, so it must not be owned by either language's ORM.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

# The single source of truth lives at the repo root, one level above /worker.
REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env at the repo root."
        )
    return url


def connect() -> psycopg.Connection:
    """Open a new connection.

    No connection pool here, on purpose. The worker is a single loop doing one
    thing at a time, so it holds exactly one long-lived connection. /web needs
    a pool because it serves many requests concurrently; the worker does not.

    On autocommit=True — this is not laziness, it is the opposite:

    By default psycopg opens a transaction implicitly on the first statement
    and leaves it open until you commit. A `with conn.transaction():` block
    then finds a transaction already running, so instead of being a real
    transaction it becomes a mere SAVEPOINT inside it — and nothing is durable
    until someone calls commit(). One stray rollback() later, work that looked
    committed silently disappears.

    With autocommit=True there is no implicit transaction, so every
    `with conn.transaction():` block IS the outermost one and commits when it
    exits. The rule becomes simple and visible: anything that must be atomic
    goes inside an explicit transaction() block, and anything not in one is a
    single self-contained statement.
    """
    return psycopg.connect(
        database_url(),
        connect_timeout=5,
        autocommit=True,
        # Identifies this service in Postgres' pg_stat_activity, so you can see
        # at a glance which service a connection belongs to. Free, and
        # invaluable when something is holding a lock.
        application_name="kyc_worker",
    )


class Database:
    """Owns one connection and reopens it if it dies.

    A worker runs for days. Postgres restarts, networks blip, connections get
    closed by the other end. Rather than crash the process, the worker drops
    the dead connection and opens a new one on the next attempt.
    """

    def __init__(self) -> None:
        self._conn: psycopg.Connection | None = None

    def connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = connect()
        return self._conn

    def discard(self) -> None:
        """Throw away the current connection after an error."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def close(self) -> None:
        self.discard()

    def fetch_server_time(self) -> tuple[str, str, int]:
        """Ask Postgres for its clock, its name, and our backend process id.

        This is the worker's proof of connectivity: these values can only be
        printed if a query genuinely round-tripped to the database. The backend
        pid is the same one that appears in pg_stat_activity.
        """
        conn = self.connection()
        with conn.cursor() as cur:
            cur.execute("select now()::text, current_database(), pg_backend_pid()")
            row = cur.fetchone()
            assert row is not None
        # No commit needed: the connection is in autocommit mode, so this read
        # did not leave a transaction open. Before that change this had to
        # commit explicitly, or the connection would sit "idle in transaction"
        # between heartbeats — which blocks vacuum and can hold locks.
        return row[0], row[1], row[2]
