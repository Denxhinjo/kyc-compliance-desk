import crypto from "node:crypto";
// Relative, not the "@/" alias: that alias is resolved by Next's bundler, and
// this module is also imported by scripts/ running under plain Node, which
// knows nothing about it. Files reached only through Next may use the alias
// freely; anything a script can reach must not.
import { pool } from "./db.ts";
import { signWebhookBody } from "./didit.ts";

/**
 * SIMULATOR — the vendor's side of completing a verification.
 *
 * One implementation, called both by the verification screen's Server Action
 * and by the scripted test harness. Two copies would drift, and the scenarios
 * this exists to stage — dropped deliveries, out-of-order results — are exactly
 * the ones where a subtle difference between "what the UI does" and "what the
 * test does" would make a proof worthless.
 */

export interface CompleteOptions {
  /** False stages a lost delivery: the vendor records the outcome, we never hear it. */
  deliverWebhook?: boolean;
  /**
   * Override the vendor's event timestamp, in seconds. Used to stage an
   * out-of-order delivery — a result the vendor produced earlier arriving now.
   */
  timestampSeconds?: number;
}

export interface CompleteOutcome {
  sessionId: string;
  vendorData: string;
  status: string;
  delivered: boolean;
  webhookStatus?: number;
  eventId?: string;
}

export async function completeSession(
  sessionId: string,
  status: string,
  options: CompleteOptions = {},
): Promise<CompleteOutcome> {
  const deliver = options.deliverWebhook ?? true;

  // The vendor's own record. This happens whether or not we are told, because
  // that is what "the vendor knows something we do not" means.
  //
  // result_at is the VENDOR's clock, set explicitly so a test can stage a
  // result the vendor reached earlier than one we have already applied. That is
  // the only way to exercise the recency guard honestly: it compares vendor
  // timestamps against vendor timestamps, so a test that could only move our
  // own clock would prove nothing.
  const resultAt = options.timestampSeconds
    ? new Date(options.timestampSeconds * 1000)
    : new Date();

  const { rows } = await pool.query<{ vendor_data: string }>(
    `update mock_vendor_sessions
        set status = $1, webhook_delivered = $2, result_at = $3
      where session_id = $4
      returning vendor_data`,
    [status, deliver, resultAt, sessionId],
  );

  const session = rows[0];
  if (!session) {
    throw new Error(`simulator has no session ${sessionId}`);
  }

  if (!deliver) {
    return {
      sessionId,
      vendorData: session.vendor_data,
      status,
      delivered: false,
    };
  }

  const seconds = Math.floor(resultAt.getTime() / 1000);
  const eventId = crypto.randomUUID();

  const envelope = {
    event_id: eventId,
    webhook_type: "status.updated",
    timestamp: seconds,
    created_at: seconds,
    // The vendor's application, NOT ours. Present so the confusingly named
    // field is visible in the stored payload rather than quietly absent.
    application_id: "00000000-0000-4000-8000-00000000a99a",
    environment: "sandbox",
    status,
    session_id: sessionId,
    workflow_id: process.env.DIDIT_WORKFLOW_ID || "simulator-workflow",
    // OUR applications.id.
    vendor_data: session.vendor_data,
    metadata: {},
  };

  const rawBody = Buffer.from(JSON.stringify(envelope), "utf8");
  const signature = signWebhookBody(rawBody);
  const publicBaseUrl = process.env.PUBLIC_BASE_URL ?? "http://localhost:3001";

  const response = await fetch(`${publicBaseUrl}/api/webhooks/didit`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "user-agent": "DiditWebhook/2.0 +https://didit.me",
      "x-timestamp": String(seconds),
      "x-signature": signature,
    },
    body: rawBody,
  });

  return {
    sessionId,
    vendorData: session.vendor_data,
    status,
    delivered: true,
    webhookStatus: response.status,
    eventId,
  };
}
