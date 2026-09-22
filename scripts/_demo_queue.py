#!/usr/bin/env python
"""Database steps for the two queue demos. Not run directly — see the .ps1s.

Split this way on purpose: PowerShell spawns and waits on the two worker
processes, which is what it is good at, and this does the SQL and the
formatting, which it is not.

NOTHING IS DELETED FROM audit_events. It refuses DELETE, and that rule binds
the demo scripts as much as anything else — so instead of clearing the log,
each run records a watermark id first and counts only rows above it. Repeated
takes on camera therefore stay independent without touching history.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))

import psycopg  # noqa: E402

from db import database_url  # noqa: E402

WIDTH = 66
RULE = "=" * WIDTH


def connect():
    return psycopg.connect(database_url(), autocommit=True, connect_timeout=5)


def cmd_reset(_args) -> int:
    """Clear the queue. `jobs` is ordinary state — only audit_events is sacred."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("delete from jobs")
    return 0


def cmd_watermark(_args) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select coalesce(max(id), 0) from audit_events")
        print(cur.fetchone()[0])
    return 0


def cmd_enqueue(args) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into jobs (job_type, payload)
            select 'demo.noop', jsonb_build_object('n', g)
              from generate_series(1, %s) g
            """,
            (args.count,),
        )
    return 0


def cmd_report(args) -> int:
    """The only thing on screen. Everything above it is plumbing."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select (details->>'job_id')::bigint as job_id,
                   details->>'worker'           as worker,
                   occurred_at
              from audit_events
             where id > %s
               and action = 'job.executed'
               and details->>'job_type' = 'demo.noop'
             order by occurred_at
            """,
            (args.since,),
        )
        rows = cur.fetchall()

    executed = len(rows)
    distinct = len({r[0] for r in rows})
    duplicates = executed - distinct

    by_worker: dict[str, int] = {}
    for _, worker, _ts in rows:
        by_worker[worker] = by_worker.get(worker, 0) + 1

    print(f"  jobs enqueued     {args.count:>6}")
    print(f"  executions        {executed:>6}")
    print(f"  distinct jobs     {distinct:>6}")
    print()

    if duplicates:
        print(f"  DUPLICATES        {duplicates:>6}   <-- the same work, done twice")
    else:
        print(f"  DUPLICATES        {duplicates:>6}   <-- none. Not one.")
    print()

    share = "   ".join(f"{w} {c}" for w, c in sorted(by_worker.items()))
    print(f"  split             {share}")

    if duplicates:
        # The two rows that make it concrete. One example, not a list —
        # thirteen of these would scroll the headline off screen.
        seen: dict[int, list] = {}
        for job_id, worker, ts in rows:
            seen.setdefault(job_id, []).append((worker, ts))
        example = next(
            (jid, hits) for jid, hits in seen.items() if len(hits) > 1
        )
        job_id, hits = example
        hits.sort(key=lambda h: h[1])
        gap_ms = (hits[1][1] - hits[0][1]).total_seconds() * 1000
        print()
        print(f"  job {job_id} was executed by BOTH workers:")
        for worker, ts in hits[:2]:
            print(f"    {ts.strftime('%H:%M:%S.%f')[:-3]}   {worker}")
        print(f"    {gap_ms:.0f} ms apart")

    print(RULE)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("reset").set_defaults(func=cmd_reset)
    sub.add_parser("watermark").set_defaults(func=cmd_watermark)

    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--count", type=int, required=True)
    enqueue.set_defaults(func=cmd_enqueue)

    report = sub.add_parser("report")
    report.add_argument("--since", type=int, required=True)
    report.add_argument("--count", type=int, required=True)
    report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
