import crypto from "node:crypto";
import { pool } from "@/lib/db";

/**
 * SIMULATOR — stands in for POST https://verification.didit.me/v3/session/
 *
 * Exists because Didit requires a business account, so a reader cloning this
 * repository cannot obtain sandbox credentials. It speaks the same contract:
 * https://docs.didit.me/sessions-api/create-session
 *
 * It lives inside /web rather than being a third service, because two services
 * and one database is the ceiling and demo scaffolding does not get to raise
 * it. Everything simulated is namespaced under mock-vendor/ so it can be
 * removed in one commit.
 *
 * As of Phase 4 the simulator REMEMBERS sessions, in its own
 * mock_vendor_sessions table. It has to: the sweeper can only be demonstrated
 * if the vendor knows an outcome we were never told, which means the simulator
 * must retain what happened when it deliberately drops a webhook. That table
 * belongs to the pretend vendor, not to the domain model, and nothing outside
 * these routes reads it.
 */

export const dynamic = "force-dynamic";

function simulatorDisabled(): Response | null {
  if ((process.env.DIDIT_MODE ?? "simulator") === "live") {
    // In live mode this route must not answer at all. A simulator that stays
    // reachable in production is a way to forge verification sessions.
    return Response.json({ error: "not found" }, { status: 404 });
  }
  return null;
}

export async function POST(request: Request): Promise<Response> {
  const disabled = simulatorDisabled();
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

  await pool.query(
    `insert into mock_vendor_sessions (session_id, vendor_data, status)
     values ($1, $2, 'Not Started')`,
    [sessionId, body.vendor_data],
  );

  // Real Didit returns a 12-character opaque token. Ours carries the session id
  // so the verification screen needs no lookup to know which session it is.
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
