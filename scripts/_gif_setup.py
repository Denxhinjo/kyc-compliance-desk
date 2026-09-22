#!/usr/bin/env python
"""Database setup for record-desk.ps1. Not run directly.

Exists because the alternative was PowerShell here-strings containing Python
containing SQL containing quotes, which broke on the first run and would have
broken again on the next edit.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "worker"))

import psycopg  # noqa: E402

from db import database_url  # noqa: E402

DB_NAME = "kyc_gif"


def url_for(name: str) -> str:
    parsed = urlparse(database_url())
    return urlunparse(parsed._replace(path=f"/{name}"))


def cmd_url(_args) -> int:
    print(url_for(DB_NAME))
    return 0


def cmd_exists(_args) -> int:
    with psycopg.connect(url_for("postgres"), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("select 1 from pg_database where datname = %s", (DB_NAME,))
            print("yes" if cur.fetchone() else "no")
    return 0


def cmd_build(args) -> int:
    """Drop, recreate, migrate and seed the throwaway database."""
    with psycopg.connect(url_for("postgres"), autocommit=True) as conn:
        with conn.cursor() as cur:
            # Anything still connected would block the drop.
            cur.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = %s and pid <> pg_backend_pid()",
                (DB_NAME,),
            )
            cur.execute(f'drop database if exists "{DB_NAME}"')
            cur.execute(f'create database "{DB_NAME}"')

    env = {**_environ(), "DATABASE_URL": url_for(DB_NAME)}
    run = subprocess.run(
        [sys.executable, str(ROOT / "db" / "migrate.py"), "up"],
        env=env, capture_output=True, text=True,
    )
    if run.returncode != 0:
        print(run.stdout + run.stderr, file=sys.stderr)
        return 1

    run = subprocess.run(
        [sys.executable, "seed.py", "--count", str(args.count),
         "--weeks", "3", "--seed", "31337"],
        cwd=str(ROOT / "worker"), env=env, capture_output=True, text=True,
    )
    if run.returncode != 0:
        print(run.stdout + run.stderr, file=sys.stderr)
        return 1
    return 0


def _environ() -> dict:
    import os

    return dict(os.environ)


def cmd_pick(_args) -> int:
    """A pending case with a real sanctions match — the one worth filming."""
    with psycopg.connect(url_for(DB_NAME), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select a.id, a.full_name, a.risk_score
                  from applications a
                  join decisions r
                    on r.application_id = a.id and r.outcome = 'referred'
                 where a.status = 'screening'
                   and exists (select 1 from screening_results s
                               where s.application_id = a.id)
                   and not exists (select 1 from decisions t
                                   where t.application_id = a.id
                                     and t.outcome in ('approved','rejected'))
                 order by a.risk_score desc
                 limit 1
                """
            )
            row = cur.fetchone()
    if row is None:
        return 1
    print(json.dumps({"id": str(row[0]), "name": row[1], "score": row[2]}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("url").set_defaults(func=cmd_url)
    sub.add_parser("exists").set_defaults(func=cmd_exists)
    sub.add_parser("pick").set_defaults(func=cmd_pick)
    build = sub.add_parser("build")
    build.add_argument("--count", type=int, default=260)
    build.set_defaults(func=cmd_build)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
