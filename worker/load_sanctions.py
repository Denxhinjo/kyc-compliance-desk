#!/usr/bin/env python
"""Put the sanctions list into Postgres. Run once per list version.

    python load_sanctions.py              # whatever SANCTIONS_SOURCE says
    python load_sanctions.py --source ofac

Downloads the list if it is missing, parses it, writes the rows, and registers
the snapshot. Idempotent: a version already loaded is left alone, so this is
safe in a deploy script and safe to run twice.

This replaces what the worker used to do at boot. Holding the list in memory
was a property of the worker; being in the database is a property of the
SYSTEM, so loading it is a deployment step rather than something every process
repeats on startup.
"""

from __future__ import annotations

import argparse
import logging
import sys

from config import SANCTIONS_SOURCE
from db import connect
from screening.sources import ensure_available, load_index, load_into_postgres
from screening.store import active_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SANCTIONS_SOURCE)
    parser.add_argument(
        "--force",
        action="store_true",
        help="load even if this version is already marked loaded",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    ensure_available(args.source)
    index = load_index(args.source)

    with connect() as conn:
        if args.force:
            with conn.cursor() as cur:
                cur.execute(
                    "update sanctions_snapshots set entries_loaded_at = null "
                    "where source = %s and content_hash = %s",
                    (index.source, index.content_hash),
                )
        snapshot_id = load_into_postgres(conn, index)
        snapshot = active_snapshot(conn, args.source)

    if snapshot is None:
        print("load finished but no snapshot is searchable — that is a bug",
              file=sys.stderr)
        return 1

    print(
        f"\n{args.source}: snapshot {snapshot_id} searchable — "
        f"{snapshot.record_count:,} entries, published "
        f"{snapshot.published_at or 'unknown'}, sha256 {snapshot.content_hash[:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
