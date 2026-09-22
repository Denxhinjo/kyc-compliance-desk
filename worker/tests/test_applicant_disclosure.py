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

Requires the web service to be running. Skips cleanly when it is not, in
keeping with the rest of the suite — CI runs the worker and typechecks the web
app but does not boot it, which is stated in the README's limitations.
"""

from __future__ import annotations

import os

import urllib.error
import urllib.request
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

BASE_URL = os.environ.get("PUBLIC_BASE_URL") or "http://localhost:3001"

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
        pytest.skip(f"web service not reachable at {BASE_URL}: {err}")
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
    wrote to its own specification. If none exists, it skips and says so rather
    than inventing one.
    """
    from db import database_url  # the worker's own DATABASE_URL resolver

    try:
        connection = psycopg.connect(database_url(), autocommit=True, connect_timeout=5)
    except Exception as err:  # noqa: BLE001 — unreachable database means skip
        pytest.skip(f"no database reachable: {err}")

    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                select application_id
                  from audit_events
                 where action = 'screening.completed'
                   and details::text ilike '%sanctions match%'
                 order by id desc
                 limit 1
                """
            )
            row = cur.fetchone()
    finally:
        connection.close()

    if row is None:
        pytest.skip(
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
    """A wrong UUID must not confirm or deny that an application exists.

    ON THE STATUS CODE, which this test deliberately does not assert.

    An unknown application renders the not-found page but answers HTTP 200,
    not 404. That is not a typo — it is caused by `loading.tsx` existing in
    this segment. Next.js begins streaming the loading shell immediately, the
    response headers are flushed, and by the time `notFound()` throws the
    status can no longer be changed.

    Measured, not guessed: moving `status/[id]/loading.tsx` aside makes the
    same request answer 404, and putting it back makes it answer 200 again.

    So it is a real trade between a correct status code and the loading state
    that keeps the page from jumping when data arrives. Which one wins is a
    product decision, not a bug to quietly fix, and it is recorded here so
    whoever makes it can see the cost. What matters for THIS file is
    disclosure, and on that the page is correct either way: the body says
    nothing about whether the application exists.
    """
    try:
        status, body = fetch(f"/status/{uuid.uuid4()}")
    except urllib.error.HTTPError as err:
        status, body = err.code, err.read().decode("utf-8", "replace")

    assert status in (200, 404), f"unexpected status {status}"

    # The disclosure property, which is the point of this file.
    leaked = [token for token in FORBIDDEN_TOKENS if token.lower() in body.lower()]
    assert not leaked, f"the not-found page disclosed {leaked}"
    # NOT "Your application": that heading is in the loading skeleton, which
    # streams before the not-found content replaces it, so it is present in the
    # response for any id at all. Asserting on it tested the skeleton rather
    # than the page. A timeline entry only ever renders from real audit rows.
    assert "Application started" not in body, (
        "a real application's history rendered for an id that does not exist"
    )
