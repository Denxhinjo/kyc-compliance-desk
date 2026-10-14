"""The continuous seeder: a few synthetic applicants an hour, through the real pipeline.

    python trickle.py                 # one tick, 0-3 applicants
    python trickle.py --count 2       # exactly two, for a manual demo
    python trickle.py --dry-run       # generate and print, send nothing

WHY THIS IS SEPARATE FROM seed.py

`seed.py` builds a BACKDATED history: it spreads N applicants over the previous
N weeks and writes the whole arc of each case directly, which is the right tool
for "make this demo look like it has been running for a month". It also refuses
to run twice without --force, because audit_events is append-only and a second
run would double the history. That guard is correct and this file does not go
near it.

This does the opposite job: a steady trickle, now, forwards. Nothing is
backdated and nothing is written directly.

WHAT THIS FILE DOES AND DOES NOT DO

It generates synthetic people and posts them to /api/seed/tick. That is all. It
holds no database credential and executes no part of the pipeline itself:

    creation            lib/applications.ts   -- the apply form's own code
    verification        lib/verification.ts   -- the verify page's own code
    the vendor result   lib/mock-vendor.ts    -- a real signed webhook, POSTed
                                                 to /api/webhooks/didit
    screening/scoring   worker/screening_handler.py, under whichever rules
                        version is in force at that moment
    decisions           lib/decisions.ts      -- the review desk's own code

The division is deliberate. Everything that could be subtly wrong runs as
production code; what lives here is only the invention of synthetic people,
which is the one thing production has no opinion about.

NAMES COME FROM seed.py AND NOWHERE ELSE. No new name sources, and no new real
names: `make_applicant` is called with an empty list of listed entries, so
every name is generated from the culture name lists rather than drawn from the
sanctions list.

That has a visible consequence worth stating plainly: trickle applicants do not
produce sanctions-match referrals. Their referrals come from country risk and
declined document checks, which is a narrower mix than the bulk seeder's.
Drawing names from the live OFAC list would fix it and would mean adding real
people's names to the data, which this round was told not to do.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import date

# The whole point: the synthetic sources are seed.py's, imported rather than
# copied. A second set of name lists is a second set to drift.
from seed import (
    APPROVE_REASONS,
    FOLLOW_UPS,
    REJECT_REASONS,
    make_applicant,
)

#: How many applicants a tick creates, and how often.
#:
#: Weighted to average 1.15 per run. At hourly that is about 28 a day, inside
#: the 20-40 asked for, and the zero is what stops the arrival pattern looking
#: like a metronome -- a real day has quiet hours.
PER_TICK_WEIGHTS: dict[int, float] = {0: 0.30, 1: 0.35, 2: 0.25, 3: 0.10}

#: Matches MAX_APPLICANTS_PER_TICK in the route, which refuses more.
MAX_PER_TICK = 5

#: How many reviewer notes to offer the synthetic reviewer per tick. It picks
#: from these; sending a few keeps consecutive decisions from reading alike.
REASONS_PER_TICK = 6

TIMEOUT_SECONDS = 30


def applicants_for_this_tick(rng: random.Random) -> int:
    counts = list(PER_TICK_WEIGHTS)
    weights = [PER_TICK_WEIGHTS[c] for c in counts]
    return rng.choices(counts, weights=weights, k=1)[0]


def reviewer_notes(rng: random.Random, approve: bool) -> list[str]:
    """Notes in the voice seed.py established, from seed.py's own lists.

    `_human_reason` is not reused directly because it takes a scoring
    assessment to quote the score from, and this process deliberately has no
    assessment -- the score belongs to the worker, which has already computed
    it. The follow-up line is kept because uniform-length notes are exactly
    what the comment on those lists warns about.
    """
    pool = APPROVE_REASONS if approve else REJECT_REASONS
    chosen = rng.sample(pool, k=min(REASONS_PER_TICK, len(pool)))
    notes = []
    for note in chosen:
        if rng.random() < 0.35:
            note = f"{note} {rng.choice(FOLLOW_UPS)}"
        notes.append(note)
    return notes


#: The public form's own bounds, from web/src/lib/validation.ts.
MIN_AGE = 18
MAX_AGE = 120

#: A cap on resampling, so a bad distribution cannot spin forever.
MAX_DOB_ATTEMPTS = 40


def whole_years(born: date, today: date) -> int:
    """Age the way the form counts it, which is the way that matters here.

    Mirrors `ageOn` in web/src/lib/validation.ts: whole years, decremented
    when the birthday has not yet come round this year. Approximating with
    days/365.25 disagrees with it by a year for anyone born within a few days
    of today's date, which is exactly the boundary this guards.
    """
    age = today.year - born.year
    if (today.month, today.day) < (born.month, born.day):
        age -= 1
    return age


def adult_applicant(rng: random.Random, today: date):
    """One synthetic person the public form would actually accept.

    WHY THIS FILTER EXISTS. seed.py's `sample_date_of_birth` clamps the SAMPLED
    age to 18 and then picks a day within that calendar year, so an applicant
    sampled at 18 is born anywhere in it — and someone born late in that year
    is still 17 by the form's whole-year rule. Roughly a fifth of age-18
    samples are therefore under age on the day they are generated.

    That never mattered before because the bulk seeder writes applications
    straight to the database, bypassing the validator entirely. This path goes
    THROUGH the validator, which is the point of it, so the mismatch surfaces
    here: the route refuses the applicant, reports it in `rejected_input`, and
    trickle.py exits non-zero — turning a scheduled run red for no better
    reason than an off-by-a-few-months birthday.

    Resampling rather than correcting the date: nudging the year would bias the
    age distribution at its most populated end, and the distribution is one of
    the things seed.py was careful about.
    """
    for _ in range(MAX_DOB_ATTEMPTS):
        applicant = make_applicant(rng, today, [], None)
        if MIN_AGE <= whole_years(applicant.date_of_birth, today) <= MAX_AGE:
            return applicant
    raise RuntimeError(
        f"could not generate an applicant aged {MIN_AGE}-{MAX_AGE} in "
        f"{MAX_DOB_ATTEMPTS} attempts — has sample_date_of_birth changed?"
    )


def generate(count: int, rng: random.Random) -> list[dict[str, str]]:
    """Synthetic applicants, in the shape the apply form's validator expects.

    `listed_entries=[]` is what keeps this free of real names, and it is also
    what keeps the tick cheap: loading the sanctions list to invent a name
    would mean a 6 MB download every hour for a job that creates one applicant.
    `make_applicant` guards on the list being non-empty and falls back to the
    culture name lists.
    """
    today = date.today()
    out = []
    for _ in range(count):
        applicant = adult_applicant(rng, today)
        fields = asdict(applicant)
        out.append(
            {
                # The route builds a FormData from these and runs the SAME
                # validator the public form uses, so the keys are the form's
                # field names rather than the dataclass's. seed.Applicant has
                # no second address line; the form accepts it as optional.
                "fullName": fields["full_name"],
                "dateOfBirth": fields["date_of_birth"].isoformat(),
                "addressLine1": fields["line1"],
                "addressLine2": "",
                "addressCity": fields["city"],
                "addressPostcode": fields["postcode"],
                "addressCountry": fields["country"],
            }
        )
    return out


def post_tick(base_url: str, secret: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/seed/tick",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-seed-secret": secret,
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="exactly this many applicants instead of the weighted 0-3",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="generate and print the payload without sending it",
    )
    parser.add_argument("--seed", type=int, default=None, help="for a reproducible tick")
    args = parser.parse_args()

    rng = random.Random(args.seed)

    count = args.count if args.count is not None else applicants_for_this_tick(rng)
    if count < 0 or count > MAX_PER_TICK:
        print(f"--count must be between 0 and {MAX_PER_TICK}", file=sys.stderr)
        return 2

    payload = {
        "applicants": generate(count, rng),
        "approve_reasons": reviewer_notes(rng, approve=True),
        "reject_reasons": reviewer_notes(rng, approve=False),
    }

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        print(f"\n{count} applicant(s) generated; nothing sent (--dry-run)")
        return 0

    # FAIL CLOSED AND LOUD. A trickle that silently does nothing is the failure
    # this is partly built to make visible, so an unset variable is an error
    # with a non-zero exit, not a quiet skip. The workflow then goes red.
    base_url = os.environ.get("PUBLIC_BASE_URL") or ""
    secret = os.environ.get("DRAIN_SECRET") or ""
    missing = [
        name
        for name, value in (("PUBLIC_BASE_URL", base_url), ("DRAIN_SECRET", secret))
        if not value
    ]
    if missing:
        print(f"not set: {', '.join(missing)}", file=sys.stderr)
        return 1

    try:
        result = post_tick(base_url, secret, payload)
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")[:300]
        print(f"seed tick failed: HTTP {err.code}: {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as err:
        print(f"seed tick failed: could not reach {base_url}: {err.reason}", file=sys.stderr)
        return 1

    print(
        f"tick: requested {count}, created {result.get('created')}, "
        f"reviewed {result.get('reviewed')}, "
        f"review skipped {result.get('review_skipped')}"
    )
    rejected = result.get("rejected_input") or []
    if rejected:
        # The route validates with the public form's validator. Anything
        # refused means this file generated a person the form would not accept,
        # which is a bug here and should be noisy.
        print(f"REFUSED BY THE VALIDATOR: {json.dumps(rejected)[:400]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
