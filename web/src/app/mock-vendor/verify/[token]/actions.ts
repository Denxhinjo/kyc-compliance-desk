"use server";

import { redirect } from "next/navigation";
import { signWebhookBody } from "@/lib/didit";
import { buildEnvelope, decodeToken } from "./envelope";

/**
 * SIMULATOR — stands in for the applicant finishing at Didit's hosted screen.
 *
 * Builds a real Didit `status.updated` envelope, signs it with the shared
 * secret, and POSTs it over HTTP to our own webhook endpoint. Going over the
 * wire rather than calling the handler directly is deliberate: it exercises the
 * actual route, the actual header parsing and the actual raw-body read. A test
 * that bypasses the transport proves nothing about the transport.
 */
export async function completeVerification(
  _previous: { error?: string },
  form: FormData,
): Promise<{ error: string } | never> {
  const token = String(form.get("token") ?? "");
  const status = String(form.get("status") ?? "Approved");

  if ((process.env.DIDIT_MODE ?? "simulator") === "live") {
    return { error: "simulator is disabled in live mode" };
  }

  const session = decodeToken(token);
  if (!session) return { error: "invalid session token" };

  const envelope = buildEnvelope(session, status);

  // The exact bytes that get signed are the exact bytes that get sent. This is
  // the sender's half of the same rule the verifier enforces.
  const rawBody = Buffer.from(JSON.stringify(envelope), "utf8");
  const signature = signWebhookBody(rawBody);

  const publicBaseUrl = process.env.PUBLIC_BASE_URL ?? "http://localhost:3001";

  let response: Response;
  try {
    response = await fetch(`${publicBaseUrl}/api/webhooks/didit`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "user-agent": "DiditWebhook/2.0 +https://didit.me",
        "x-timestamp": String(envelope.timestamp),
        "x-signature": signature,
      },
      body: rawBody,
    });
  } catch (err) {
    return {
      error: `could not deliver webhook: ${err instanceof Error ? err.message : err}`,
    };
  }

  if (!response.ok) {
    return { error: `webhook returned HTTP ${response.status}` };
  }

  redirect(`/status/${session.vendorData}`);
}
