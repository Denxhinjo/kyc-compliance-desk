import crypto from "node:crypto";

/**
 * The identity verification vendor.
 *
 * Written against Didit's published contract:
 *   https://docs.didit.me/sessions-api/create-session
 *   https://docs.didit.me/integration/webhooks
 *
 * NOT tested against their live API — we have no account. DIDIT_MODE=simulator
 * points session creation at a local stand-in that speaks the same contract.
 * Switching to the real thing is configuration, not code.
 */

export const DIDIT_MODE = process.env.DIDIT_MODE ?? "simulator";
const DIDIT_BASE_URL = process.env.DIDIT_BASE_URL ?? "https://verification.didit.me";
const PUBLIC_BASE_URL = process.env.PUBLIC_BASE_URL ?? "http://localhost:3001";

/** How far out of date a webhook's timestamp may be. Didit's documented rule. */
const MAX_TIMESTAMP_SKEW_SECONDS = 300;

function webhookSecret(): string {
  const secret = process.env.DIDIT_WEBHOOK_SECRET;
  if (!secret) {
    throw new Error("DIDIT_WEBHOOK_SECRET is not set");
  }
  return secret;
}

// ---------------------------------------------------------------------------
// Signature
// ---------------------------------------------------------------------------

/**
 * Compute the webhook signature over the exact bytes of a request body.
 *
 * Used by the verifier, and by the simulator and replay script to produce
 * genuine signatures. One function, so a test that passes cannot be passing
 * against a different algorithm than production uses.
 */
export function signWebhookBody(rawBody: Buffer, secret = webhookSecret()): string {
  return crypto.createHmac("sha256", secret).update(rawBody).digest("hex");
}

export type SignatureVerdict =
  | { ok: true }
  | { ok: false; reason: string };

/**
 * Verify a Didit webhook.
 *
 * ON WHICH SIGNATURE WE CHECK
 *
 * Didit sends three headers and recommends X-Signature-V2, which is an HMAC
 * over a *canonicalised re-serialisation* of the JSON — keys sorted, compact
 * separators, Unicode unescaped, whole floats normalised to integers.
 *
 * We deliberately use X-Signature, the HMAC over the raw bytes, instead.
 *
 * Verifying V2 would mean reimplementing their JSON serialiser exactly. Every
 * one of those four rules is somewhere my implementation could differ subtly
 * from theirs, and the failure is not a loud crash — it is either mismatches on
 * certain payloads (which looks like a vendor outage) or, far worse, a "fix"
 * that ends up verifying a string I constructed rather than the one they
 * signed. That silently destroys the entire guarantee.
 *
 * Raw bytes have exactly one interpretation. There is nothing to get wrong.
 *
 * V2 exists because many frameworks make the raw body hard to reach. Next.js
 * route handlers do not: `await request.arrayBuffer()` hands them over before
 * anything has parsed them.
 *
 * X-Signature-Simple is ignored entirely — it signs only
 * `timestamp:session_id:status:webhook_type`, so it authenticates the envelope
 * but not the body, and Didit themselves say that if you rely on it you must
 * treat the payload as untrusted and re-fetch it.
 */
export function verifyWebhookSignature(
  rawBody: Buffer,
  headers: Headers,
  now: Date = new Date(),
): SignatureVerdict {
  const provided = headers.get("x-signature");
  if (!provided) {
    return { ok: false, reason: "missing x-signature header" };
  }

  const timestampHeader = headers.get("x-timestamp");
  if (!timestampHeader) {
    return { ok: false, reason: "missing x-timestamp header" };
  }

  const timestamp = Number(timestampHeader);
  if (!Number.isFinite(timestamp)) {
    return { ok: false, reason: "x-timestamp is not a number" };
  }

  // Replay protection. A signature proves a message is genuine; it does NOT
  // stop someone who captured a genuine message from sending it again. This
  // bounds that window to five minutes. The unique constraint on
  // vendor_event_id then makes a replay inside that window harmless anyway —
  // three layers, each closing a gap the others leave open.
  const skew = Math.abs(now.getTime() / 1000 - timestamp);
  if (skew > MAX_TIMESTAMP_SKEW_SECONDS) {
    return {
      ok: false,
      reason: `timestamp is ${Math.round(skew)}s out of date (max ${MAX_TIMESTAMP_SKEW_SECONDS}s)`,
    };
  }

  const expected = signWebhookBody(rawBody);

  // Constant-time comparison. `===` on strings returns as soon as it reaches a
  // differing byte, so it takes measurably longer the more leading bytes match.
  // Over many attempts that leaks the signature a byte at a time.
  // timingSafeEqual always compares the whole buffer.
  //
  // It throws on length mismatch, which would itself leak length — so the
  // lengths are compared first and a mismatch is rejected outright.
  const providedBuf = Buffer.from(provided, "utf8");
  const expectedBuf = Buffer.from(expected, "utf8");
  if (providedBuf.length !== expectedBuf.length) {
    return { ok: false, reason: "signature does not match" };
  }
  if (!crypto.timingSafeEqual(providedBuf, expectedBuf)) {
    return { ok: false, reason: "signature does not match" };
  }

  return { ok: true };
}

// ---------------------------------------------------------------------------
// The webhook envelope
// ---------------------------------------------------------------------------

/**
 * The fields of a Didit `status.updated` webhook that we actually use.
 *
 * NOTE THE TRAP: Didit's `application_id` is *their* application — the app in
 * your Didit account. It is NOT our applications.id. Our link back to an
 * applicant is `vendor_data`, which we set to our application UUID when the
 * session is created. Using their application_id as our foreign key would fail
 * in a way that looks like data corruption.
 */
export interface DiditWebhookEnvelope {
  event_id: string;
  webhook_type: string;
  timestamp: number;
  created_at: number;
  /** Didit's application, NOT ours. See the note above. */
  application_id?: string;
  environment?: string;
  status?: string;
  session_id?: string;
  workflow_id?: string;
  /** OUR applications.id, set when we created the session. */
  vendor_data?: string;
  decision?: unknown;
  [key: string]: unknown;
}

/** Session statuses Didit documents, exactly as spelled (note "Kyc Expired"). */
export const DIDIT_STATUSES = [
  "Not Started",
  "In Progress",
  "Approved",
  "Declined",
  "In Review",
  "Abandoned",
  "Expired",
  "Kyc Expired",
  "Resubmitted",
  "Awaiting User",
] as const;

// ---------------------------------------------------------------------------
// Session creation
// ---------------------------------------------------------------------------

export interface VerificationSession {
  sessionId: string;
  sessionToken: string;
  /** Where to send the applicant to prove who they are. */
  url: string;
  status: string;
}

/**
 * Create a verification session for one application.
 *
 * `vendor_data` carries our application id so the webhook can find its way
 * back to the right case. It is the only link between their world and ours.
 */
export async function createVerificationSession(
  applicationId: string,
): Promise<VerificationSession> {
  const endpoint =
    DIDIT_MODE === "live"
      ? `${DIDIT_BASE_URL}/v3/session/`
      : `${PUBLIC_BASE_URL}/api/mock-vendor/v3/session/`;

  const apiKey = process.env.DIDIT_API_KEY ?? "";
  const workflowId = process.env.DIDIT_WORKFLOW_ID ?? "";

  if (DIDIT_MODE === "live" && (!apiKey || !workflowId)) {
    throw new Error(
      "DIDIT_MODE=live needs DIDIT_API_KEY and DIDIT_WORKFLOW_ID. " +
        "Set DIDIT_MODE=simulator to run without an account.",
    );
  }

  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": apiKey,
    },
    body: JSON.stringify({
      workflow_id: workflowId || "simulator-workflow",
      vendor_data: applicationId,
      callback: `${PUBLIC_BASE_URL}/status/${applicationId}`,
      language: "en",
    }),
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `vendor rejected session creation: HTTP ${response.status} ${detail.slice(0, 300)}`,
    );
  }

  const body = (await response.json()) as {
    session_id: string;
    session_token: string;
    url: string;
    status: string;
  };

  return {
    sessionId: body.session_id,
    sessionToken: body.session_token,
    url: body.url,
    status: body.status,
  };
}
