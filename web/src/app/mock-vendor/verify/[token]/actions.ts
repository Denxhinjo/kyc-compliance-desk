"use server";

import { redirect } from "next/navigation";
import { completeSession } from "@/lib/mock-vendor";
import { decodeToken } from "./envelope";

/**
 * SIMULATOR — the applicant finishing at the vendor's hosted screen.
 *
 * Two things happen, and keeping them separable is the point of this phase:
 *
 *   1. The VENDOR records the outcome. Always — it is what the vendor knows,
 *      independently of whether we ever hear about it.
 *   2. The vendor DELIVERS a webhook. This can be suppressed, which is how a
 *      lost delivery is staged for the sweeper test.
 *
 * Real delivery failures happen for reasons outside anyone's control: a deploy,
 * a 500, an expired tunnel, a firewall change. The vendor still knows the
 * answer; we simply never heard it. That gap is what the sweeper closes.
 *
 * The work itself lives in @/lib/mock-vendor so this screen and the scripted
 * test harness drive exactly the same code.
 */
export async function completeVerification(
  _previous: { error?: string },
  form: FormData,
): Promise<{ error: string } | never> {
  const token = String(form.get("token") ?? "");
  const status = String(form.get("status") ?? "Approved");
  const dropWebhook = form.get("dropWebhook") === "on";

  if ((process.env.DIDIT_MODE ?? "simulator") === "live") {
    return { error: "simulator is disabled in live mode" };
  }

  const session = decodeToken(token);
  if (!session) return { error: "invalid session token" };

  let outcome;
  try {
    outcome = await completeSession(session.sessionId, status, {
      deliverWebhook: !dropWebhook,
    });
  } catch (err) {
    return { error: err instanceof Error ? err.message : "simulator failed" };
  }

  if (outcome.delivered && outcome.webhookStatus !== 200) {
    return { error: `webhook returned HTTP ${outcome.webhookStatus}` };
  }

  redirect(
    dropWebhook
      ? `/status/${session.vendorData}?delivery=dropped`
      : `/status/${session.vendorData}`,
  );
}
