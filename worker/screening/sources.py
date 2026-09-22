"""Where the list comes from.

Three sources, one interface, chosen by SANCTIONS_SOURCE. The matching code
never learns which one it got — the same adapter discipline as the vendor
client in Phase 4.

ON LICENSING, because it is a real constraint and not a footnote:

  synthetic  Fabricated, committed to the repository. Makes `git clone &&
             pytest` work offline with no downloads and no licence at all.
             Never to be used as a sanctions list.

  ofac       The US Treasury's Specially Designated Nationals list. A work of
             the US Government and therefore public domain under 17 U.S.C. 105:
             free to use, modify and redistribute, no conditions. Downloaded by
             scripts/download_ofac.py into a gitignored directory.

  opensanctions  Broader coverage and better structure, and the obvious choice
             if you can use it — but it is CC-BY-NC. Commercial use requires a
             paid licence. Supported here, deliberately not shipped, and not
             the default.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from .index import ListEntry, SanctionsIndex, build_index
from .normalise import normalise_name

log = logging.getLogger("worker.screening.sources")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

SYNTHETIC_PATH = DATA_DIR / "synthetic_sanctions.json"
OFAC_PATH = DATA_DIR / "ofac_sdn.json"
OPENSANCTIONS_PATH = DATA_DIR / "opensanctions.json"


def _file_identity(path: Path) -> tuple[str, str | None]:
    """The hash of a list file, and the publisher's date if it carries one.

    Hashing the FILE rather than the parsed entries, so anyone holding the same
    file can recompute this and confirm what was screened against. Parsed
    entries would depend on our parser, which makes the hash a statement about
    our code rather than about the vendor's data.
    """
    raw = path.read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()

    published_at: str | None = None
    try:
        payload: Any = json.loads(raw)
        if isinstance(payload, dict):
            published_at = payload.get("published_at")
    except json.JSONDecodeError:
        # A line-delimited export (OpenSanctions) is not one JSON object. The
        # hash still works, which is the part that matters.
        pass

    return content_hash, published_at


def _entry_from_json(raw: dict) -> ListEntry:
    return ListEntry(
        entity_id=str(raw["entity_id"]),
        name=str(raw["name"]),
        aliases=tuple(raw.get("aliases") or ()),
        list_name=str(raw.get("list_name") or "unknown"),
        entity_type=str(raw.get("entity_type") or "sanctions"),
        countries=tuple(raw.get("countries") or ()),
        date_of_birth=raw.get("date_of_birth"),
    )


def load_synthetic(path: Path = SYNTHETIC_PATH) -> SanctionsIndex:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = [_entry_from_json(raw) for raw in payload["entries"]]
    content_hash, published_at = _file_identity(path)
    return build_index(
        entries, source="synthetic", content_hash=content_hash, published_at=published_at
    )


def load_ofac(path: Path = OFAC_PATH) -> SanctionsIndex:
    """The OFAC SDN list, as written by scripts/download_ofac.py."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing. Run: python scripts/download_ofac.py\n"
            "Or set SANCTIONS_SOURCE=synthetic to use the committed fixture."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = [_entry_from_json(raw) for raw in payload["entries"]]
    content_hash, published_at = _file_identity(path)
    return build_index(
        entries, source="ofac", content_hash=content_hash, published_at=published_at
    )


def load_opensanctions(path: Path = OPENSANCTIONS_PATH) -> SanctionsIndex:
    """OpenSanctions' 'targets.simple.json' export, one JSON object per line.

    Not downloaded automatically. The data is CC-BY-NC and commercial use needs
    a licence, so obtaining it is a decision for whoever runs this, not
    something the code should quietly do on their behalf.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing.\n"
            "OpenSanctions data is CC-BY-NC: free for non-commercial use, "
            "licensed for commercial. See https://www.opensanctions.org/licensing/\n"
            "Download their targets.simple.json export to this path yourself."
        )

    entries: list[ListEntry] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            topics = raw.get("topics") or []
            entity_type = "pep" if "role.pep" in topics else "sanctions"
            birth_dates = raw.get("birth_date") or []
            entries.append(
                ListEntry(
                    entity_id=str(raw.get("id")),
                    name=str(raw.get("caption") or ""),
                    aliases=tuple(raw.get("alias") or ()),
                    list_name=", ".join(raw.get("datasets") or []) or "opensanctions",
                    entity_type=entity_type,
                    countries=tuple(raw.get("countries") or ()),
                    date_of_birth=birth_dates[0] if birth_dates else None,
                )
            )
    content_hash, published_at = _file_identity(path)
    return build_index(
        entries,
        source="opensanctions",
        content_hash=content_hash,
        published_at=published_at,
    )


_LOADERS = {
    "synthetic": load_synthetic,
    "ofac": load_ofac,
    "opensanctions": load_opensanctions,
}

#: Built once per process. The list does not change while a worker runs, and
#: normalising twenty thousand entries on every job would dominate the runtime.
_cached: SanctionsIndex | None = None

#: Set by preload(). Only used to make an unexpected load LOUD — see below.
_preloaded = False


def load_index(source: str) -> SanctionsIndex:
    global _cached
    if _cached is not None and _cached.source == source:
        return _cached

    if _preloaded:
        # A cache miss after boot means something changed the source mid-process
        # or cleared the cache. Neither should be possible in a worker, and the
        # consequence is a multi-second load INSIDE a claimed job — the thing
        # preloading exists to prevent. Not fatal, because a screening that runs
        # slowly beats one that does not run, but it must not pass silently.
        log.warning(
            "cold-loading the %r list after boot — a job is about to block on "
            "this. The preloaded list was %r.",
            source,
            _cached.source if _cached else None,
        )

    try:
        loader = _LOADERS[source]
    except KeyError:
        raise ValueError(
            f"unknown SANCTIONS_SOURCE {source!r}; expected one of {sorted(_LOADERS)}"
        ) from None

    _cached = loader()
    log.info(
        "loaded %d entries from the %s list (published %s, sha256 %s)",
        len(_cached),
        source,
        _cached.published_at or "unknown",
        _cached.content_hash[:12],
    )
    return _cached


def ensure_available(source: str) -> None:
    """Fetch the list file if the image does not already carry it.

    Only OFAC needs this. The synthetic fixture is committed, and OpenSanctions
    is deliberately never downloaded automatically because it is CC-BY-NC and
    obtaining it is a licensing decision for whoever runs this.

    OFAC is gitignored — 5.7MB of data that goes stale, which does not belong in
    git — so a container image does not contain it and a deployed worker has to
    go and get it. Downloading at BOOT rather than at build time also means a
    restarted dyno picks up a newer list without a redeploy, which is a small
    dent in the staleness limitation rather than a fix for it: nothing refreshes
    while a worker is up.

    Failures are swallowed here and handled by the caller's fallback. A worker
    that cannot reach treasury.gov should still start and still screen — against
    a list it names honestly — rather than crash-looping.
    """
    if source != "ofac" or OFAC_PATH.exists():
        return

    # The parser lives in scripts/download_ofac.py and is shared rather than
    # duplicated. The repository root is on the path because /data and /scripts
    # belong to neither service — the same reason /db migrations do.
    import sys

    root = str(DATA_DIR.parent)
    if root not in sys.path:
        sys.path.insert(0, root)

    log.info("no local OFAC list; downloading it before claiming any work")
    from scripts.download_ofac import main as download_ofac

    if download_ofac() != 0:
        raise RuntimeError("the OFAC download did not produce a usable list")


def preload(source: str, *, fallback: str | None = None) -> SanctionsIndex:
    """Load the list at worker boot, before any job is claimed.

    WHY THIS IS NOT JUST AN OPTIMISATION

    Loading inside `run_screening` put a multi-second, multi-megabyte operation
    inside a CLAIMED job. That job is already marked 'running' with a
    `locked_at` timestamp, so its load time counts against JOB_STALE_SECONDS —
    the threshold at which the reaper presumes the worker dead and hands the job
    to someone else. Slow disk, a cold page cache or a larger list all push that
    the wrong way, and the failure it produces is the queue's worst one: the
    same job running twice.

    Boot is the right place because nothing is claimed yet. There is no job to
    lose, no lock to expire, and a missing or corrupt list file kills the
    process immediately and visibly instead of parking jobs one at a time with
    a stack trace nobody reads.

    THE FALLBACK

    With no `fallback`, a failure to load propagates and the worker does not
    start. That is the right default: a worker that cannot load its list would
    claim screening jobs and fail every one, and a crash loop is far easier to
    notice than a worker quietly parking everything it touches.

    Production passes `fallback="synthetic"`, which trades that for
    availability: if treasury.gov is unreachable the worker still starts and
    still screens, against the committed fixture. That is a REAL DEGRADATION and
    is treated as one — logged at ERROR, not INFO, because screening against a
    25-entry invented list is very close to not screening at all. It is
    survivable here only because this is a demo with synthetic applicants.

    It is not silent in the data either: every match written while degraded
    points at a `sanctions_snapshots` row whose source reads 'synthetic', so a
    case decided during the outage says so on the case view and in the audit
    trail. Nothing has to be inferred from a log that has since rotated away.
    """
    global _preloaded
    try:
        ensure_available(source)
        index = load_index(source)
    except Exception as err:  # noqa: BLE001 — any failure means try the fallback
        if not fallback or fallback == source:
            raise
        log.error(
            "DEGRADED: could not load the %r sanctions list (%s). Falling back "
            "to %r. Screening is running against a list that is not a sanctions "
            "list; every match recorded until this worker restarts will say so.",
            source,
            err,
            fallback,
        )
        index = load_index(fallback)

    _preloaded = True
    return index


def ensure_snapshot(conn, index: SanctionsIndex) -> int:
    """Record which list version this is, and return its id.

    Called by the screening handler rather than by the downloader, deliberately.
    The downloader only ever sees OFAC; the synthetic fixture is never
    downloaded at all, and a file copied in by hand is downloaded by nothing.
    Registering at LOAD time means every list the worker actually screens
    against has a row, whatever its provenance — and it keeps DATABASE_URL out
    of a standalone script.

    ON CONFLICT DO NOTHING against the unique (source, content_hash): identical
    content is the same snapshot however many times it is loaded, so restarting
    a worker must not create a new row and make it look as though the list
    changed.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into sanctions_snapshots
                (source, published_at, content_hash, record_count)
            values (%s, %s, %s, %s)
            on conflict (source, content_hash) do nothing
            returning id
            """,
            (index.source, index.published_at, index.content_hash, len(index)),
        )
        row = cur.fetchone()
        if row is not None:
            log.info(
                "registered sanctions snapshot %s: %s published %s, %d entries",
                row[0],
                index.source,
                index.published_at or "unknown",
                len(index),
            )
            return row[0]

        # Already registered. Read back the id we conflicted with.
        cur.execute(
            "select id from sanctions_snapshots where source = %s and content_hash = %s",
            (index.source, index.content_hash),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Loading the list INTO Postgres
#
# The other half of the move out of memory. `ensure_snapshot` above registers
# which version of a list was used; this writes the list itself, so the worker
# no longer has to hold it.
# ---------------------------------------------------------------------------


def load_into_postgres(conn, index: SanctionsIndex, *, batch: int = 1000) -> int:
    """Write a loaded index's entries into the database, once.

    Idempotent by snapshot: if the snapshot already has `entries_loaded_at`
    set, this does nothing and says so. Re-running a downloader, or starting a
    second worker, must not double the list — and `on conflict` on
    (snapshot_id, entity_id) makes a partial re-run converge rather than
    duplicate.

    ORDER MATTERS. Rows are written first and `entries_loaded_at` is stamped
    last, in the same transaction. A load that dies halfway therefore leaves a
    snapshot that the matcher will refuse to use, rather than one that silently
    under-matches. Stamping first would invert that, and an under-matching
    sanctions screen is the failure that looks like success.
    """
    snapshot_id = ensure_snapshot(conn, index)

    with conn.cursor() as cur:
        cur.execute(
            "select entries_loaded_at is not null from sanctions_snapshots where id = %s",
            (snapshot_id,),
        )
        already = cur.fetchone()[0]
    if already:
        log.info("snapshot %s already loaded — nothing to write", snapshot_id)
        return snapshot_id

    log.info("writing %d entries into snapshot %s", len(index), snapshot_id)

    with conn.transaction():
        with conn.cursor() as cur:
            for start in range(0, len(index.entries), batch):
                chunk = index.entries[start : start + batch]
                for entry in chunk:
                    cur.execute(
                        """
                        insert into sanctions_entries
                            (snapshot_id, entity_id, name, entity_type,
                             list_name, countries, date_of_birth)
                        values (%s, %s, %s, %s, %s, %s, %s)
                        on conflict (snapshot_id, entity_id) do nothing
                        returning id
                        """,
                        (
                            snapshot_id,
                            entry.entity_id,
                            entry.name,
                            entry.entity_type,
                            entry.list_name,
                            list(entry.countries),
                            entry.date_of_birth,
                        ),
                    )
                    row = cur.fetchone()
                    if row is None:
                        # Already present from a partial earlier run.
                        continue
                    entry_pk = row[0]

                    for position, spelling in enumerate((entry.name, *entry.aliases)):
                        normalised = normalise_name(spelling)
                        if not normalised:
                            continue
                        cur.execute(
                            """
                            insert into sanctions_names
                                (entry_id, spelling, normalised, is_primary)
                            values (%s, %s, %s, %s)
                            returning id
                            """,
                            (entry_pk, spelling, normalised, position == 0),
                        )
                        name_pk = cur.fetchone()[0]

                        # One row per token. Duplicates within a spelling are
                        # dropped — "Ali Ali Hassan" gains nothing from two
                        # identical index entries.
                        for token in sorted(set(normalised.split())):
                            cur.execute(
                                "insert into sanctions_name_tokens "
                                "(name_id, entry_id, token) values (%s, %s, %s)",
                                (name_pk, entry_pk, token),
                            )

            # Last, and inside the same transaction.
            cur.execute(
                "update sanctions_snapshots set entries_loaded_at = now() where id = %s",
                (snapshot_id,),
            )

    log.info("snapshot %s loaded and marked searchable", snapshot_id)
    return snapshot_id
