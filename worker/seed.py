"""Generate a synthetic applicant history that does not look generated.

    .venv/Scripts/python seed.py --count 500 --weeks 6

WHAT GIVES SYNTHETIC DATA AWAY

Worth naming the tells, because every choice below is aimed at one of them:

  1. FLAT TIMESTAMPS. random.uniform(start, end) produces a histogram with no
     shape at all. Real arrivals have a daily rhythm and a weekly one, and a
     histogram is the first thing anyone plots.

  2. UNIFORM EVERYTHING. Ages evenly spread across 18-80; every name used
     exactly 16 times out of 500. Real name frequency is closer to Zipf: a few
     very common, a long tail of singletons.

  3. NO CORRELATION BETWEEN FIELDS. Random name crossed with random country
     gives "Wolfgang Schmidt, Vietnam".

  4. EVERY JOURNEY COMPLETE. Real funnels are full of people who started and
     wandered off. A dataset where 100% of applicants finish is fiction, and it
     quietly flatters every percentage computed from it.

  5. TIDY OUTCOME RATIOS. Exactly 33/33/33 is the fingerprint of someone
     generating to a specification rather than observing a process.

  6. MACHINE-EVEN DURATIONS. Everything taking two to four hours, with no tail.

  7. INSERTION ORDER NOT MATCHING TIME ORDER. If rows are generated in random
     order, the sequential id will not be monotonic with created_at — which is
     exactly what a backfill looks like. So this inserts CHRONOLOGICALLY, and
     ids and timestamps agree the way they would if the rows had really arrived.

THE OUTCOMES ARE NOT ASSIGNED, THEY ARE COMPUTED

This script does not decide who gets approved. It generates applicants and
vendor results, then runs the REAL Phase 5 code — the actual SanctionsIndex,
the actual pure score_application() — and routes on the real thresholds. Only
the human decisions on referred cases are simulated.

So the numbers on the stats page measure the actual pipeline rather than
assumptions about it. The outcome mix will be lopsided, because that is what
the scoring produces; an even split would itself be the tell.

A NOTE ON HONESTY

This writes BACKDATED audit_events. That table is the one thing in the system
meant to be the unvarnished truth, and here it is being fabricated wholesale.
So every seeded audit row carries "seeded": true in its details. Anyone can
filter them out and nobody reading a timeline can mistake invented history for
real history.

It also cannot be undone. audit_events rejects DELETE, and that rule binds the
person who wrote it as much as anyone else — so the seeder refuses to run twice
unless forced, and a genuine reset means dropping the schema and re-migrating.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from config import SANCTIONS_SOURCE
from db import connect
from scoring import (
    ApplicantProfile,
    Routing,
    ScreeningHit,
    score_application,
)
from screening.sources import ensure_snapshot, load_index

# ---------------------------------------------------------------------------
# The arrival model
# ---------------------------------------------------------------------------

#: Relative volume by hour, local time. Almost nothing at 04:00, a long evening
#: peak. Deliberately not a neat curve — real ones have a lunchtime bump and an
#: after-work spike, and a smooth sine wave reads as generated.
HOUR_WEIGHTS = [
    0.04, 0.02, 0.01, 0.01, 0.01, 0.02,  # 00-05
    0.06, 0.16, 0.34, 0.52, 0.60, 0.62,  # 06-11
    0.70, 0.66, 0.58, 0.56, 0.62, 0.78,  # 12-17
    0.92, 1.00, 0.96, 0.80, 0.54, 0.24,  # 18-23
]

#: Monday=0. A consumer product gets a weekend lift, not a weekend collapse —
#: people open accounts when they are not at work.
DAY_WEIGHTS = [0.92, 0.95, 0.97, 0.98, 0.90, 1.00, 0.96]

#: When a compliance officer is at their desk. This single detail does more for
#: realism than anything else here: it is what makes the queue build overnight
#: and across the weekend and drain on the next working morning, and it is what
#: makes median time-to-decision a real number rather than a constant.
OFFICE_START_HOUR = 9
OFFICE_END_HOUR = 17.5


def arrival_weight(when: datetime) -> float:
    return HOUR_WEIGHTS[when.hour] * DAY_WEIGHTS[when.weekday()]


def sample_arrivals(count: int, weeks: int, rng: random.Random) -> list[datetime]:
    """Arrival times shaped by hour and weekday, by rejection sampling.

    Propose a uniform instant, keep it with probability proportional to its
    weight. A few lines, and it produces a histogram with genuine structure
    rather than a flat block.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(weeks=weeks)
    span = (now - start).total_seconds()

    times: list[datetime] = []
    while len(times) < count:
        candidate = start + timedelta(seconds=rng.random() * span)
        if rng.random() < arrival_weight(candidate):
            times.append(candidate)

    # Chronological, so ids and timestamps agree — see tell #7.
    times.sort()
    return times


def next_office_moment(after: datetime, rng: random.Random) -> datetime:
    """The next plausible moment an officer would pick a case up.

    Not the next working second: officers work a queue in batches, so this
    lands somewhere inside the working day rather than at 09:00:00 sharp.
    """
    moment = after
    for _ in range(400):  # a fortnight of half-days; far more than needed
        weekday = moment.weekday()
        hour = moment.hour + moment.minute / 60

        if weekday >= 5:  # weekend
            days = 7 - weekday
            moment = (moment + timedelta(days=days)).replace(
                hour=OFFICE_START_HOUR, minute=0, second=0
            )
            continue
        if hour < OFFICE_START_HOUR:
            moment = moment.replace(hour=OFFICE_START_HOUR, minute=0, second=0)
        elif hour >= OFFICE_END_HOUR:
            moment = (moment + timedelta(days=1)).replace(
                hour=OFFICE_START_HOUR, minute=0, second=0
            )
            continue

        # Inside working hours. Officers do not clear the queue instantly.
        delay = rng.lognormvariate(math.log(45 * 60), 1.0)  # median ~45 min
        picked = moment + timedelta(seconds=min(delay, 6 * 3600))
        if picked.hour + picked.minute / 60 < OFFICE_END_HOUR:
            return picked
        moment = (moment + timedelta(days=1)).replace(
            hour=OFFICE_START_HOUR, minute=0, second=0
        )

    return moment


def lognormal_seconds(median_seconds: float, spread: float, rng: random.Random) -> float:
    """A duration with a long right tail.

    Real processing times are not uniform between two bounds; most are quick
    and a few are dramatically not. A lognormal gives that shape for free.
    """
    return rng.lognormvariate(math.log(median_seconds), spread)


# ---------------------------------------------------------------------------
# The people
# ---------------------------------------------------------------------------

# --- Names ------------------------------------------------------------------
#
# The first version used one pool of forty given names crossed with one pool of
# forty surnames, both sampled with a Pareto(1.2) bias. Two things went wrong,
# and both were visible in a single screenshot of the queue.
#
#   1. The Pareto tail is far too steep. Index 0 came up roughly a third of the
#      time, so "James" and "Smith" dominated and a ten-row queue contained
#      "James Smith", "James Kowalski" and "James Smith" again. Repetition in
#      real data is incidental; that was systematic, and systematic repetition
#      is the loudest sign that a name was assembled rather than borne.
#
#   2. Name and country were drawn INDEPENDENTLY. The docstring at the top of
#      this file lists "Wolfgang Schmidt, Vietnam" as tell #3, and then the
#      code went and did exactly that.
#
# So a name is now drawn from a culture that the country selects, and the
# shapes vary the way real names do: patronymics, double-barrelled surnames,
# second given names, and the same name transliterated more than one way.


@dataclass(frozen=True)
class NameCulture:
    given: tuple[str, ...]
    family: tuple[str, ...]
    #: A second given name.
    middle_chance: float = 0.10
    #: A double surname. Ordinary in Iberia, occasional in Britain, rare else.
    hyphen_chance: float = 0.04
    #: Patronymic forms, which lengthen a name without changing whose it is —
    #: and which fuzzy matching has to survive.
    patronymics: tuple[str, ...] = ()
    patronymic_chance: float = 0.0


CULTURES: dict[str, NameCulture] = {
    "britain": NameCulture(
        given=("James", "Olivia", "Liam", "Grace", "Noah", "Sophie", "Harry",
               "Isla", "Oscar", "Freya", "Alfie", "Niamh", "Rory", "Bethan",
               "Callum", "Eilidh", "Seren", "Dylan"),
        family=("Smith", "Jones", "Williams", "Taylor", "Brown", "Davies",
                "O'Brien", "Murphy", "Kelly", "MacLeod", "Okafor", "Mensah",
                "Patel", "Begum", "Ahmed", "Lloyd", "Pritchard", "Fitzgerald"),
        hyphen_chance=0.06,
    ),
    "iberia": NameCulture(
        given=("Carlos", "Sofia", "Diego", "Lucia", "Mateo", "Elena", "Tiago",
               "Beatriz", "Rafael", "Ines"),
        family=("Silva", "Garcia", "Fernandez", "Costa", "Moreno", "Oliveira",
                "Vasquez", "Delgado", "Carvalho", "Ferreira"),
        # Two surnames are the norm here rather than the exception.
        hyphen_chance=0.34,
    ),
    "central_europe": NameCulture(
        given=("Mateusz", "Anna", "Lukas", "Zofia", "Tomas", "Katerina",
               "Marek", "Hanna", "Jakub", "Eva"),
        family=("Kowalski", "Nowak", "Wojcik", "Novak", "Horvath", "Varga",
                "Dvorak", "Zielinski", "Kucera", "Szabo"),
    ),
    "germanic": NameCulture(
        given=("Lukas", "Emma", "Jonas", "Lena", "Finn", "Ingrid", "Sven",
               "Astrid", "Bram", "Saskia"),
        family=("Muller", "Schmidt", "Hansen", "Jensen", "Andersson",
                "Larsson", "Bakker", "de Vries", "Nilsson", "Weber"),
        middle_chance=0.18,
    ),
    "france": NameCulture(
        given=("Camille", "Lucas", "Chloe", "Hugo", "Manon", "Theo", "Ines",
               "Nathan", "Jade", "Enzo", "Louise", "Gabin"),
        family=("Dubois", "Moreau", "Laurent", "Lefebvre", "Girard", "Renaud",
                "Bernard", "Fontaine", "Rousseau", "Marchand", "Leroy"),
        hyphen_chance=0.12,
    ),
    "italy": NameCulture(
        given=("Giulia", "Marco", "Chiara", "Lorenzo", "Alessia", "Matteo",
               "Francesca", "Davide", "Elisa", "Riccardo"),
        family=("Rossi", "Ferrari", "Esposito", "Conti", "Greco", "Bruno",
                "Gallo", "Marino", "Ricci", "Lombardi"),
    ),
    "slavic": NameCulture(
        given=("Andrei", "Elena", "Dmitri", "Viktor", "Natalia", "Sergei",
               "Irina", "Pavel"),
        family=("Ivanov", "Sokolov", "Petrov", "Volkov", "Popescu",
                "Constantinescu", "Ionescu", "Morozov"),
        patronymics=("Ivanovich", "Sergeyevich", "Dmitrievna", "Nikolayevna",
                     "Petrovich", "Alexeyevna"),
        patronymic_chance=0.30,
    ),
    "arabic": NameCulture(
        given=("Mohammed", "Fatima", "Yusuf", "Layla", "Omar", "Zainab",
               "Ibrahim", "Nadia", "Hassan", "Amira", "Khalid", "Rania"),
        family=("Al-Rashid", "Haddad", "Nasser", "Khalil", "Mansour",
                "Al-Mansoori", "Saleh", "Aziz", "Farouk", "Barakat"),
        patronymics=("bin Ahmed", "bin Saleh", "bint Omar"),
        patronymic_chance=0.14,
    ),
    "south_asia": NameCulture(
        given=("Priya", "Arjun", "Ananya", "Rohan", "Meera", "Vikram",
               "Aisha", "Imran"),
        family=("Patel", "Sharma", "Khan", "Reddy", "Iyer", "Chowdhury",
                "Siddiqui", "Nair"),
    ),
    "east_asia": NameCulture(
        given=("Chen", "Mei", "Wei", "Jing", "Minh", "Linh", "Ji-woo", "Hana"),
        family=("Wang", "Zhang", "Nguyen", "Tran", "Kim", "Park", "Li", "Pham"),
        middle_chance=0.02,
        hyphen_chance=0.0,
    ),
    "west_africa": NameCulture(
        given=("Kwame", "Amara", "Chinedu", "Ngozi", "Kofi", "Adaeze",
               "Emeka", "Fatoumata"),
        family=("Okafor", "Mensah", "Traore", "Diallo", "Osei", "Adeyemi",
                "Boateng", "Nwosu"),
    ),
    "southern_africa": NameCulture(
        given=("Thabo", "Lerato", "Sipho", "Naledi", "Bongani", "Zanele",
               "Mandla", "Nomsa", "Tshepo", "Refilwe"),
        family=("Dlamini", "Nkosi", "Botha", "Mokoena", "Zulu", "Khumalo",
                "Van der Merwe", "Sithole", "Maseko", "Ndlovu"),
    ),
    "latam": NameCulture(
        given=("Mateo", "Valentina", "Santiago", "Camila", "Joaquin", "Isabela",
               "Thiago", "Manuela", "Bruno", "Larissa"),
        family=("Santos", "Souza", "Rodrigues", "Almeida", "Pereira", "Lima",
                "Barbosa", "Ribeiro", "Cardoso", "Mendes"),
        hyphen_chance=0.22,
    ),
    "north_america": NameCulture(
        given=("Michael", "Jessica", "Tyler", "Ashley", "Jordan", "Brandon",
               "Megan", "Cody", "Danielle", "Austin", "Kayla", "Devon"),
        family=("Johnson", "Miller", "Anderson", "Thompson", "Nguyen",
                "Martinez", "Robinson", "Hernandez", "Wright", "Coleman"),
        middle_chance=0.30,
    ),
    "iran": NameCulture(
        given=("Zahra", "Reza", "Maryam", "Amir", "Sara", "Mehdi", "Leila",
               "Hamid", "Nasrin", "Farhad", "Shirin", "Kian"),
        family=("Mousavi", "Hosseini", "Karimi", "Ahmadi", "Rezaei",
                "Jafari", "Sadeghi", "Ebrahimi", "Naderi", "Tehrani"),
    ),
    "myanmar": NameCulture(
        given=("Aung", "Thida", "Zaw", "Nilar", "Myo", "Khin", "Kyaw", "Ei"),
        family=("Win", "Htun", "Oo", "Myint", "Soe", "Hlaing", "Thein", "Nyunt"),
        hyphen_chance=0.0,
    ),
    "turkey": NameCulture(
        given=("Emre", "Elif", "Mustafa", "Zeynep", "Burak", "Ayse", "Kerem",
               "Selin", "Hakan", "Merve", "Onur", "Ece"),
        family=("Yilmaz", "Demir", "Kaya", "Celik", "Sahin", "Ozturk",
                "Arslan", "Dogan", "Aydin", "Polat"),
    ),
}

COUNTRY_CULTURE = {
    "GB": "britain", "IE": "britain",
    "ES": "iberia", "PT": "iberia",
    "PL": "central_europe",
    "DE": "germanic", "SE": "germanic", "NL": "germanic",
    "FR": "france", "IT": "italy",
    "RO": "slavic",
    "AE": "arabic",
    "IN": "south_asia",
    "VN": "east_asia",
    "NG": "west_africa",
    "ZA": "southern_africa",
    "BR": "latam",
    "US": "north_america",
    "IR": "iran",
    "TR": "turkey",
    "MM": "myanmar",
}

#: Britain and the US are immigration countries: a meaningful share of people
#: there carry names from elsewhere. Without this, every GB applicant is called
#: Smith or Jones, which is its own kind of fiction.
DIASPORA_CHANCE = {"GB": 0.30, "US": 0.34, "FR": 0.18, "DE": 0.16, "NL": 0.14}
DIASPORA_POOL = ("south_asia", "arabic", "west_africa", "east_asia",
                 "central_europe", "iberia", "slavic")

#: Names that transliterate more than one way. Drawn from the same person-space
#: as everything else, so "Mohammed Hassan" and "Muhammad Hasan" can both turn
#: up and be different people — which is exactly the case fuzzy matching has to
#: get right and a human has to adjudicate.
TRANSLITERATIONS = {
    "Mohammed": ("Muhammad", "Mohamed", "Mohammad"),
    "Hassan": ("Hasan", "Hassane"),
    "Yusuf": ("Yousef", "Youssef", "Yusif"),
    "Ibrahim": ("Ebrahim", "Brahim"),
    "Fatima": ("Fatimah", "Fatma"),
    "Zainab": ("Zaynab", "Zeinab"),
    "Ivanov": ("Ivanoff", "Ivanow"),
    "Sokolov": ("Sokoloff",),
    "Al-Rashid": ("Alrashid", "El-Rashid", "Al Rashid"),
    "Mousavi": ("Musavi", "Moussavi"),
    "Chowdhury": ("Choudhury", "Chaudhry"),
    "Yilmaz": ("Yilmez", "Jilmaz"),
}


def zipf_weights(size: int, exponent: float = 0.8) -> list[float]:
    """Frequency weights for a pool of `size` names.

    A true Zipf is 1/rank; this is 1/rank**0.8, which is gentler. The previous
    version used `paretovariate(1.2)`, whose index 0 came up about a third of
    the time — that is not a name distribution, it is one name with occasional
    company.
    """
    return [1.0 / (rank ** exponent) for rank in range(1, size + 1)]


def pick_name(items: tuple[str, ...], rng: random.Random) -> str:
    return rng.choices(items, weights=zipf_weights(len(items)), k=1)[0]


def transliterate(name: str, rng: random.Random, chance: float = 0.26) -> str:
    variants = TRANSLITERATIONS.get(name)
    if variants and rng.random() < chance:
        return rng.choice(variants)
    return name


def culture_for(country: str, rng: random.Random) -> str:
    """Which naming culture an applicant from this country plausibly has."""
    if rng.random() < DIASPORA_CHANCE.get(country, 0.04):
        return rng.choice(DIASPORA_POOL)
    return COUNTRY_CULTURE.get(country, "britain")


def make_name(culture_key: str, rng: random.Random) -> str:
    """One person's name, in a shape their culture actually uses."""
    culture = CULTURES[culture_key]

    given = transliterate(pick_name(culture.given, rng), rng)
    family = transliterate(pick_name(culture.family, rng), rng)

    parts = [given]

    if culture.patronymics and rng.random() < culture.patronymic_chance:
        parts.append(rng.choice(culture.patronymics))
    elif rng.random() < culture.middle_chance:
        parts.append(pick_name(culture.given, rng))

    if rng.random() < culture.hyphen_chance:
        second = transliterate(pick_name(culture.family, rng), rng)
        if second != family:
            # Iberian double surnames are spaced; British ones hyphenated.
            joiner = " " if culture_key in {"iberia", "latam"} else "-"
            family = f"{family}{joiner}{second}"

    parts.append(family)
    return " ".join(parts)


#: Weighted like a UK-centred consumer book with a broad diaspora mix, and a
#: thin tail of higher-risk jurisdictions. Not uniform: a uniform country
#: distribution is tell #2 in its purest form.
COUNTRIES = (
    ["GB"] * 46 + ["IE"] * 5 + ["DE"] * 8 + ["FR"] * 7 + ["ES"] * 6 +
    ["PL"] * 6 + ["IT"] * 4 + ["NL"] * 4 + ["PT"] * 3 + ["SE"] * 3 +
    ["RO"] * 3 + ["US"] * 3 + ["IN"] * 2 + ["BR"] * 2 + ["ZA"] * 1 +
    # FATF increased monitoring
    ["NG"] * 2 + ["TR"] * 1 + ["AE"] * 1 + ["VN"] * 1 +
    # FATF call for action, genuinely rare here
    ["IR"] * 1
)

CITIES = {
    "GB": [("London", "EC2N 4AJ"), ("Manchester", "M1 4BT"), ("Leeds", "LS1 5RU"),
           ("Bristol", "BS1 4DJ"), ("Glasgow", "G1 3SL")],
    "IE": [("Dublin", "D02 XY45")], "DE": [("Berlin", "10115"), ("Munich", "80331")],
    "FR": [("Paris", "75002"), ("Lyon", "69001")], "ES": [("Madrid", "28013")],
    "PL": [("Warsaw", "00-001"), ("Krakow", "30-001")], "IT": [("Milan", "20121")],
    "NL": [("Amsterdam", "1012 AB")], "PT": [("Lisbon", "1100-148")],
    "SE": [("Stockholm", "111 20")], "RO": [("Bucharest", "010011")],
    "US": [("New York", "10001")], "IN": [("Mumbai", "400001")],
    "BR": [("Sao Paulo", "01310")], "ZA": [("Cape Town", "8001")],
    "NG": [("Lagos", "101001")], "TR": [("Istanbul", "34000")],
    "AE": [("Dubai", "00000")], "VN": [("Hanoi", "100000")], "IR": [("Tehran", "11369")],
}

STREETS = ["High Street", "Station Road", "Church Lane", "Victoria Road",
           "Mill Lane", "Bishopsgate", "Queen Street", "Park Avenue"]


def sample_date_of_birth(rng: random.Random, today: date) -> date:
    """Skewed young, with a tail into later life. Not uniform 18-80."""
    age = int(min(max(rng.lognormvariate(math.log(33), 0.36), 18), 88))
    day_offset = rng.randrange(365)
    return date(today.year - age, 1, 1) + timedelta(days=day_offset)


@dataclass
class Applicant:
    full_name: str
    date_of_birth: date
    line1: str
    city: str
    postcode: str
    country: str


#: Applicants constructed to land NEAR A THRESHOLD, as a share of the run.
#:
#: Why this has to be deliberate: the scorer is not being asked for a number,
#: it is being asked for a decision, and the only decisions worth looking at
#: are the ones that were nearly the other decision. Sampling the general
#: population does not produce those often enough to fill a queue — the first
#: version of this file produced a queue where every score was 30, 40 or 45,
#: and a reviewer looking at it would reasonably conclude the model had three
#: settings.
#:
#: WHAT "BORDERLINE" CAN MEAN HERE. Every value in the ruleset is a multiple
#: of five — sanctions 60/45/20, PEP 30/15/5, country 40/15, document
#: 0/10/20/30/40 — so a total is always a multiple of five and scores of 19,
#: 21, 79 or 81 are arithmetically unreachable. The tightest genuine pairs are
#: 15 against 20 at the referral line and 75 against 80 at the rejection line:
#: one point-step apart, the smallest difference the system can express.
#:
#: These shape the INPUT population. Nothing here assigns a score — the real
#: score_application() still computes every one of them, and if a rule changed
#: these cases would move with it.
BORDERLINE_SHARE = 0.12


@dataclass(frozen=True)
class Borderline:
    """A recipe for an applicant who lands next to a threshold."""
    country: str
    vendor_status: str
    #: Take a name from the sanctions list, so screening finds a match.
    listed_name: bool
    #: Force a conflicting date of birth, which downgrades the match one band.
    dob_conflict: bool
    expected: int
    note: str


BORDERLINE_RECIPES = (
    # --- the referral line: 20 refers, 15 auto-approves ---------------------
    Borderline("NG", "Approved", False, False, 15,
               "FATF monitoring only — auto-approves at 15, five points under"),
    Borderline("TR", "Approved", False, False, 15, "as above, different country"),
    Borderline("GB", "In Review", False, False, 10,
               "document in review, nothing else — comfortably clear"),
    Borderline("VN", "In Review", False, False, 25,
               "monitoring plus an unfinished document check — refers"),
    Borderline("AE", "Approved", True, True, 20,
               "a sanctions name match downgraded to weak by a conflicting "
               "date of birth, in a monitoring country: exactly on the line"),

    # --- the rejection line: 80 auto-rejects, 75 refers ---------------------
    Borderline("IR", "Declined", False, False, 80,
               "call for action plus a declined document — auto-rejects on "
               "the nose, with no sanctions match at all"),
    Borderline("MM", "Abandoned", False, False, 70,
               "another call-for-action country, abandoned rather than "
               "declined — refers instead of refusing"),
    Borderline("GB", "Abandoned", True, True, 50,
               "downgraded match plus an abandoned check"),
    Borderline("NG", "Abandoned", True, False, 90,
               "a probable sanctions match, a monitoring country and an "
               "abandoned document check — well over"),
    Borderline("GB", "Approved", True, False, 45,
               "a clean applicant whose only problem is their name"),
)


#: Tokens that mark a sanctions entry as an organisation rather than a person.
#: OFAC lists both, and an applicant called "HK LINK ASIA ELECTRONICS LIMITED"
#: is a tell of exactly the kind this file exists to avoid — the applicants are
#: individuals opening personal accounts.
CORPORATE_TOKENS = frozenset({
    "limited", "ltd", "ltd.", "llc", "l.l.c.", "inc", "inc.", "corp",
    "corporation", "company", "co.", "gmbh", "s.a.", "sa", "ag", "bv", "nv",
    "plc", "pte", "sdn", "bhd", "trading", "holdings", "holding", "group",
    "industries", "enterprises", "international", "shipping", "electronics",
    "petroleum", "bank", "airlines", "technologies", "logistics", "import",
    "export", "joint", "stock", "llp", "kft", "ooo", "oao", "pjsc", "jsc",
})


def is_person_name(name: str) -> bool:
    """A rough filter for "this list entry is a human being".

    Deliberately conservative — losing a few real people from the pool costs
    nothing, while letting one shipping company through produces an applicant
    whose name is a giveaway on the busiest screen in the demo.
    """
    if " " not in name:
        return False
    tokens = {word.strip(",").lower() for word in name.split()}
    return not (tokens & CORPORATE_TOKENS)


def _parse_listed_dob(value: str) -> date | None:
    """A list entry's date of birth, when it is a full and usable date.

    OFAC writes "10 Dec 1948", the synthetic fixture writes "1975-04-12", and
    some entries carry only a year. Handling only ISO — which is what the first
    version did — meant every OFAC date silently failed to parse and the
    date-of-birth conflict mechanic quietly did nothing on the one list that
    actually ships to production.
    """
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%Y"):
        try:
            return datetime.strptime(text[:11].strip(), fmt).date()
        except ValueError:
            continue
    return None


def make_applicant(
    rng: random.Random,
    today: date,
    listed_entries: list,
    recipe: "Borderline | None" = None,
) -> Applicant:
    """One synthetic person.

    COUNTRY IS DRAWN FIRST and the name follows from it. The other way round
    produced "Wolfgang Schmidt, Vietnam", which this file's own docstring lists
    as tell #3.
    """
    date_of_birth = sample_date_of_birth(rng, today)

    if recipe is not None:
        country = recipe.country
        if recipe.listed_name and listed_entries:
            # A conflicting date of birth downgrades a match one band, which is
            # how this recipe hits its number. So the entry has to HAVE a date
            # of birth for the conflict to be expressible at all.
            usable = [e for e in listed_entries if e.date_of_birth] or listed_entries
            entry = rng.choice(usable)
            full_name = entry.name
            if entry.date_of_birth:
                listed = _parse_listed_dob(entry.date_of_birth)
                if listed and recipe.dob_conflict:
                    # Same name, unmistakably not the same person.
                    date_of_birth = listed.replace(year=listed.year - 23)
                elif listed:
                    date_of_birth = listed
        else:
            full_name = make_name(culture_for(country, rng), rng)
    else:
        country = rng.choice(COUNTRIES)
        if rng.random() < 0.03 and listed_entries:
            # A few applicants share a name with someone on the list. This is
            # the realistic case and the reason the review desk exists: common
            # names collide, and a human has to tell the two people apart.
            full_name = rng.choice(listed_entries).name
        else:
            full_name = make_name(culture_for(country, rng), rng)

    city, postcode = rng.choice(CITIES.get(country, [("Springfield", "00000")]))
    return Applicant(
        full_name=full_name,
        date_of_birth=date_of_birth,
        line1=f"{rng.randrange(1, 240)} {rng.choice(STREETS)}",
        city=city,
        postcode=postcode,
        country=country,
    )


# ---------------------------------------------------------------------------
# The journey
# ---------------------------------------------------------------------------

#: Where applicants stop. Tell #4: a dataset in which everyone finishes is
#: fiction, and it quietly flatters every percentage computed from it.
#:
#: These are the honest shape of an onboarding funnel — a real one loses people
#: at every step, and those losses are the difference between "applications
#: created" and "applications decided".
DROP_NEVER_STARTED = 0.06   # created an application, never opened verification
DROP_ABANDONED = 0.05       # opened the vendor flow, never finished
DROP_STILL_CHECKING = 0.03  # vendor has it, no verdict yet

#: Referrals that are still sitting in the queue, as a function of age.
#:
#: Two wrong models were tried before this one, and both are worth recording.
#:
#: The first assumed officers always clear the queue within the working day.
#: That left one case pending out of 500 — optimistic rather than realistic,
#: since real desks carry a backlog.
#:
#: The second gave every referral a flat 18% chance of still being open,
#: independent of age. That produced a queue whose oldest case had been waiting
#: 32 days, which is not a backlog, it is a compliance failure. The model was
#: wrong: it implies a case from six weeks ago is exactly as likely to be
#: outstanding as one from yesterday, and real queues do not behave that way.
#: Old items get chased, escalated and cleared.
#:
#: So the probability decays with age, plus a small floor for the genuinely
#: stuck ones — the case waiting on a document the applicant will never send.
#: Most of the queue is recent; a couple of stragglers are old, which is both
#: true to life and exactly what the red "waiting" styling on the desk is for.
BACKLOG_RECENT = 0.55      # a referral from today is probably still open
BACKLOG_HALFLIFE_DAYS = 3.0
BACKLOG_FLOOR = 0.015      # the genuinely stuck ones


def still_pending_probability(age_days: float) -> float:
    return BACKLOG_FLOOR + BACKLOG_RECENT * math.exp(-age_days / BACKLOG_HALFLIFE_DAYS)

#: The compliance team. Three accounts rather than one, because the desk's
#: "recently decided" list is the only place an officer's work is visible, and
#: a column of one name reads as a system with one user — which is exactly what
#: a reviewer suspects a demo of being.
#:
#: They are not interchangeable. Real teams have a senior who takes the hard
#: cases and is more willing to refuse, and a newer joiner who approves more and
#: writes shorter notes. That difference shows up in a decisions table, and its
#: absence is noticeable once you look for it.
OFFICERS = (
    ("staff:a.okonkwo@example.com", 0.62, 0.46),
    ("staff:m.lindqvist@example.com", 0.74, 0.34),
    ("staff:d.rahman@example.com", 0.83, 0.20),
)


def pick_officer(rng: random.Random) -> tuple[str, float, float]:
    """Who picked this case up. Weighted: the senior carries a heavier load."""
    return rng.choices(OFFICERS, weights=(0.42, 0.34, 0.24), k=1)[0]


#: What the vendor says, for applicants who finish.
#: Widened from five outcomes to eight. The document check is worth 0, 10, 20,
#: 30 or 40 points depending on this value, so a narrow outcome mix collapses a
#: whole dimension of the score — which is part of why every pending case used
#: to sit between 30 and 45.
VENDOR_OUTCOMES = (
    ["Approved"] * 82 + ["Declined"] * 5 + ["In Review"] * 4 +
    ["Abandoned"] * 3 + ["Expired"] * 2 + ["Awaiting User"] * 2 +
    ["Resubmitted"] * 1 + ["In Progress"] * 1
)


def seeded(details: dict) -> Jsonb:
    """Mark every fabricated audit row as fabricated."""
    return Jsonb({**details, "seeded": True})


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic applicants.")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--weeks", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260918)
    parser.add_argument(
        "--force",
        action="store_true",
        help="seed again even though seeded data is already present",
    )
    args = parser.parse_args()

    rng = random.Random(args.seed)
    today = date.today()

    index = load_index(SANCTIONS_SOURCE)
    # Individuals only, and only those carrying a date of birth. OFAC lists
    # vessels, aircraft and companies alongside people, and "SAAM FSU" as an
    # applicant name is the same tell as "HK LINK ASIA ELECTRONICS LIMITED".
    # A date of birth is the cleanest available signal for "this is a person",
    # and it is also what the downgrade mechanic below needs to exist.
    listed_entries = [
        e for e in index.entries if is_person_name(e.name) and e.date_of_birth
    ]
    print(f"screening against the {SANCTIONS_SOURCE} list ({len(index)} entries)")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from audit_events where details->>'seeded' = 'true'")
            already = cur.fetchone()[0]
        if already and not args.force:
            print(
                f"\n{already} seeded audit events are already present.\n"
                "Seeding again would double the history, and audit_events cannot\n"
                "be deleted — the append-only rule binds this script too. A real\n"
                "reset means dropping the schema and re-migrating:\n"
                '  docker compose exec -T db psql -U kyc -d kyc '
                '-c "drop schema public cascade; create schema public;"\n'
                "  python db/migrate.py up\n"
                "\nOr pass --force to add another batch on top.",
                file=sys.stderr,
            )
            return 1

        # Seeded screening points at a real snapshot too, so the demo data is
        # traceable in exactly the same way live data is.
        snapshot_id = ensure_snapshot(conn, index)

        arrivals = sample_arrivals(args.count, args.weeks, rng)
        counts = {"approved": 0, "rejected": 0, "referred_pending": 0,
                  "referred_decided": 0, "incomplete": 0}

        # Which arrivals get a borderline recipe. Chosen up front and spread
        # across the whole window, so the interesting cases are not all bunched
        # at one end of the history.
        borderline_count = int(args.count * BORDERLINE_SHARE)
        borderline_at = set(rng.sample(range(len(arrivals)), borderline_count))

        for index_of, created_at in enumerate(arrivals):
            recipe = (
                rng.choice(BORDERLINE_RECIPES) if index_of in borderline_at else None
            )
            applicant = make_applicant(rng, today, listed_entries, recipe)
            _seed_one(conn, rng, applicant, created_at, index, counts,
                      snapshot_id, recipe)

    print(f"\nseeded {args.count} applicants over {args.weeks} weeks")
    for key, value in counts.items():
        print(f"  {key:<20} {value:5d}")
    print("\nEvery audit row is marked \"seeded\": true.")
    return 0


def _seed_one(conn, rng, applicant, created_at, index, counts, snapshot_id,
              recipe=None) -> None:
    """One applicant's whole history, written in the order it happened.

    A `recipe` applicant skips the funnel drop-outs and takes a fixed vendor
    outcome, because the whole point of them is to reach the scorer. Their
    SCORE is still computed by the real scoring code — the recipe shapes the
    inputs, never the result.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into applications
                    (status, full_name, date_of_birth, address_line1, address_city,
                     address_postcode, address_country, created_at, updated_at)
                values ('started', %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (applicant.full_name, applicant.date_of_birth, applicant.line1,
                 applicant.city, applicant.postcode, applicant.country,
                 created_at, created_at),
            )
            application_id = cur.fetchone()[0]

        _audit(conn, application_id, created_at, "system:web", "application.created",
               {"channel": "web_form", "country": applicant.country})

        # --- did they even start? ---
        if recipe is None and rng.random() < DROP_NEVER_STARTED:
            counts["incomplete"] += 1
            return

        session_at = created_at + timedelta(
            seconds=lognormal_seconds(90, 0.9, rng)
        )
        session_id = _uuid(rng)
        _set_status(conn, application_id, "submitted", session_at, session_id)
        _audit(conn, application_id, session_at, "system:web",
               "verification.session_created",
               {"vendor": "didit", "session_id": session_id,
                "vendor_status": "Not Started", "status_to": "submitted"})

        if recipe is None and rng.random() < DROP_ABANDONED:
            counts["incomplete"] += 1
            return

        # --- the vendor takes a while, sometimes a long while ---
        vendor_at = session_at + timedelta(
            seconds=min(lognormal_seconds(6 * 60, 1.5, rng), 4 * 86400)
        )
        if vendor_at > datetime.now(timezone.utc):
            counts["incomplete"] += 1
            return

        if recipe is None and rng.random() < DROP_STILL_CHECKING:
            _set_status(conn, application_id, "checking", vendor_at, session_id,
                        vendor_status="In Review", vendor_result_at=vendor_at)
            _audit(conn, application_id, vendor_at, "system:worker", "status.changed",
                   {"from": "submitted", "to": "checking", "source": "webhook",
                    "vendor_status": "In Review"})
            counts["incomplete"] += 1
            return

        vendor_status = (
            recipe.vendor_status if recipe else rng.choice(VENDOR_OUTCOMES)
        )
        _set_status(conn, application_id, "screening", vendor_at, session_id,
                    vendor_status=vendor_status, vendor_result_at=vendor_at)
        _audit(conn, application_id, vendor_at, "system:worker", "status.changed",
               {"from": "submitted", "to": "screening", "source": "webhook",
                "vendor_status": vendor_status})

        # --- the REAL screening and the REAL scoring ---
        matches = index.search(
            applicant.full_name, date_of_birth=str(applicant.date_of_birth)
        )
        screened_at = vendor_at + timedelta(seconds=rng.uniform(0.4, 4.0))
        _store_matches(
            conn, application_id, index.source, matches, screened_at, snapshot_id
        )

        assessment = score_application(
            ApplicantProfile(
                country=applicant.country,
                vendor_status=vendor_status,
                hits=tuple(
                    ScreeningHit(
                        match_type=m.entry.entity_type,
                        list_name=m.entry.list_name,
                        matched_name=m.matched_name,
                        match_score=m.score,
                        entity_id=m.entry.entity_id,
                        date_of_birth_conflict=m.date_of_birth_conflict,
                    )
                    for m in matches
                ),
            )
        )

        stored = assessment.as_dict()
        stored["source"] = index.source
        stored["candidates_considered"] = len(matches)
        stored["sanctions_snapshot_id"] = snapshot_id
        with conn.cursor() as cur:
            cur.execute(
                """
                update applications
                   set risk_score = %s, risk_signals = %s,
                       risk_scored_at = %s, risk_ruleset_version = %s,
                       updated_at = %s
                 where id = %s
                """,
                (assessment.score, Jsonb(stored), screened_at,
                 assessment.ruleset_version, screened_at, application_id),
            )
        _audit(conn, application_id, screened_at, "system:worker",
               "screening.completed",
               {"score": assessment.score, "routing": assessment.routing,
                "source": index.source, "candidates": len(matches),
                "reasons": list(assessment.reasons)})

        reason = _automatic_reason(assessment)

        if assessment.routing == Routing.REVIEW:
            _decision(conn, application_id, "referred", "system", reason,
                      assessment.score, screened_at)
            _audit(conn, application_id, screened_at, "system:worker",
                   "decision.recorded",
                   {"outcome": "referred", "decided_by": "system",
                    "score": assessment.score, "reasons": list(assessment.reasons)})

            # A human picks it up during office hours — which is what makes the
            # median time-to-decision a real number. A Friday evening referral
            # genuinely waits until Monday.
            decided_at = next_office_moment(screened_at, rng)
            age_days = (datetime.now(timezone.utc) - screened_at).total_seconds() / 86400
            if (
                decided_at > datetime.now(timezone.utc)
                or rng.random() < still_pending_probability(age_days)
            ):
                counts["referred_pending"] += 1
                return

            officer, approve_rate, detail = pick_officer(rng)
            # A higher score is more likely to be refused. A flat rate meant a
            # 25 and a 75 were refused equally often, which makes the officers
            # look like they are ignoring the score in front of them.
            severity = min(max((assessment.score - 20) / 60.0, 0.0), 1.0)
            approve = rng.random() < approve_rate * (1.0 - 0.55 * severity)
            outcome = "approved" if approve else "rejected"
            _decision(conn, application_id, outcome, officer,
                      _human_reason(approve, assessment, rng, detail),
                      assessment.score, decided_at)
            _set_status(conn, application_id, "decided", decided_at, session_id,
                        vendor_status=vendor_status, vendor_result_at=vendor_at)
            _audit(conn, application_id, decided_at, officer,
                   "decision.recorded",
                   {"outcome": outcome, "decided_by": officer,
                    "automatic": False, "risk_score": assessment.score})
            counts["referred_decided"] += 1
            return

        outcome = "approved" if assessment.routing == Routing.APPROVE else "rejected"
        _decision(conn, application_id, outcome, "system", reason,
                  assessment.score, screened_at)
        _set_status(conn, application_id, "decided", screened_at, session_id,
                    vendor_status=vendor_status, vendor_result_at=vendor_at)
        _audit(conn, application_id, screened_at, "system:worker",
               "decision.recorded",
               {"outcome": outcome, "decided_by": "system", "automatic": True,
                "score": assessment.score, "reasons": list(assessment.reasons)})
        counts[outcome] += 1


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _uuid(rng: random.Random) -> str:
    hexes = "0123456789abcdef"
    part = lambda n: "".join(rng.choice(hexes) for _ in range(n))  # noqa: E731
    return f"{part(8)}-{part(4)}-4{part(3)}-a{part(3)}-{part(12)}"


def _set_status(conn, application_id, status, at, session_id, *,
                vendor_status=None, vendor_result_at=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update applications
               set status = %s, vendor_applicant_id = %s, updated_at = %s,
                   vendor_status = coalesce(%s, vendor_status),
                   vendor_result_at = coalesce(%s, vendor_result_at)
             where id = %s
            """,
            (status, session_id, at, vendor_status, vendor_result_at, application_id),
        )


def _audit(conn, application_id, at, actor, action, details) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into audit_events (application_id, occurred_at, actor, action, details)
            values (%s, %s, %s, %s, %s)
            """,
            (application_id, at, actor, action, seeded(details)),
        )


def _decision(conn, application_id, outcome, by, reason, score, at) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into decisions
                (application_id, outcome, decided_by, reason,
                 risk_score_at_decision, decided_at)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (application_id, outcome, by, reason, score, at),
        )


def _store_matches(conn, application_id, source, matches, at, snapshot_id) -> None:
    for match in matches:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into screening_results
                    (application_id, source, match_type, list_name, matched_name,
                     matched_entity_id, match_score, payload, screened_at,
                     snapshot_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (application_id, source, matched_entity_id)
                    where matched_entity_id is not null
                do nothing
                """,
                (application_id, source, match.entry.entity_type,
                 match.entry.list_name, match.matched_name, match.entry.entity_id,
                 round(match.score, 2),
                 Jsonb({"primary_name": match.entry.name,
                        "aliases": list(match.entry.aliases),
                        "countries": list(match.entry.countries),
                        "listed_date_of_birth": match.entry.date_of_birth,
                        "date_of_birth_conflict": match.date_of_birth_conflict}),
                 at, snapshot_id),
            )


def _automatic_reason(assessment) -> str:
    if not assessment.signals:
        return f"Automatic approval: risk score {assessment.score}, no risk signals found."
    return f"Risk score {assessment.score}. " + "; ".join(assessment.reasons) + "."


#: Written the way an officer writes: what was checked, what was concluded.
#:
#: This matters more than it looks. `decisions.reason` is NOT NULL and
#: constrained non-blank precisely so that an auditor sampling a case always
#: has something to read, and a demo whose reasons are all one sentence long
#: quietly undoes the argument the constraint was making. Thirty identical
#: sentences is exactly what an auditor looks for.
APPROVE_REASONS = [
    "Match is a different individual — listed date of birth and nationality both differ. Document check passed.",
    "Reviewed the list entry against the passport: different middle name and place of birth. Not the same person.",
    "Common name collision. Applicant's address history and date of birth are inconsistent with the listed entity.",
    "PEP match confirmed but the role ended over a decade ago; enhanced due diligence completed and source of funds evidenced.",
    "Name similarity only. No corroborating identifier matches. Cleared.",
    "Transliteration variant of a listed name. Date of birth is 14 years out and the listed entity has no UK connection.",
    "Applicant resubmitted a valid document on the second attempt. Identity confirmed, no list match of any strength.",
    "Country risk only — no adverse findings against the individual. Standard monitoring applied.",
    "Spoke to the applicant by phone and confirmed address history against the credit file. Satisfied this is a different person.",
    "Screening hit is against a vessel, not a natural person. Not applicable to this applicant.",
    "Listed entity is a company with a similar trading name. Applicant is an individual. No relationship found.",
]
REJECT_REASONS = [
    "Identity document could not be verified after two attempts and the applicant did not respond to follow-up.",
    "Listed date of birth and nationality both match the applicant. Treating as a positive match; escalated.",
    "Document check declined and the applicant is resident in a call-for-action jurisdiction. Refusing.",
    "Applicant abandoned verification twice and the name matches a sanctions entry. Insufficient basis to onboard.",
    "Date of birth, nationality and given name all align with the listed entity. No basis to treat as a coincidence.",
    "Applicant declined to provide source of funds after two requests. Cannot complete enhanced due diligence.",
    "Document images show signs of manipulation; vendor flagged and manual review agrees. Referred to MLRO.",
    "Address provided does not exist and the applicant could not be reached on the number given.",
]

#: Occasionally an officer adds a second line. Real notes are not uniform
#: length, and the variation is itself evidence that a person wrote them.
FOLLOW_UPS = [
    "Screenshots of the comparison saved to the case file.",
    "Second reviewer consulted; agreed.",
    "No further action required.",
    "Will re-screen at next periodic review.",
    "Flagged for monitoring rather than refusal.",
    "MLRO notified.",
]


def _human_reason(approve: bool, assessment, rng: random.Random,
                  detail: float = 0.35) -> str:
    """One officer's note on one case.

    `detail` is the officer's house style — how often they add a second line.
    A team whose notes are all the same length is a team of one.
    """
    pool = APPROVE_REASONS if approve else REJECT_REASONS
    note = f"{rng.choice(pool)} (score {assessment.score})"
    if rng.random() < detail:
        note = f"{note} {rng.choice(FOLLOW_UPS)}"
    return note


if __name__ == "__main__":
    raise SystemExit(main())
