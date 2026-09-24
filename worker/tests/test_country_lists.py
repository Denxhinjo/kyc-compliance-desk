"""The FATF lists are reference data, and reference data needs provenance.

WHY THIS FILE EXISTS

The sanctions list carries a source, a publication date, a record count, a
SHA-256 and a load timestamp, and every screening result points at the
snapshot it used. The country lists carried a comment saying "as at 2026-09".

That asymmetry is not wrong in itself — 24 country codes do not need a
trigram index, and a change to a legal list is better as a reviewable commit
than as an UPDATE nobody sees. What was wrong is that the provenance was
prose. Nothing recorded which FATF revision a decision had been scored
against, and nothing stopped the codes changing while the ruleset version
stayed put.

These tests are the mechanism that keeps it honest. They do not check that
the lists are *correct* — no test can, because correctness lives at
fatf-gafi.org and changes three times a year. They check that the lists
cannot change *quietly*.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest

from scoring import COUNTRY_LISTING, ApplicantProfile, score_application

#: The digest of the codes as they currently stand.
#:
#: A failure here is not a bug. It means somebody edited a list, which is a
#: legitimate thing to do three times a year — and the point is that they
#: cannot do it without also coming here, restating where the new codes came
#: from, and updating published_at. That is the whole mechanism: the lists may
#: change, but not silently, and not without re-answering "sourced from what?"
PINNED_DIGEST = "fda64737206e5dae011e10f68c5167ae6238bc0ba48fe59fc7d916bf1b756d9e"


def test_the_lists_cannot_change_without_this_test_noticing():
    assert COUNTRY_LISTING.digest == PINNED_DIGEST, (
        "the FATF country lists changed.\n\n"
        "If that was deliberate: update PINNED_DIGEST here, set "
        "COUNTRY_LISTING.published_at to the publication date of the FATF "
        "plenary the new codes come from, and rewrite .provenance to say so. "
        "Bump RULESET_VERSION too — decisions scored before and after are not "
        "comparable.\n\n"
        "If it was not deliberate, someone has changed who gets flagged."
    )


def test_an_unsourced_list_says_so_rather_than_inventing_a_date():
    """The rule the whole type exists for.

    A manufactured publication date is worse than an absent one: it claims an
    authority it does not have, and it survives review precisely because it
    looks right. So the invariant is not "there must be a date" — it is "if
    there is no date, the data must say why, in the data".
    """
    if COUNTRY_LISTING.published_at is None:
        assert COUNTRY_LISTING.provenance.strip(), (
            "published_at is null and provenance is empty — that is an "
            "unexplained gap rather than a declared one"
        )
        assert "UNSOURCED" in COUNTRY_LISTING.provenance, (
            "published_at is null, so provenance must say plainly that these "
            "codes are not traceable to a published revision. A reader "
            "skimming a decision record should not have to infer it from an "
            "absent field."
        )


def test_a_publication_date_if_present_is_a_real_date():
    """Guards the other direction, for when the lists are sourced properly.

    Without this, "2026-09" or "June 2024 plenary" would pass silently and the
    field would stop being machine-readable the moment somebody filled it in
    helpfully.
    """
    published = COUNTRY_LISTING.published_at
    if published is None:
        pytest.skip("lists are declared unsourced; nothing to validate")

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", published), (
        f"published_at is {published!r}; it must be a full ISO-8601 date, "
        "because it is stored on every decision and read back by machines"
    )
    parsed = dt.date.fromisoformat(published)
    assert parsed <= dt.date.today(), (
        f"published_at is {published}, which is in the future — FATF cannot "
        "have published a revision that has not happened"
    )


def test_no_country_is_on_both_lists():
    """They carry different points, so an overlap makes scoring order-dependent.

    Nothing in FATF's own publication would produce this — a jurisdiction is
    subject to a call for action or to increased monitoring, not both — so an
    overlap means a hand-edit went wrong.
    """
    overlap = COUNTRY_LISTING.call_for_action & COUNTRY_LISTING.increased_monitoring
    assert not overlap, (
        f"{sorted(overlap)} appear on both lists. Scoring checks call-for-"
        "action first, so these would score 40 and the monitoring branch "
        "would never run — a silent precedence rather than a stated one."
    )


@pytest.mark.parametrize(
    "codes",
    [COUNTRY_LISTING.call_for_action, COUNTRY_LISTING.increased_monitoring],
    ids=["call_for_action", "increased_monitoring"],
)
def test_every_entry_is_an_iso_3166_alpha_2_code(codes):
    """Applications store a two-letter country, so a three-letter code here
    would never match anything and would fail open — the applicant scores
    nothing and nobody sees why."""
    malformed = sorted(c for c in codes if not re.fullmatch(r"[A-Z]{2}", c))
    assert not malformed, (
        f"{malformed} are not ISO-3166 alpha-2 codes. These are compared "
        "against applications.address_country, so a malformed entry silently "
        "matches no one."
    )


def test_the_provenance_reaches_the_decision_record():
    """The point of the exercise: it has to be ON the decision, not near it.

    An auditor reading one stored assessment should be able to answer "which
    list version was this scored against" without reading the source at the
    commit that produced it.
    """
    assessment = score_application(
        ApplicantProfile(country="IR", vendor_status="Approved", hits=())
    )
    recorded = assessment.as_dict()["country_list"]

    assert recorded["digest"] == COUNTRY_LISTING.digest
    assert recorded["published_at"] == COUNTRY_LISTING.published_at
    assert recorded["provenance"] == COUNTRY_LISTING.provenance
    assert recorded["source_url"].startswith("https://")


def test_it_is_recorded_even_when_no_country_signal_fires():
    """"Both lists were consulted and this country was on neither" is a finding.

    If the provenance were only attached when a country scored, an auditor
    asking why a jurisdiction was NOT flagged would have nothing to read —
    which is the question that actually gets asked after something goes wrong.
    """
    assessment = score_application(
        ApplicantProfile(country="GB", vendor_status="Approved", hits=())
    )
    assert not any(s.code.startswith("country") for s in assessment.signals)
    assert assessment.as_dict()["country_list"]["digest"] == COUNTRY_LISTING.digest
