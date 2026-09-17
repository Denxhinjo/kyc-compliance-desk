"""The identity-verification vendor, behind one interface.

The job handler must not mention Didit. It asks a VendorClient for a session's
current state and gets the same shape back whether that came from Didit's live
API or from the local simulator. Switching is configuration.

Phase 3 built the equivalent adapter in TypeScript, for creating sessions from
the web service. This is the worker's half, for reading results. They are
separate on purpose: the two services share a database and nothing else, so an
adapter cannot be shared between them without inventing the internal API that
architecture rule 1 exists to avoid.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from config import (
    DIDIT_API_KEY,
    DIDIT_BASE_URL,
    DIDIT_MODE,
    PUBLIC_BASE_URL,
    VENDOR_TIMEOUT_SECONDS,
)

log = logging.getLogger("worker.vendor")


class VendorError(Exception):
    """The vendor could not be reached, or answered with something unusable.

    Transient by assumption, so the job retries with backoff. A vendor being
    down is the ordinary case this queue was built for.
    """


@dataclass(frozen=True)
class VerificationResult:
    """What the vendor currently says about one session."""

    session_id: str
    #: The vendor's own vocabulary, kept verbatim: 'Approved', 'In Review', ...
    status: str
    #: OUR applications.id, echoed back by the vendor.
    vendor_data: str | None
    #: The vendor's OWN timestamp for this result, if they publish one.
    #:
    #: None when pulled from Didit's decision endpoint, which exposes no result
    #: timestamp. Callers must treat None as "recency unknown" and fall back to
    #: the state machine — never substitute our own clock, because comparing our
    #: observation time against their event time mixes two clocks and the skew
    #: between them is where this kind of guard silently stops working.
    result_at: datetime | None
    #: The full response, stored for the reviewer and for later phases.
    raw: dict[str, Any]


class VendorClient(Protocol):
    name: str

    def fetch_session(self, session_id: str) -> VerificationResult | None:
        """Return the session's current state, or None if the vendor has no
        record of it. Raises VendorError if the vendor could not be asked."""


def _get_json(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=VENDOR_TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")[:500]
        if err.code == 404:
            return 404, None
        raise VendorError(f"HTTP {err.code} from vendor: {body}") from err
    except urllib.error.URLError as err:
        raise VendorError(f"could not reach vendor: {err.reason}") from err
    except json.JSONDecodeError as err:
        raise VendorError(f"vendor returned something that is not JSON: {err}") from err


class DiditClient:
    """The real vendor.

    GET /v3/session/{session_id}/decision/ with the API key in x-api-key.
    https://docs.didit.me/sessions-api/retrieve-session

    Written from the published contract and NOT exercised against the live API,
    because this project has no Didit account.
    """

    name = "didit"

    def fetch_session(self, session_id: str) -> VerificationResult | None:
        if not DIDIT_API_KEY:
            raise VendorError("DIDIT_MODE=live but DIDIT_API_KEY is not set")

        status_code, body = _get_json(
            f"{DIDIT_BASE_URL}/v3/session/{session_id}/decision/",
            {"x-api-key": DIDIT_API_KEY, "accept": "application/json"},
        )
        if status_code == 404 or body is None:
            return None

        return VerificationResult(
            session_id=str(body.get("session_id", session_id)),
            status=str(body.get("status", "")),
            vendor_data=body.get("vendor_data"),
            # Their decision endpoint returns created_at (when the SESSION was
            # created) and expires_at — neither of which is "when this result
            # was reached". Rather than pass off a timestamp that means
            # something else, this is left None and the state machine carries
            # the weight. An honest None beats a plausible wrong value.
            result_at=None,
            raw=body,
        )


class SimulatorClient:
    """The local stand-in, speaking the same contract.

    It reads from the simulator's own store rather than from our domain tables:
    a vendor that consulted our database would prove nothing, because the entire
    point of the sweeper is asking a party that knows something we do not.
    """

    name = "simulator"

    def fetch_session(self, session_id: str) -> VerificationResult | None:
        # No trailing slash, unlike Didit's documented URL: Next.js answers a
        # trailing slash with a 308 to the unslashed form. Following a redirect
        # would work, but an adapter exists precisely so each vendor's URL
        # conventions stay where they belong rather than leaking into shared code.
        status_code, body = _get_json(
            f"{PUBLIC_BASE_URL}/api/mock-vendor/v3/session/{session_id}/decision",
            {"accept": "application/json"},
        )
        if status_code == 404 or body is None:
            return None

        # The simulator does publish a result timestamp, so the recency guard is
        # exercised on this path even though the live path cannot supply one.
        raw_result_at = body.get("result_at")
        result_at = None
        if isinstance(raw_result_at, str):
            try:
                result_at = datetime.fromisoformat(raw_result_at)
                if result_at.tzinfo is None:
                    result_at = result_at.replace(tzinfo=timezone.utc)
            except ValueError:
                log.warning("simulator sent an unparseable result_at: %r", raw_result_at)

        return VerificationResult(
            session_id=str(body.get("session_id", session_id)),
            status=str(body.get("status", "")),
            vendor_data=body.get("vendor_data"),
            result_at=result_at,
            raw=body,
        )


def get_client() -> VendorClient:
    return DiditClient() if DIDIT_MODE == "live" else SimulatorClient()
