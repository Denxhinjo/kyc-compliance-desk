"""Comparing secondary identifiers between an applicant and a list entry.

WHY THIS EXISTS

A name match is a POTENTIAL match. Whether two records are the same person
depends on the identifiers either side publishes — date of birth first, then
nationality, then whatever else the list carries. Until 2026-10-2 this project
collapsed all of that into one word, "confirmed", derived from name similarity
alone. It is the single most misleading thing the system said: in compliance,
"confirmed" is the conclusion of a human review, not the output of a string
comparison.

So nothing here returns a verdict. Each identifier present on both sides is
reported as `agree`, `year_agrees`, `conflict` or `not_available`, and the
reviewer draws the conclusion.

EVERYTHING COMES FROM THE STORED SNAPSHOT

Values are read from the sanctions snapshot that was screened against, never
inferred and never recalled from anywhere else. Where the list is silent the
answer is `not_available`, which is a real and useful answer: "the list does
not say" is different from "they disagree", and a reviewer needs to see which
one they are looking at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

AGREE = "agree"
YEAR_AGREES = "year_agrees"
CONFLICT = "conflict"
NOT_AVAILABLE = "not_available"

#: A full date as OFAC writes it: "10 Dec 1948".
_FULL_DATE = re.compile(r"^(\d{2}) ([A-Za-z]{3}) (\d{4})$")
#: A bare year: "1948".
_YEAR_ONLY = re.compile(r"^(\d{4})$")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True)
class Comparison:
    """One identifier, compared. Presentation is the caller's business."""

    identifier: str
    outcome: str
    #: What to show the reviewer, quoting the list verbatim where it is odd.
    detail: str

    @property
    def conflicts(self) -> bool:
        return self.outcome == CONFLICT

    def as_dict(self) -> dict[str, str]:
        return {
            "identifier": self.identifier,
            "outcome": self.outcome,
            "detail": self.detail,
        }


def _parse_listed_date(raw: str) -> tuple[str, tuple[int, ...]] | None:
    """A listed date of birth as ('full', (y, m, d)) or ('year', (y,)).

    Returns None for anything that is not one precise value. That is not
    laziness about parsing: see `compare_dates_of_birth` for why an imprecise
    value is allowed to agree with nothing and to contradict nothing.
    """
    value = raw.strip()
    full = _FULL_DATE.match(value)
    if full:
        day, month, year = full.groups()
        index = _MONTHS.get(month.lower())
        if index is None:
            return None
        return "full", (int(year), index, int(day))
    year_only = _YEAR_ONLY.match(value)
    if year_only:
        return "year", (int(year_only.group(1)),)
    return None


def compare_dates_of_birth(
    applicant: object | None, listed: tuple[str, ...] | list[str] | None
) -> Comparison:
    """Compare an applicant's date of birth against every date the list gives.

    THE LIST OFTEN GIVES SEVERAL. 532 OFAC SDN entries publish more than one
    date of birth -- Hasan NASRALLAH carries four: 31 Aug 1960, 1953, 1955 and
    1958. Until 2026-10-2 the loader kept the first and discarded the rest,
    so the system could announce a "conflict" with a date the list ITSELF also
    publishes, and quietly weaken a genuine match. A conflict therefore
    requires disagreement with EVERY listed date, not with one of them.

    AN IMPRECISE VALUE CONTRADICTS NOTHING. "circa 1951" cannot rule anybody
    out, and neither can "1950 to 1955", "Mar 1962" or "02 Jan 1960 to 05 Jan
    1960" -- 260 values in the current snapshot are shaped like one of those.
    If ANY listed value is imprecise, this never returns `conflict`, even when
    other precise values are also listed and disagree. It can still return
    `agree` on a precise value that matches, because agreement needs only one
    date to line up.
    """
    values = tuple(v for v in (listed or ()) if v and v.strip())
    if applicant is None or not values:
        return Comparison(
            "date_of_birth",
            NOT_AVAILABLE,
            "the list publishes no date of birth" if not values
            else "the application has no date of birth",
        )

    year, month, day = applicant.year, applicant.month, applicant.day

    parsed = [(raw, _parse_listed_date(raw)) for raw in values]
    imprecise = [raw for raw, p in parsed if p is None]

    for raw, p in parsed:
        if p is None:
            continue
        kind, parts = p
        if kind == "full" and parts == (year, month, day):
            return Comparison("date_of_birth", AGREE, f"listed {raw}")
        if kind == "year" and parts[0] == year:
            return Comparison(
                "date_of_birth", YEAR_AGREES,
                f"the list gives a year only ({raw}), which matches",
            )

    if imprecise:
        # Nothing agreed, and something on the list cannot be ruled out.
        shown = ", ".join(f'"{v}"' for v in imprecise)
        return Comparison(
            "date_of_birth", NOT_AVAILABLE,
            f"the list gives {shown}, which is too imprecise to compare",
        )

    listed_shown = ", ".join(values)
    return Comparison(
        "date_of_birth", CONFLICT,
        f"listed {listed_shown}; the application gives "
        f"{year:04d}-{month:02d}-{day:02d}",
    )


def compare_suffix(applicant_suffix: str | None, listed_suffix: str | None) -> Comparison:
    """Compare generational suffixes: Jr, Sr, II, III.

    A suffix exists precisely to tell apart two people who share a name, so
    "Jr" against "Sr" is positive evidence of two different people and is
    reported as a conflict. A suffix on only one side proves nothing either
    way -- most records simply omit it -- so it is `not_available`, and the
    caller caps name strength rather than treating it as agreement.
    """
    if applicant_suffix and listed_suffix:
        if applicant_suffix == listed_suffix:
            return Comparison("suffix", AGREE, f"both give {applicant_suffix.title()}")
        return Comparison(
            "suffix", CONFLICT,
            f"the list gives {listed_suffix.title()}, "
            f"the application gives {applicant_suffix.title()}",
        )
    if listed_suffix:
        return Comparison(
            "suffix", NOT_AVAILABLE,
            f"the list gives {listed_suffix.title()}; the application gives none",
        )
    if applicant_suffix:
        return Comparison(
            "suffix", NOT_AVAILABLE,
            f"the application gives {applicant_suffix.title()}; the list gives none",
        )
    return Comparison("suffix", NOT_AVAILABLE, "neither side gives one")


def compare_nationality(
    applicant: str | None, listed: tuple[str, ...] | list[str] | None
) -> Comparison:
    """Compare nationality.

    Until Release B the application does not collect one, so this answers
    `not_available` on every case. It is written now, and called now, because
    an identifier the interface never mentions is an identifier nobody asks
    for -- showing "Nationality: not available" is what makes its absence
    visible.

    NOT address country. `applications.address_country` is where somebody
    lives, which is not where they hold a passport, and treating one as the
    other would manufacture agreements and conflicts that no source states.
    """
    values = tuple(v for v in (listed or ()) if v and v.strip())
    if not applicant:
        detail = (
            f"the list gives {', '.join(values)}; the application does not record "
            "nationality"
            if values
            else "neither side gives one"
        )
        return Comparison("nationality", NOT_AVAILABLE, detail)
    if not values:
        return Comparison(
            "nationality", NOT_AVAILABLE, "the list publishes no nationality"
        )

    wanted = applicant.strip().casefold()
    for value in values:
        if value.strip().casefold() == wanted:
            return Comparison("nationality", AGREE, f"both give {value}")
    return Comparison(
        "nationality", CONFLICT,
        f"listed {', '.join(values)}; the application gives {applicant}",
    )
