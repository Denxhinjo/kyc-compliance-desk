import { pool } from "@/lib/db";

/**
 * SIMULATOR — stands in for
 * GET https://verification.didit.me/v3/session/{session_id}/decision/
 * https://docs.didit.me/sessions-api/retrieve-session
 *
 * This is the endpoint the sweeper calls. It answers from the simulator's own
 * store, never from the applications table — a pretend vendor that read our
 * domain tables would prove nothing, because the whole point of the sweeper is
 * asking a party that knows something we do not.
 *
 * One deliberate difference from the real API: this returns `result_at`, a
 * vendor-side timestamp for the current status. Didit's real decision endpoint
 * exposes no such field, which is why the worker's recency guard treats it as
 * optional. Including it here means the guard is actually exercised by the
 * tests rather than being dead code on the only path we can run.
 */

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  if ((process.env.DIDIT_MODE ?? "simulator") === "live") {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  const { id } = await params;
  if (!/^[0-9a-f-]{36}$/i.test(id)) {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  const { rows } = await pool.query<{
    session_id: string;
    vendor_data: string;
    status: string;
    webhook_delivered: boolean;
    created_at: string;
    result_at: string;
  }>(
    `select session_id, vendor_data, status, webhook_delivered,
            created_at::text, result_at::text
       from mock_vendor_sessions
      where session_id = $1`,
    [id],
  );

  const session = rows[0];
  if (!session) {
    // 404 is meaningful, not an error: it means the vendor has no record of
    // this session. The worker treats that as permanent rather than retrying.
    return Response.json({ error: "session not found" }, { status: 404 });
  }

  return Response.json({
    session_id: session.session_id,
    session_kind: "user",
    status: session.status,
    vendor_data: session.vendor_data,
    workflow_id: process.env.DIDIT_WORKFLOW_ID || "simulator-workflow",
    workflow_version: 3,
    created_at: session.created_at,
    // When this status was reached, by the vendor's clock.
    result_at: session.result_at,
    features: [],
    // Not part of Didit's contract — visible here so a reader can see whether a
    // delivery was suppressed on purpose while staging the sweeper test.
    simulator_webhook_delivered: session.webhook_delivered,
  });
}
