"""Score a synthetic population and report the distribution.

    .venv/Scripts/python analyse_distribution.py --count 400 --source ofac

Runs the REAL index and the REAL scoring functions — no database, no worker, no
queue. That is possible only because scoring.py is pure, and it is the clearest
practical demonstration of what purity buys: the risk logic can be exercised
over hundreds of cases in a second, with nothing to set up and nothing to mock.

HOW THE POPULATION IS BUILT, because the numbers mean nothing without it

Real base rates, approximately, for a consumer fintech:

  - the overwhelming majority of applicants are unremarkable
  - ~5-8% fail or abandon the document check
  - genuine sanctions matches are very rare, well under 0.1%
  - PEPs are uncommon but not rare, around 0.5-2%
  - FALSE-POSITIVE name matches are the single biggest driver of review volume

The population below is built to those rates rather than to flatter the
thresholds. A generator that produced 30% sanctions hits would make the system
look decisive and would tell you nothing about how it behaves on a Tuesday.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter

from scoring import (
    ApplicantProfile,
    Routing,
    ScreeningHit,
    score_application,
)
from screening.sources import load_index

# Ordinary names, drawn to look like a European consumer book with a broad
# diaspora mix. None of these is on any list.
GIVEN = [
    "James", "Olivia", "Mateusz", "Aisha", "Lukas", "Sofia", "Daniel", "Chen",
    "Priya", "Tomasz", "Emma", "Mohammed", "Sarah", "Andrei", "Fatima", "Liam",
    "Yusuf", "Anna", "Carlos", "Mei", "Ibrahim", "Elena", "Noah", "Zainab",
    "Viktor", "Laura", "Hassan", "Ingrid", "Diego", "Nadia",
]
FAMILY = [
    "Smith", "Kowalski", "Nowak", "Silva", "Muller", "Rossi", "Dubois", "Khan",
    "Patel", "Wang", "Garcia", "Hansen", "Okafor", "Ivanov", "Haddad", "Novak",
    "Andersson", "Mensah", "Traore", "Sokolov", "Petrov", "Diallo", "Nguyen",
    "Kim", "Osei", "Bakker", "Horvath", "Costa", "Fernandez", "Larsson",
]

# Weighted to look like a real book: mostly low-risk, a tail of elsewhere.
COUNTRIES = (
    ["GB"] * 40 + ["DE"] * 12 + ["FR"] * 10 + ["ES"] * 8 + ["PL"] * 6 +
    ["IE"] * 4 + ["NL"] * 4 + ["IT"] * 4 + ["PT"] * 3 + ["SE"] * 3 +
    ["US"] * 3 + ["IN"] * 2 + ["BR"] * 2 +
    # FATF increased monitoring
    ["NG"] * 2 + ["ZA"] + ["TR"] + ["AE"] + ["VN"] +
    # FATF call for action — genuinely rare in a European consumer book
    ["IR"]
)

# ~93% clean, matching a real document-check pass rate.
VENDOR_STATUSES = (
    ["Approved"] * 93 + ["Declined"] * 3 + ["Abandoned"] * 2 +
    ["In Review"] * 1 + ["Expired"] * 1
)


def build_population(count: int, index, rng: random.Random) -> list[tuple[str, str, str, str]]:
    """(full_name, country, vendor_status, planted) for each synthetic applicant.

    `planted` records whether this applicant was deliberately made to BE someone
    on the list, so recall can be measured rather than assumed. Without it the
    distribution tells you how often the system fires, which is the less
    important half: a missed sanctions match is a regulatory breach, and a run
    that reports only a tidy-looking split is hiding exactly that number.
    """
    listed_names = [entry.name for entry in index.entries if " " in entry.name]

    people: list[tuple[str, str, str, str]] = []
    for _ in range(count):
        roll = rng.random()
        planted = "no"

        if roll < 0.02 and listed_names:
            # A genuine hit: the applicant IS on the list, exact spelling.
            name = rng.choice(listed_names)
            planted = "exact"
        elif roll < 0.05 and listed_names:
            # A genuine hit wearing a different spelling — the transliteration
            # case. One character changed, which is what a passport
            # transliteration difference actually looks like.
            base = rng.choice(listed_names)
            position = rng.randrange(len(base))
            if base[position].isalpha():
                name = base[:position] + rng.choice("aeiou") + base[position + 1:]
            else:
                name = base
            planted = "transliterated"
        else:
            # Everybody else.
            name = f"{rng.choice(GIVEN)} {rng.choice(FAMILY)}"

        people.append((name, rng.choice(COUNTRIES), rng.choice(VENDOR_STATUSES), planted))

    return people


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=400)
    parser.add_argument("--source", default="synthetic")
    parser.add_argument("--seed", type=int, default=20260917)
    args = parser.parse_args()

    # Seeded, so the numbers in decisions.md can be reproduced rather than
    # merely quoted.
    rng = random.Random(args.seed)
    index = load_index(args.source)
    print(f"list: {args.source}, {len(index)} entries")

    people = build_population(args.count, index, rng)

    scores: list[int] = []
    routings = Counter()
    signal_counts = Counter()
    hit_counts = Counter()
    examples: dict[str, tuple] = {}

    planted_total = Counter()
    planted_caught = Counter()
    missed: list[tuple[str, str]] = []
    # False positives among applicants who are NOT on any list. The cost side
    # of the trade: each one is a real customer turned away or an officer's
    # hour burned.
    ordinary_total = 0
    false_positive_bands = Counter()
    false_rejections: list[tuple[str, str, int]] = []

    for name, country, vendor_status, planted in people:
        matches = index.search(name)
        hit_counts[min(len(matches), 5)] += 1

        profile = ApplicantProfile(
            country=country,
            vendor_status=vendor_status,
            hits=tuple(
                ScreeningHit(
                    match_type=m.entry.entity_type,
                    list_name=m.entry.list_name,
                    matched_name=m.matched_name,
                    match_score=m.score,
                    entity_id=m.entry.entity_id,
                )
                for m in matches
            ),
        )
        assessment = score_application(profile)

        if planted != "no":
            planted_total[planted] += 1
            found = any(m.entry.entity_type == "sanctions" for m in matches)
            if found:
                planted_caught[planted] += 1
            else:
                missed.append((planted, name))
        else:
            ordinary_total += 1
            for signal in assessment.signals:
                if signal.code.startswith("sanctions_"):
                    band = signal.code.split("_", 1)[1]
                    false_positive_bands[band] += 1
                    if assessment.routing == Routing.REJECT:
                        false_rejections.append(
                            (name, signal.evidence.get("matched_name", "?"),
                             assessment.score)
                        )

        scores.append(assessment.score)
        routings[assessment.routing] += 1
        for signal in assessment.signals:
            signal_counts[signal.code] += 1

        if assessment.routing not in examples and assessment.signals:
            examples[assessment.routing] = (name, country, assessment)

    total = len(scores)
    print(f"\n{'=' * 62}\nSCORE DISTRIBUTION  (n={total})\n{'=' * 62}")

    buckets = [(0, 0), (1, 9), (10, 19), (20, 29), (30, 39), (40, 49),
               (50, 59), (60, 79), (80, 99), (100, 10_000)]
    for low, high in buckets:
        n = sum(1 for s in scores if low <= s <= high)
        if n == 0 and low > 0:
            continue
        label = f"{low}" if low == high else f"{low}-{high if high < 10_000 else '+'}"
        bar = "#" * round(60 * n / total)
        print(f"  {label:>8}  {n:5d}  {100 * n / total:5.1f}%  {bar}")

    print(f"\n{'=' * 62}\nROUTING\n{'=' * 62}")
    for routing in (Routing.APPROVE, Routing.REVIEW, Routing.REJECT):
        n = routings[routing]
        print(f"  {routing:<8} {n:5d}  {100 * n / total:5.1f}%")

    print(f"\n{'=' * 62}\nWHICH SIGNALS FIRED\n{'=' * 62}")
    for code, n in signal_counts.most_common():
        print(f"  {code:<36} {n:5d}  {100 * n / total:5.1f}%")

    print(f"\n{'=' * 62}\nLIST CANDIDATES PER APPLICANT\n{'=' * 62}")
    for candidates in sorted(hit_counts):
        n = hit_counts[candidates]
        label = f"{candidates}+" if candidates == 5 else str(candidates)
        print(f"  {label:>3} candidate(s)  {n:5d}  {100 * n / total:5.1f}%")

    print(f"\n{'=' * 62}\nRECALL: DID WE CATCH THE PEOPLE WE PLANTED?\n{'=' * 62}")
    print("  The number that matters most. A missed sanctions match is a")
    print("  regulatory breach; a false positive is a customer turned away and")
    print("  an officer's hour burned. Only one of those is a criminal offence,")
    print("  so a tidy-looking split means nothing without this figure.\n")
    for kind in ("exact", "transliterated"):
        planted = planted_total[kind]
        if planted == 0:
            continue
        caught = planted_caught[kind]
        print(
            f"  {kind:<16} caught {caught:3d} / {planted:3d}"
            f"   ({100 * caught / planted:5.1f}%)"
        )
    if missed:
        print(f"\n  MISSED ({len(missed)}) — each of these is a breach:")
        for kind, name in missed[:10]:
            print(f"    [{kind}] {name}")

    print(f"\n{'=' * 62}\nFALSE POSITIVES: ORDINARY APPLICANTS FLAGGED\n{'=' * 62}")
    print(f"  {ordinary_total} applicants were on no list at all.\n")
    for band in ("confirmed", "probable", "weak"):
        n = false_positive_bands[band]
        if n:
            print(f"  matched at {band:<10} {n:4d}   {100 * n / ordinary_total:5.2f}%")
    n_rejected = len(false_rejections)
    print(
        f"\n  AUTO-REJECTED despite being on no list: {n_rejected}"
        f"   ({100 * n_rejected / ordinary_total:5.2f}%)"
    )
    for name, matched, score in false_rejections[:6]:
        print(f"    {name!r} matched {matched!r} (score {score})")

    print(f"\n{'=' * 62}\nONE EXAMPLE OF EACH OUTCOME\n{'=' * 62}")
    for routing in (Routing.APPROVE, Routing.REVIEW, Routing.REJECT):
        if routing not in examples:
            print(f"\n  {routing.upper()}: none in this sample")
            continue
        name, country, assessment = examples[routing]
        print(f"\n  {routing.upper()}  {name} ({country})  score {assessment.score}")
        for signal in assessment.signals:
            print(f"    {signal.points:+4d}  {signal.reason}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
