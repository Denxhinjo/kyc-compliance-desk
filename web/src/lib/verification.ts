import { withTransaction } from "@/lib/db";
import { logEvent } from "@/lib/audit";
import { createVerificationSession } from "@/lib/didit";
import { seededMarker } from "@/lib/seeded";

/**
 * Starting a verification session, in one place.
 *
 * THIS IS THE EXTRACTION THAT MATTERS MOST, because skipping it is what caused
 * the bug the continuous seeder exists to stop recurring.
 *
 * The bulk seeder wrote `vendor_applicant_id` directly — a random UUID, with
 * no corresponding row in the simulator's own session store. Every one of
 * those applications therefore referred to a verification session that had
 * never existed. The sweeper dutifully asked about them on every run, the
 * simulator answered 404 because it had never heard of them, and 38
 * applications sat unresolvable in production for three weeks.
 *
 * A session id that no vendor has a record of is not a session. The only way
 * to be sure a seeded application has a real one is to create it the same way
 * an applicant does, which is what this function is for.
 */

export interface StartedSession {
  sessionId: string;
  /** Where the applicant would be sent. The simulator's own page. */
  url: string;
  vendorStatus: string;
  /** True when this call moved the application out of 'started'. */
  movedToSubmitted: boolean;
}

/*
 * `seeded` is not an option here either. By the time a verification session is
 * started the application's creation event exists, so the mark can be read
 * rather than passed — the same reasoning as recordDecision. See lib/seeded.ts.
 */
export type StartOptions = Record<string, never>;

/**
 * Create the session with the vendor, then record it.
 *
 * Ordering matters. The vendor call happens FIRST, outside the transaction,
 * because an HTTP request cannot be rolled back — holding a database
 * transaction open across a network call to a third party is how you end up
 * with locks held for however long their API takes to respond.
 */
export async function startVerificationSession(
  applicationId: string,
): Promise<StartedSession> {
  const session = await createVerificationSession(applicationId);

  const movedToSubmitted = await withTransaction(async (client) => {
    // The web service makes exactly ONE lifecycle transition, and guards it in
    // the WHERE clause: this can only ever move an application forward out of
    // 'started'. Every vendor-driven transition goes through the state machine
    // in worker/lifecycle.py instead — duplicating that machine in TypeScript
    // would mean two copies of the rules that must never disagree.
    const moved = await client.query(
      `update applications
          set status = 'submitted', vendor_applicant_id = $1
        where id = $2 and status = 'started'`,
      [session.sessionId, applicationId],
    );

    // If it was not in 'started' the session id still needs recording, but the
    // status is someone else's to change.
    if (moved.rowCount === 0) {
      await client.query(
        `update applications set vendor_applicant_id = $1 where id = $2`,
        [session.sessionId, applicationId],
      );
    }

    const marker = await seededMarker(client, applicationId);

    await logEvent(client, {
      applicationId,
      // The actor says WHO; the mark says WHAT THE APPLICATION IS. A seeded
      // application's session is still created by the web service.
      actor: "system:web",
      action: "verification.session_created",
      details: {
        vendor: "didit",
        session_id: session.sessionId,
        vendor_status: session.status,
        status_to: moved.rowCount === 1 ? "submitted" : null,
        ...marker,
      },
    });

    return moved.rowCount === 1;
  });

  return {
    sessionId: session.sessionId,
    url: session.url,
    vendorStatus: session.status,
    movedToSubmitted,
  };
}
