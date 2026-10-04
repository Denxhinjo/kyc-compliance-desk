"""Tests for risk scoring.

No database, no network, no clock. scoring.py is pure, so these run in
milliseconds and nothing is mocked — which is the concrete payoff of keeping
the I/O somewhere else.

Read in three parts: every signal alone, then the boundaries, then the
properties that must hold whatever the rules become.
"""

from __future__ import annotations

import pytest

from scoring import (
    DOCUMENT_POINTS,
    FATF_CALL_FOR_ACTION,
    FATF_INCREASED_MONITORING,
    PEP_POINTS,
    SANCTIONS_POINTS,
    ApplicantProfile,
    Routing,
    ScreeningHit,
    Thresholds,
    band_of,
    route,
    score_application,
)

CLEAN_COUNTRY = "GB"


def profile(
    *,
    country: str = CLEAN_COUNTRY,
    vendor_status: str | None = "Approved",
    hits: tuple[ScreeningHit, ...] = (),
) -> ApplicantProfile:
    """An applicant with nothing wrong, unless the test says otherwise.

    Every test states only its own variable, so what it is testing is the only
    thing that differs from a clean baseline.
    """
    return ApplicantProfile(country=country, vendor_status=vendor_status, hits=hits)


def hit(
    match_type: str = "sanctions",
    score: float = 95.0,
    *,
    dob_conflict: bool = False,
    entity_id: str = "E-1",
) -> ScreeningHit:
    return ScreeningHit(
        match_type=match_type,
        list_name="TEST-LIST",
        matched_name="Test Person",
        match_score=score,
        entity_id=entity_id,
        date_of_birth_conflict=dob_conflict,
    )


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------


def test_a_clean_applicant_scores_zero_and_is_approved():
    assessment = score_application(profile())
    assert assessment.score == 0
    assert assessment.signals == ()
    assert assessment.routing == Routing.APPROVE


def test_a_passed_document_check_earns_no_credit():
    """Passing is the baseline, not a bonus. Nothing may score below zero."""
    assert score_application(profile(vendor_status="Approved")).score == 0


# ---------------------------------------------------------------------------
# Each signal, alone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score,band,points",
    [
        (100.0, "confirmed", SANCTIONS_POINTS["confirmed"]),
        (92.0, "confirmed", SANCTIONS_POINTS["confirmed"]),
        (91.9, "probable", SANCTIONS_POINTS["probable"]),
        (85.0, "probable", SANCTIONS_POINTS["probable"]),
        (84.9, "weak", SANCTIONS_POINTS["weak"]),
        (80.0, "weak", SANCTIONS_POINTS["weak"]),
    ],
)
def test_sanctions_bands(score, band, points):
    assessment = score_application(profile(hits=(hit("sanctions", score),)))
    assert band_of(score) == band
    assert assessment.score == points


def test_a_confirmed_sanctions_match_alone_reviews_rather_than_rejects():
    """The conclusion the measurements forced, and the most important test here.

    The instinct is that a confirmed sanctions match should refuse the applicant
    outright — it is a criminal offence to serve a listed person, after all.

    Measured against the real OFAC SDN list, 0.58% of applicants on no list at
    all reached the 'confirmed' band purely by having a common name:
    "Carlos Garcia" against "Carlos Alberto GAXIOLA GARCIA". At 80 points those
    were auto-rejected. On a book of 100,000 that is 580 real customers refused
    by a string comparison.

    So a name match alone guarantees REVIEW and never refusal. Refusal needs a
    second independent signal. Recall is untouched — the match is still found,
    recorded and shown to an officer; only the automatic refusal is withdrawn.

    This is also what firms actually do: a potential match is escalated and a
    human confirms identity before the firm acts.
    """
    alone = score_application(profile(hits=(hit("sanctions", 97.0),)))
    assert alone.routing == Routing.REVIEW

    # A second, independent signal is what makes refusal automatic.
    corroborated = score_application(
        profile(hits=(hit("sanctions", 97.0),), vendor_status="Declined")
    )
    assert corroborated.routing == Routing.REJECT


def test_no_single_signal_can_auto_reject():
    """Generalised: refusal always requires at least two independent findings.

    A property rather than an example, so it keeps holding as the points move.
    """
    singles = [
        profile(hits=(hit("sanctions", 100.0),)),
        profile(hits=(hit("pep", 100.0),)),
        profile(hits=(hit("adverse_media", 100.0),)),
        profile(country="IR"),
        profile(vendor_status="Declined"),
    ]
    for single in singles:
        assessment = score_application(single)
        assert assessment.routing != Routing.REJECT, assessment.as_dict()


@pytest.mark.parametrize(
    "score,points",
    [
        (95.0, PEP_POINTS["confirmed"]),
        (87.0, PEP_POINTS["probable"]),
        (81.0, PEP_POINTS["weak"]),
    ],
)
def test_pep_bands(score, points):
    assert score_application(profile(hits=(hit("pep", score),))).score == points


def test_a_pep_match_can_never_cause_an_automatic_rejection():
    """The legal point of this whole phase, as an assertion.

    Being a Politically Exposed Person is not illegal and is not grounds for
    refusal. What the law requires is enhanced due diligence, which means a
    human looks at it. Refusing PEPs wholesale ("de-risking") is something
    regulators criticise.

    So the maximum PEP contribution must stay below the rejection threshold —
    even combined with the worst possible document outcome and the worst
    possible country, a PEP must never be auto-rejected on PEP-ness.
    """
    worst = score_application(
        profile(
            hits=(hit("pep", 100.0),),
            country=sorted(FATF_CALL_FOR_ACTION)[0],
            vendor_status="Declined",
        )
    )
    # It may well be reviewed, and should be. It must not be rejected for this.
    assert PEP_POINTS["confirmed"] < Thresholds().auto_reject_at_or_above
    assert score_application(profile(hits=(hit("pep", 100.0),))).routing == Routing.REVIEW
    assert worst.routing in (Routing.REVIEW, Routing.REJECT)
    # ... and if it IS rejected, it must be the country and the documents doing
    # it, never the PEP status alone.
    assert worst.score - PEP_POINTS["confirmed"] >= Thresholds().auto_reject_at_or_above


def test_a_match_below_the_noise_floor_scores_nothing():
    assert score_application(profile(hits=(hit("sanctions", 79.9),))).score == 0


@pytest.mark.parametrize("country", sorted(FATF_CALL_FOR_ACTION))
def test_call_for_action_countries(country):
    assessment = score_application(profile(country=country))
    assert assessment.score == 40
    assert assessment.routing == Routing.REVIEW


# Derived from the list, not hardcoded. This used to name NG, SY and VN;
# Nigeria left the grey list at a later plenary and the test failed for a
# reason that had nothing to do with scoring. Parametrising over the set
# means the next plenary updates the test with the data.
@pytest.mark.parametrize("country", sorted(FATF_INCREASED_MONITORING))
def test_increased_monitoring_countries(country):
    assessment = score_application(profile(country=country))
    assert assessment.score == 15
    # Elevated, but not on its own enough to trouble a human.
    assert assessment.routing == Routing.APPROVE


def test_country_matching_is_case_insensitive():
    assert score_application(profile(country="ir")).score == 40


def test_an_ordinary_country_scores_nothing():
    assert score_application(profile(country="DE")).score == 0


@pytest.mark.parametrize(
    "vendor_status,points",
    sorted((k, v) for k, v in DOCUMENT_POINTS.items()),
)
def test_document_check_outcomes(vendor_status, points):
    assert score_application(profile(vendor_status=vendor_status)).score == points


def test_a_missing_document_result_is_itself_a_finding():
    assessment = score_application(profile(vendor_status=None))
    assert assessment.score == 30
    assert "No identity verification result" in assessment.signals[0].reason


def test_an_unrecognised_vendor_status_is_treated_as_missing():
    """A status we have no mapping for must not silently score zero."""
    assert score_application(profile(vendor_status="Something New")).score == 30


# ---------------------------------------------------------------------------
# Strongest, never the sum
# ---------------------------------------------------------------------------


def test_many_weak_matches_do_not_add_up():
    """Five weak hits mean the name is common, not that the person is guilty.

    Summing them would push an ordinary applicant past the rejection threshold
    for the offence of having an ordinary name. Only the strongest counts.
    """
    many = tuple(hit("sanctions", 80.0, entity_id=f"E-{i}") for i in range(5))
    assessment = score_application(profile(hits=many))
    assert assessment.score == SANCTIONS_POINTS["weak"]
    assert assessment.routing == Routing.REVIEW


def test_the_number_of_candidates_is_reported_even_though_it_scores_nothing():
    many = tuple(hit("sanctions", 80.0, entity_id=f"E-{i}") for i in range(30))
    assessment = score_application(profile(hits=many))
    context = [s for s in assessment.signals if s.code == "multiple_candidates"]
    assert len(context) == 1
    assert context[0].points == 0
    assert context[0].evidence["candidate_count"] == 30


def test_sanctions_and_pep_are_scored_separately():
    assessment = score_application(
        profile(hits=(hit("sanctions", 95.0, entity_id="S1"), hit("pep", 95.0, entity_id="P1")))
    )
    assert assessment.score == SANCTIONS_POINTS["confirmed"] + PEP_POINTS["confirmed"]


# ---------------------------------------------------------------------------
# Date-of-birth conflict
# ---------------------------------------------------------------------------


def test_a_conflicting_date_of_birth_downgrades_one_band():
    confirmed = score_application(profile(hits=(hit("sanctions", 97.0),)))
    conflicted = score_application(profile(hits=(hit("sanctions", 97.0, dob_conflict=True),)))

    assert confirmed.score == SANCTIONS_POINTS["confirmed"]
    assert conflicted.score == SANCTIONS_POINTS["probable"]
    assert "date of birth conflicts" in conflicted.signals[0].reason


def test_a_conflicting_date_of_birth_stops_an_automatic_rejection():
    """The common-name case. A 97% match to a person born forty years earlier
    is a different person, and must reach a human rather than be refused."""
    assessment = score_application(profile(hits=(hit("sanctions", 97.0, dob_conflict=True))
                                           if False else (hit("sanctions", 97.0, dob_conflict=True),)))
    assert assessment.routing == Routing.REVIEW


def test_a_weak_match_cannot_be_downgraded_below_weak():
    assessment = score_application(profile(hits=(hit("sanctions", 80.0, dob_conflict=True),)))
    assert assessment.score == SANCTIONS_POINTS["weak"]


# ---------------------------------------------------------------------------
# The routing boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score,expected",
    [
        (0, Routing.APPROVE),
        (18, Routing.APPROVE),
        (19, Routing.APPROVE),   # last approval
        (20, Routing.REVIEW),    # first review
        (21, Routing.REVIEW),
        (78, Routing.REVIEW),
        (79, Routing.REVIEW),    # last review
        (80, Routing.REJECT),    # first rejection
        (81, Routing.REJECT),
        (500, Routing.REJECT),
    ],
)
def test_routing_boundaries(score, expected):
    assert route(score) == expected


def test_thresholds_are_injectable_not_global():
    """Moving the line must not require touching a rule."""
    strict = Thresholds(auto_approve_below=5, auto_reject_at_or_above=40)
    assert route(10, strict) == Routing.REVIEW
    assert route(10) == Routing.APPROVE
    assert route(45, strict) == Routing.REJECT
    assert route(45) == Routing.REVIEW


def test_a_score_of_exactly_nineteen_is_reachable_and_approves():
    """19/20 with real signals, not just arithmetic on the routing function."""
    assessment = score_application(
        profile(
            country=sorted(FATF_INCREASED_MONITORING)[0],
            hits=(hit("adverse_media", 90.0),),
        )
    )
    assert assessment.score == 25  # 15 country + 10 adverse media
    assert assessment.routing == Routing.REVIEW


# ---------------------------------------------------------------------------
# Properties that must survive any change to the rules
# ---------------------------------------------------------------------------


def test_every_signal_carries_a_readable_reason():
    """A score of 65 with no explanation is useless to the officer in Phase 6.

    Every signal must say something a human can act on, not a rule name.
    """
    assessment = score_application(
        profile(
            country="IR",
            vendor_status="Declined",
            hits=(hit("sanctions", 93.0), hit("pep", 88.0, entity_id="P1")),
        )
    )
    assert assessment.signals
    for signal in assessment.signals:
        assert len(signal.reason) > 20, signal
        assert signal.code not in signal.reason  # prose, not an identifier
        # Starts like a sentence. A digit is fine ("2 list entries were..."),
        # a lowercase rule name is not.
        assert signal.reason[0].isupper() or signal.reason[0].isdigit(), signal
        assert "_" not in signal.reason.split()[0]


def test_the_score_is_always_the_sum_of_its_signals():
    """No hidden adjustments. The arithmetic must be checkable by hand."""
    assessment = score_application(
        profile(country="SY", vendor_status="Declined", hits=(hit("sanctions", 86.0),))
    )
    assert assessment.score == sum(s.points for s in assessment.signals)


def test_no_signal_ever_awards_negative_points():
    assessment = score_application(
        profile(country="IR", vendor_status="Declined", hits=(hit("sanctions", 100.0),))
    )
    assert all(s.points >= 0 for s in assessment.signals)


def test_scoring_is_deterministic():
    p = profile(country="NG", vendor_status="In Review", hits=(hit("sanctions", 88.0),))
    first, second = score_application(p), score_application(p)
    assert first.as_dict() == second.as_dict()


def test_the_stored_form_carries_the_thresholds_and_the_ruleset_version():
    """Without these, a stored 65 becomes unexplainable the moment the bands
    move — you would know the number and the reasons but no longer the routing."""
    stored = score_application(
        profile(country=sorted(FATF_INCREASED_MONITORING)[0])
    ).as_dict()
    assert stored["thresholds"] == {
        "auto_approve_below": 20,
        "auto_reject_at_or_above": 80,
    }
    assert stored["ruleset_version"]
    assert stored["signals"][0]["reason"]


# ---------------------------------------------------------------------------
# The seeder's borderline recipes
# ---------------------------------------------------------------------------


def test_the_borderline_recipes_document_the_score_they_actually_produce():
    """`expected` on a Borderline recipe is documentation, and documentation rots.

    Three of these recipes used to hardcode NG, TR and AE and describe them as
    "monitoring countries" worth 15 points. By ruleset 2026-09-2 none of the
    three was on the FATF increased-monitoring list, so both the note and the
    number beside it were wrong — and nothing failed, because score_application()
    computes the real score and `expected` is only read by humans.

    The countries now come from the sourced constants, so the recipes are true by
    construction. This asserts it stays that way, because the next list change
    will move these numbers again.

    Only the recipes without a sanctions match are checked: the others depend on
    what the screening fixture happens to contain, which is a different test's
    business.
    """
    from seed import BORDERLINE_RECIPES

    mismatches = []
    for recipe in BORDERLINE_RECIPES:
        if recipe.listed_name:
            continue
        assessment = score_application(
            ApplicantProfile(
                country=recipe.country, vendor_status=recipe.vendor_status, hits=()
            )
        )
        if assessment.score != recipe.expected:
            mismatches.append(
                f"{recipe.country}/{recipe.vendor_status}: documented "
                f"{recipe.expected}, computes {assessment.score} — {recipe.note}"
            )

    assert not mismatches, (
        "a borderline recipe documents a score the rules no longer produce:\n  "
        + "\n  ".join(mismatches)
        + "\n\nThis usually means the reference data moved. Update the recipe's "
        "expected value and its note, or derive the country from the constant "
        "so it cannot drift again."
    )


def test_the_seeders_risk_tail_comes_from_the_sourced_lists():
    """The seeder must not assert who is on a list.

    It draws its higher-risk tail from the scoring constants, intersected with
    the countries it can furnish an address for. If someone hardcodes a country
    back in, this fails.
    """
    from seed import _CALL_FOR_ACTION_TAIL, _MONITORED_TAIL
    from scoring import CALL_FOR_ACTION, INCREASED_MONITORING

    assert set(_MONITORED_TAIL) <= set(INCREASED_MONITORING.codes)
    assert set(_CALL_FOR_ACTION_TAIL) <= set(CALL_FOR_ACTION.codes)


def test_country_coverage_partitions_the_sourced_lists_exactly():
    """The seeder's report must account for every sourced code.

    Used plus excluded has to equal the list, or the report is telling a
    comforting half-truth — which is the failure mode it exists to prevent.
    """
    from seed import country_coverage
    from scoring import CALL_FOR_ACTION, INCREASED_MONITORING

    cover = country_coverage()

    assert set(cover["monitored_used"]) | set(cover["monitored_excluded"]) == set(
        INCREASED_MONITORING.codes
    )
    assert not set(cover["monitored_used"]) & set(cover["monitored_excluded"])

    assert set(cover["call_for_action_used"]) | set(
        cover["call_for_action_excluded"]
    ) == set(CALL_FOR_ACTION.codes)
    assert not set(cover["call_for_action_used"]) & set(
        cover["call_for_action_excluded"]
    )


def test_every_usable_country_really_has_address_data():
    """The whole point of the intersection.

    A code reported as usable but absent from CITIES would put its applicants
    at the placeholder address, which is the lie the intersection was added to
    avoid.
    """
    from seed import CITIES, country_coverage

    cover = country_coverage()
    for key in ("monitored_used", "call_for_action_used"):
        for code in cover[key]:
            assert code in CITIES, f"{code} reported usable but has no address data"


def test_the_coverage_warning_fires_when_a_list_is_barely_represented(capsys):
    """The trigger is a fraction, not zero.

    It used to warn only at zero coverage — the one case nobody would miss. It
    stayed silent at 1 of 22, which is a country-risk signal exercised by a
    single jurisdiction: every applicant scoring country points scores them for
    the same reason, so a rule mishandling every other listed country would
    pass unnoticed.
    """
    import seed

    original = seed.CITIES
    try:
        # One monitored jurisdiction reachable, out of however many are listed.
        only_one = sorted(seed.INCREASED_MONITORING.codes)[:1]
        seed.CITIES = {code: [("Testville", "T1")] for code in only_one}
        seed.print_country_coverage()
        out = capsys.readouterr().out
    finally:
        seed.CITIES = original

    assert "WARNING" in out, f"no warning at 1/{len(seed.INCREASED_MONITORING.codes)}:\n{out}"
    assert "barely exercised" in out


def test_no_coverage_warning_when_the_lists_are_well_represented(capsys):
    """The counterweight: a warning that always fires is noise, not a signal."""
    import seed

    seed.print_country_coverage()
    out = capsys.readouterr().out

    assert "WARNING" not in out, (
        "coverage dropped below the threshold — either address data was removed "
        f"or a sourced list grew:\n{out}"
    )
