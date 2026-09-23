"""POST /api/drain — work a bounded batch of jobs, then return.

The worker's production entry point. A Python function, because the handlers
are Python: scoring, screening and the lifecycle rules are the part of this
project most worth reading, and reimplementing them in TypeScript so that the
endpoint could be a Next.js route would be duplicating the one thing that must
never have two versions.

ON ARCHITECTURE RULE 1

CLAUDE.md says the two services never call each other, no HTTP between them.
That rule is now bent, deliberately, and it is worth being straight about which
part survives.

What the rule was protecting against was an internal API: the web app asking
the worker to DO something and waiting to hear how it went, which drags CORS,
shared auth, retries and a second failure mode into a design that had none.

That is not what this is. The web app still communicates through the jobs
table — it enqueues, commits, and the work is durable at that moment. The POST
that follows carries no payload, expects no answer, and is not waited for. It
is a doorbell, not a request: it says "there may be something to do", and if it
never arrives the scheduled call a few minutes later does the same work. Delete
the doorbell and the system is still correct, only slower.

So: the queue remains the only channel that carries meaning. The HTTP call
carries timing.

AUTHENTICATION

A shared secret in a header, compared with hmac.compare_digest. Without it this
is a public endpoint that lets anyone drive the queue — not catastrophic, since
it only does work that was already enqueued, but it is free to invoke and
trivially abusable as a way to burn someone else's function budget.

Not a signature over the body, because there is no body worth signing. The
threat here is unauthorised invocation, not tampering.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# The worker package lives outside this directory. Vercel bundles files it can
# see from the project root, so the path is made explicit rather than left to
# whatever the working directory happens to be at invocation time.
WORKER = Path(__file__).resolve().parent.parent / "worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("api.drain")

DRAIN_SECRET = os.environ.get("DRAIN_SECRET", "")
HEADER = "x-drain-secret"


def authorised(headers) -> bool:
    """Constant-time comparison of a shared secret.

    compare_digest rather than `==` for the usual reason: `==` on strings
    returns as soon as two bytes differ, and the time it took is a
    character-by-character oracle. The window is small over a network and the
    correct comparison costs nothing.

    An unset DRAIN_SECRET refuses everything rather than allowing everything.
    A deployment that forgot to set it should be visibly broken, not quietly
    open.
    """
    if not DRAIN_SECRET:
        log.error("DRAIN_SECRET is not set — refusing every request")
        return False
    return hmac.compare_digest(headers.get(HEADER, ""), DRAIN_SECRET)


class handler(BaseHTTPRequestHandler):  # noqa: N801 — Vercel requires this name
    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's interface
        if not authorised(self.headers):
            self._reply(401, {"error": "unauthorised"})
            return

        try:
            from db import connect
            from batch import DEFAULT_BATCH, drain_once, ensure_recurring

            limit = _int_param(self.path, "limit", DEFAULT_BATCH)

            with connect() as conn:
                # Cheap, and the only place it can happen now: a function has
                # no startup in which to schedule the recurring jobs.
                ensure_recurring(conn)
                result = drain_once(conn, limit=limit)

            self._reply(200, result.as_dict())
        except Exception as err:  # noqa: BLE001
            # Log the traceback, return the message. A drain failing is an
            # operational event the scheduler will retry in minutes; it should
            # be loud in the logs and terse on the wire.
            log.error("drain failed:\n%s", traceback.format_exc())
            self._reply(500, {"error": f"{type(err).__name__}: {err}"})

    def do_GET(self) -> None:  # noqa: N802
        """Liveness only. GET never does work.

        A GET that drained would be one crawler away from being invoked
        constantly, and browsers and link previewers issue GETs unbidden.
        """
        self._reply(
            200,
            {
                "service": "drain",
                "usage": f"POST with the {HEADER} header",
                "secret_configured": bool(DRAIN_SECRET),
            },
        )

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        """Silence BaseHTTPRequestHandler's own stderr access log.

        It duplicates what the platform already records, and on a function it
        is noise charged by the line.
        """


def _int_param(path: str, name: str, default: int) -> int:
    from urllib.parse import parse_qs, urlparse

    values = parse_qs(urlparse(path).query).get(name)
    if not values:
        return default
    try:
        # Clamped: an unbounded limit from a query string is how a bounded
        # batch stops being bounded.
        return max(1, min(int(values[0]), 50))
    except ValueError:
        return default
