#!/usr/bin/env python
"""Download the OFAC SDN list into data/ofac_sdn.json.

    python scripts/download_ofac.py

WHY OFAC RATHER THAN OPENSANCTIONS

OpenSanctions is the better dataset — broader, better structured, PEPs included
— and it is what a real firm would license. But it is CC-BY-NC: commercial use
requires a paid licence (https://www.opensanctions.org/licensing/). A portfolio
project that anyone should be able to clone and run cannot ship it, and a repo
that quietly downloads NC-licensed data on first run is worse, not better.

The OFAC Specially Designated Nationals list is a work of the US Government and
therefore public domain under 17 U.S.C. 105: free to use, modify and
redistribute with no conditions. It is also the list that matters most — if you
match against SDN you are doing the thing with actual legal force behind it.

WHAT IT DOES NOT COVER

SDN is sanctions only. It carries no PEP data, so PEP screening against this
source finds nothing — the synthetic fixture exists partly so that path is still
exercised. It is also US-centric: the EU and UK maintain their own lists, and a
real firm screens against all of them.

The downloaded file is gitignored. Nothing here is committed.
"""

from __future__ import annotations

import json
import sys
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path

SDN_XML_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
OUTPUT = Path(__file__).resolve().parent.parent / "data" / "ofac_sdn.json"

def _namespace_of(root: ElementTree.Element) -> dict[str, str]:
    """Read the XML namespace off the document rather than hardcoding it.

    OFAC has already moved this once — the legacy exports used
    http://tempuri.org/sdnList.xsd and the current service uses its own URL.
    A hardcoded namespace does not fail loudly when that happens: findall()
    simply returns nothing and you get a cheerful "parsed 0 entries", which is
    exactly the shape of bug that reaches production wearing a green tick.
    """
    tag = root.tag
    return {"sdn": tag[1:].split("}")[0]} if tag.startswith("{") else {"sdn": ""}


def _text(node, path: str, ns: dict[str, str]) -> str | None:
    found = node.find(path, ns)
    return found.text.strip() if found is not None and found.text else None


def _full_name(first: str | None, last: str | None) -> str:
    return " ".join(part for part in (first, last) if part).strip()


def parse(xml_bytes: bytes) -> list[dict]:
    root = ElementTree.fromstring(xml_bytes)
    ns = _namespace_of(root)
    entries: list[dict] = []

    for entity in root.findall("sdn:sdnEntry", ns):
        uid = _text(entity, "sdn:uid", ns)
        if uid is None:
            continue

        name = _full_name(
            _text(entity, "sdn:firstName", ns), _text(entity, "sdn:lastName", ns)
        )
        # 'Individual' or 'Entity'. Both are kept — a firm screens companies as
        # well as people — but the distinction is recorded so a reviewer can see
        # that a match is against a shipping company rather than a person.
        sdn_type = _text(entity, "sdn:sdnType", ns) or "Unknown"
        if not name:
            continue

        # Aliases are the most valuable part of this file. A transliteration
        # pair like "Mohammed Al-Sayed" against "Muhammad Al Sayyid" scores 80
        # on string similarity — too weak for any threshold to act on. If the
        # list publishes both spellings, the match becomes exact and the
        # threshold never has to adjudicate.
        aliases: list[str] = []
        for alias in entity.findall("sdn:akaList/sdn:aka", ns):
            alias_name = _full_name(
                _text(alias, "sdn:firstName", ns), _text(alias, "sdn:lastName", ns)
            )
            if alias_name and alias_name != name:
                aliases.append(alias_name)

        countries: list[str] = []
        for address in entity.findall("sdn:addressList/sdn:address", ns):
            country = _text(address, "sdn:country", ns)
            if country and country not in countries:
                countries.append(country)

        date_of_birth = None
        for dob in entity.findall("sdn:dateOfBirthList/sdn:dateOfBirthItem", ns):
            raw = _text(dob, "sdn:dateOfBirth", ns)
            if raw:
                date_of_birth = raw
                break

        entries.append(
            {
                "entity_id": f"OFAC-{uid}",
                "name": name,
                "aliases": aliases,
                "list_name": "OFAC SDN",
                # SDN is a sanctions list. It contains no PEP designations.
                "entity_type": "sanctions",
                "countries": countries,
                "date_of_birth": date_of_birth,
                "sdn_type": sdn_type,
            }
        )

    return entries


def main() -> int:
    print(f"downloading {SDN_XML_URL}")
    try:
        with urllib.request.urlopen(SDN_XML_URL, timeout=120) as response:
            raw = response.read()
    except Exception as err:  # noqa: BLE001 — a script, and the message is the point
        print(f"download failed: {err}", file=sys.stderr)
        print(
            "\nOFAC occasionally moves these URLs. Check "
            "https://ofac.treasury.gov/sanctions-list-service for the current one.",
            file=sys.stderr,
        )
        return 1

    print(f"parsing {len(raw) / 1_048_576:.1f} MB")
    entries = parse(raw)

    if not entries:
        # Loudly, because "0 entries" from a 27 MB download means the format
        # moved, and a screening system that silently checks against an empty
        # list is worse than one that refuses to start.
        print(
            "parsed 0 entries from a non-empty download — the XML format has "
            "probably changed. Refusing to write an empty list.",
            file=sys.stderr,
        )
        return 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(
            {
                "source": "ofac",
                "licence": (
                    "Public domain. A work of the US Government under "
                    "17 U.S.C. 105. Free to use and redistribute."
                ),
                "url": SDN_XML_URL,
                "entries": entries,
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    with_aliases = sum(1 for e in entries if e["aliases"])
    print(f"wrote {len(entries)} entries to {OUTPUT}")
    print(f"  {with_aliases} have at least one alias")
    print("\nSet SANCTIONS_SOURCE=ofac in .env to screen against it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
