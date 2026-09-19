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


def load_index(source: str) -> SanctionsIndex:
    global _cached
    if _cached is not None and _cached.source == source:
        return _cached

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
