"use server";

import { redirect } from "next/navigation";
import { withTransaction } from "@/lib/db";
import { logEvent } from "@/lib/audit";
import { createVerificationSession } from "@/lib/didit";

/**
 * Start a verification session and send the applicant to the vendor.
 *
 * Ordering matters here. The vendor call happens FIRST, outside the
 * transaction, because an HTTP request cannot be rolled back — holding a
 * database transaction open across a network call to a third party is how you
 * end up with locks held for however long their API takes to respond.
 */
export async function startVerification(
  _previous: { error?: string },
  form: FormData,
): Promise<{ error: string } | never> {
  // Read the id from the form rather than closing over it. That keeps this a
  // direct server action reference, which is what lets <form action={...}>
  // submit without JavaScript — an inline wrapper in the client component would
  // make it a client function and quietly drop progressive enhancement.
  const applicationId = String(form.get("applicationId") ?? "");
  if (!/^[0-9a-f-]{36}$/i.test(applicationId)) {
    return { error: "invalid application id" };
  }

  let session;
  try {
    session = await createVerificationSession(applicationId);
  } catch (err) {
    return {
      error: err instanceof Error ? err.message : "could not reach the vendor",
    };
  }

  await withTransaction(async (client) => {
    await client.query(
      `update applications
          set vendor_applicant_id = $1
        where id = $2`,
      [session.sessionId, applicationId],
    );

    await logEvent(client, {
      applicationId,
      actor: "system:web",
      action: "verification.session_created",
      details: {
        vendor: "didit",
        session_id: session.sessionId,
        vendor_status: session.status,
      },
    });
  });

  redirect(session.url);
}
