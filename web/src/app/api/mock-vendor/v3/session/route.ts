import crypto from "node:crypto";

/**
 * SIMULATOR — stands in for POST https://verification.didit.me/v3/session/
 *
 * Exists because Didit requires a business account, so a reader cloning this
 * repository cannot obtain sandbox credentials. Rather than leave the vendor
 * integration untestable, this speaks the same contract:
 * https://docs.didit.me/sessions-api/create-session
 *
 * It lives inside /web rather than being a third service, because the
 * architecture is two services and one database and that is not negotiable for
 * demo scaffolding. Everything simulated is namespaced under mock-vendor/ so
 * it can be deleted in one commit.
 *
 * It is stateless on purpose: the session token encodes what the verification
 * screen needs, so the simulator needs no table of its own and adds nothing to
 * the real schema.
 */

export const dynamic = "force-dynamic";

function guardSimulatorEnabled(): Response | null {
  if ((process.env.DIDIT_MODE ?? "simulator") === "live") {
    // In live mode this route must not answer at all. A simulator that stays
    // reachable in production is a way to forge verification sessions.
    return Response.json({ error: "not found" }, { status: 404 });
  }
  return null;
}

export async function POST(request: Request): Promise<Response> {
  const disabled = guardSimulatorEnabled();
  if (disabled) return disabled;

  const body = (await request.json()) as {
    workflow_id?: string;
    vendor_data?: string;
    callback?: string;
  };

  if (!body.vendor_data) {
    return Response.json({ error: "vendor_data is required" }, { status: 400 });
  }

  const sessionId = crypto.randomUUID();

  // Real Didit returns a 12-character opaque token. Ours carries its own state
  // so there is nothing to store — same shape of thing, different contents.
  const token = Buffer.from(
    JSON.stringify({ sessionId, vendorData: body.vendor_data }),
    "utf8",
  ).toString("base64url");

  const publicBaseUrl = process.env.PUBLIC_BASE_URL ?? "http://localhost:3001";

  return Response.json(
    {
      session_id: sessionId,
      session_number: Math.floor(Math.random() * 90000) + 10000,
      session_token: token,
      url: `${publicBaseUrl}/mock-vendor/verify/${token}`,
      vendor_data: body.vendor_data,
      metadata: null,
      status: "Not Started",
      workflow_id: body.workflow_id ?? "simulator-workflow",
      workflow_version: 3,
      callback: body.callback ?? null,
    },
    { status: 201 },
  );
}
