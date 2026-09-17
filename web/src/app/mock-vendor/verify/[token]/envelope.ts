import crypto from "node:crypto";

/**
 * SIMULATOR — the pieces of the vendor's side that are not server actions.
 *
 * These live outside actions.ts because a file marked "use server" may only
 * export async functions: every export becomes a callable endpoint, so a
 * synchronous helper there is a build error rather than a style problem. Worth
 * knowing, because the error message points at the export rather than at the
 * directive that caused it.
 */

export interface SessionToken {
  sessionId: string;
  vendorData: string;
}

export function decodeToken(token: string): SessionToken | null {
  try {
    const decoded = JSON.parse(
      Buffer.from(token, "base64url").toString("utf8"),
    ) as SessionToken;
    if (!decoded.sessionId || !decoded.vendorData) return null;
    return decoded;
  } catch {
    return null;
  }
}

/** The envelope Didit documents, with the fields we care about populated. */
export function buildEnvelope(
  session: SessionToken,
  status: string,
  now: Date = new Date(),
): Record<string, unknown> {
  const seconds = Math.floor(now.getTime() / 1000);
  return {
    event_id: crypto.randomUUID(),
    webhook_type: "status.updated",
    timestamp: seconds,
    created_at: seconds,
    // Didit's application, NOT ours. Included precisely so the confusingly
    // named field is visible in the stored payload rather than quietly absent.
    application_id: "00000000-0000-4000-8000-00000000a99a",
    environment: "sandbox",
    status,
    session_id: session.sessionId,
    workflow_id: process.env.DIDIT_WORKFLOW_ID || "simulator-workflow",
    // OUR applications.id. The only link between their world and ours.
    vendor_data: session.vendorData,
    sandbox_scenario: null,
    metadata: {},
    decision:
      status === "Approved" || status === "Declined" || status === "In Review"
        ? {
            id_verifications: [
              {
                node_id: "id-1",
                status,
                verification_method: "document",
                assurance: "documentary",
              },
            ],
          }
        : undefined,
  };
}
