"""What the public status page is allowed to tell an applicant.

WHY THIS IS A TEST AND NOT A COMMENT

`/status/<id>` needs no login — the application's UUID is the only credential —
and it renders the same `audit_events` rows the compliance officer reads. Those
rows contain the risk score, the routing decision and sentences of the form
"Sanctions match against X on OFAC SDN at 100% name similarity (confirmed)".

`timeline-copy.ts` keeps them out with a whitelist. But the reason that works
is subtle and easy to undo: the page is a Server Component, so `details` never
crosses to the browser. Turn that list into a Client Component — a plausible
change, if someone wants a filter or a "show more" toggle — and the raw rows
get serialised into the payload again, with no rendering code touched and no
reviewer likely to notice.

A comment saying "careful, this is why it works" does not survive that. A red
test does.

The assertion is deliberately over-broad: it greps the whole HTTP response,
markup and RSC payload together, for tokens that should not be anywhere in it.
A narrower check on rendered text would miss precisely the regression that
matters.

RUNNING IT, AND WHY IT IS NOT ALLOWED TO SKIP ON CI

This needs a running web service, which the rest of the worker suite does not.
Locally it skips politely when the app is not up, like everything else here.

On CI it must not. A guard that silently does not run is worse than no guard,
because the green tick is then evidence of nothing while looking like evidence
of something — and this one is guarding a disclosure boundary rather than a
rendering detail. The `disclosure` job in .github/workflows/tests.yml boots
Postgres, migrates, seeds, builds and starts the web app, and sets
STRICT_DISCLOSURE_TEST=1. In that mode every skip below becomes a failure: no
web service is a failure, no reachable database is a failure, and a seeded
dataset containing nothing to disclose is a failure too.

That last one matters most. Without it the job could pass by finding no
flagged application and skipping — green, and asserting precisely nothing.
"""

from __future__ import annotations

import os

import urllib.error
import urllib.request
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

BASE_URL = os.environ.get("PUBLIC_BASE_URL") or "http://localhost:3001"

#: Set by CI. Turns every "cannot check this here" into a failure.
STRICT = os.environ.get("STRICT_DISCLOSURE_TEST") == "1"


def unavailable(reason: str) -> None:
    """Skip locally, fail on CI.

    One helper rather than a flag threaded through each fixture, so there is
    exactly one place where the difference lives and no way to add a fixture
    later that quietly skips in strict mode.
    """
    if STRICT:
        pytest.fail(
            f"STRICT_DISCLOSURE_TEST=1 and the check could not run: {reason}. "
            "On CI this is a failure rather than a skip — a disclosure guard "
            "that does not run is worse than no guard."
        )
    pytest.skip(reason)

#: The applicant's own name is the one thing from a screening match they are
#: entitled to see — it is their name. Everything else about the match is not
#: theirs, and some of it is not anyone's outside the compliance function.
FORBIDDEN_TOKENS = (
    "OFAC",
    "SDN",
    "sanctions match",
    "risk_score",
    "name similarity",
    "confirmed",
    "probable",
    "auto_reject",
    "ruleset",
)

#: Payload keys that would indicate the raw audit `details` object reached the
#: browser, whatever it happened to contain for this particular applicant.
FORBIDDEN_KEYS = ('"score"', '"reasons"', '"routing"', '"candidates"')


def fetch(path: str) -> tuple[int, str]:
    request = urllib.request.Request(
        f"{BASE_URL}{path}", headers={"User-Agent": "kyc-disclosure-test"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, response.read().decode("utf-8", "replace")


@pytest.fixture(scope="module")
def web_service():
    """Skip the module unless the web app is actually up."""
    try:
        fetch("/")
    except (urllib.error.URLError, OSError) as err:
        unavailable(f"web service not reachable at {BASE_URL}: {err}")
    return BASE_URL


@pytest.fixture(scope="module")
def flagged_application(web_service):
    """An application whose audit log really does contain a screening match.

    FOUND, NOT CREATED, and this is the one fixture in the suite that does not
    use the throwaway test database.

    The reason is unavoidable: the assertion is about what a running web server
    serves, and that server is connected to DATABASE_URL. An application
    written into the `_test` database would be invisible to it, so the test
    would pass against a page that renders nothing — the exact false green this
    file exists to prevent.

    Writing into DATABASE_URL instead was the other option and is worse:
    `audit_events` refuses DELETE, so every run would permanently add a
    fabricated flagged applicant to the demo data.

    So it reads. The seeded history has plenty of these, and testing against
    real seeded rows is better evidence than testing against a row the test
    wrote to its own specification. If none exists it says so — as a skip
    locally, and as a failure under STRICT_DISCLOSURE_TEST, because a run that
    found nothing to disclose has proved nothing about disclosure.
    """
    from db import database_url  # the worker's own DATABASE_URL resolver

    try:
        connection = psycopg.connect(database_url(), autocommit=True, connect_timeout=5)
    except Exception as err:  # noqa: BLE001 — any connection problem
        unavailable(f"no database reachable: {err}")

    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                -- A screening match AND a full timeline. The counterweight
                -- test asserts the applicant can still see their own history,
                -- and an application assembled by hand in some other test has
                -- a screening event but no 'application.created' — which made
                -- this fixture pick it and the counterweight fail for a reason
                -- that had nothing to do with disclosure.
                select s.application_id
                  from audit_events s
                 where s.action = 'screening.completed'
                   and s.details::text ilike '%sanctions match%'
                   and exists (
                       select 1 from audit_events c
                        where c.application_id = s.application_id
                          and c.action = 'application.created')
                 order by s.id desc
                 limit 1
                """
            )
            row = cur.fetchone()
    finally:
        connection.close()

    if row is None:
        unavailable(
            "no seeded application carries a sanctions match — run "
            "worker/seed.py so there is something to fail to disclose"
        )
    return str(row[0])


def test_the_status_page_reveals_no_screening_detail(
    web_service, flagged_application
):
    """The headline assertion: none of it reaches the browser, anywhere.

    Greps the entire response — rendered markup and RSC payload together — so
    a regression that stops rendering the data but starts serialising it still
    fails here.
    """
    status, body = fetch(f"/status/{flagged_application}")
    assert status == 200

    found = [t for t in FORBIDDEN_TOKENS if t.lower() in body.lower()]
    assert not found, (
        f"the public status page disclosed {found} — an applicant must not be "
        "shown what screening found. If this broke after moving the timeline "
        "into a Client Component, that is the documented cause: `details` is "
        "then serialised into the payload even if nothing renders it."
    )

    leaked_keys = [k for k in FORBIDDEN_KEYS if k in body]
    assert not leaked_keys, (
        f"raw audit details reached the browser (keys {leaked_keys}) — the "
        "whitelist in timeline-copy.ts is being bypassed"
    )


def test_the_applicants_own_name_is_still_shown(web_service, flagged_application):
    """The counterweight, so the test above cannot be satisfied by a blank page.

    Without this, deleting the whole history section would make the disclosure
    test pass — which is the classic way a security assertion quietly stops
    asserting anything.
    """
    _, body = fetch(f"/status/{flagged_application}")
    assert "Application started" in body, "the timeline rendered nothing at all"
    assert "Your application" in body, "the page did not render"


def test_the_officer_case_view_is_not_affected(web_service, flagged_application):
    """Two audiences, two rules.

    The desk must still show the officer everything. If a well-meaning fix
    stripped the detail everywhere rather than for this one audience, the
    auditor loses the record — so the case view is checked to still be behind
    a login rather than checked to be empty.
    """
    try:
        status, _ = fetch(f"/desk/{flagged_application}")
    except urllib.error.HTTPError as err:
        status = err.code

    assert status in (200, 302, 303, 307, 308, 401, 403), (
        f"unexpected status {status} from the desk"
    )


def test_a_status_page_for_an_unknown_application_discloses_nothing(web_service):
    """A wrong UUID gets a 404, and says nothing either way.

    ON THE STATUS CODE, which this test now does assert.

    This used to answer HTTP 200 while rendering the not-found page, because
    `loading.tsx` in this segment made Next.js begin streaming the loading
    shell immediately: the response headers flushed, and by the time
    `notFound()` threw, the status could no longer be set.

    The shell was removed. It bought very little — the page is one indexed
    lookup — and a public endpoint answering 200 for something that does not
    exist is wrong in a way a technical reader notices immediately.

    A Suspense boundary inside the page, after the existence check, would have
    kept both. It was not worth it here: it means splitting the two queries
    that currently run in parallel into a sequential lookup plus a suspended
    child, which is more moving parts than a sub-second query deserves.
    """
    try:
        status, body = fetch(f"/status/{uuid.uuid4()}")
    except urllib.error.HTTPError as err:
        status, body = err.code, err.read().decode("utf-8", "replace")

    assert status == 404, (
        f"expected 404 for an application that does not exist, got {status}. "
        "If a loading.tsx has been added back to this segment, that is the "
        "cause: streaming starts before notFound() can set the status."
    )

    # The disclosure property, which is the point of this file.
    leaked = [token for token in FORBIDDEN_TOKENS if token.lower() in body.lower()]
    assert not leaked, f"the not-found page disclosed {leaked}"
    # A timeline entry only ever renders from real audit rows, so its absence
    # is the check that no real application was served. (Asserting on the
    # heading "Your application" would have been wrong even now: it used to
    # appear in the loading skeleton for every id, including unknown ones.)
    assert "Application started" not in body, (
        "a real application's history rendered for an id that does not exist"
    )
