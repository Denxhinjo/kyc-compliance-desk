"use server";

import { redirect } from "next/navigation";
import { startVerificationSession } from "@/lib/verification";

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

  // The session creation and its bookkeeping live in lib/verification.ts,
  // because the continuous seeder has to create REAL simulator sessions
  // through this exact path. The previous bulk seeder wrote a random session
  // id instead and left 38 applications pointing at sessions the vendor had
  // never heard of — see the note in that file.
  let session;
  try {
    session = await startVerificationSession(applicationId);
  } catch (err) {
    return {
      error: err instanceof Error ? err.message : "could not reach the vendor",
    };
  }

  redirect(session.url);
}
