"use server";

import { revalidatePath } from "next/cache";
import { recordDecision } from "@/lib/decisions";
import { actorFor, requireStaff } from "@/lib/session";

export interface DecideState {
  error?: string;
  /** Set when the case turned out to be decided already. */
  alreadyDecidedBy?: string;
  reason?: string;
}

/** Long enough to be a sentence. Short enough not to be an obstacle. */
const MIN_REASON_LENGTH = 15;

// The SQLSTATE backstops for migrations 007 and 023 moved to lib/decisions.ts
// along with the transaction they guard, so that the synthetic reviewer gets
// them too rather than only this form.

export async function decide(
  _previous: DecideState,
  form: FormData,
): Promise<DecideState> {
  // FIRST LINE OF EVERY ACTION.
  //
  // The desk layout already redirects anonymous visitors, but that guards
  // NAVIGATION. This function is a POST endpoint that exists independently of
  // any page: anyone who knows its id can call it without ever rendering that
  // layout. The layout is a convenience; this is the security boundary.
  const session = await requireStaff();

  const applicationId = String(form.get("applicationId") ?? "");
  const outcome = String(form.get("outcome") ?? "");
  const reason = String(form.get("reason") ?? "").trim();

  if (!/^[0-9a-f-]{36}$/i.test(applicationId)) {
    return { error: "Invalid case id." };
  }
  if (outcome !== "approved" && outcome !== "rejected") {
    return { error: "Choose approve or reject." };
  }
  if (reason.length < MIN_REASON_LENGTH) {
    // The database enforces non-blank; this enforces "a sentence".
    //
    // Neither can force anyone to think, and pretending otherwise would be
    // silly. What they do is make the absence of a reason impossible, so an
    // auditor sampling this case always has something to read — and a
    // one-word reason is itself a finding they can act on.
    return {
      error: `Give a reason of at least ${MIN_REASON_LENGTH} characters. An auditor reads this to judge whether the decision was reasonable on the evidence.`,
      reason,
    };
  }

  // The decision itself lives in lib/decisions.ts, because the synthetic
  // reviewer that keeps the demo queue moving has to record decisions through
  // exactly this code — the lock, the already-decided re-read, the
  // parked-evidence refusal and the unique-index backstop included. A separate
  // implementation for seeded data would have exempted it from every guarantee
  // this desk is built on, unreviewed and running hourly.
  //
  // What stays here is the part that is genuinely about this audience: turning
  // each outcome into a sentence an officer can act on.
  const result = await recordDecision({
    applicationId,
    outcome,
    reason,
    actor: actorFor(session),
  });

  if (!result.ok) {
    switch (result.kind) {
      case "no_such_case":
        return { error: "No such case." };
      case "already_decided":
        return { alreadyDecidedBy: result.decidedBy };
      case "evidence_stuck":
        return {
          error: result.jobId
            ? "Screening ran again for this applicant and could not record " +
              `what it found (job #${result.jobId}). The evidence in front of ` +
              "you may be incomplete, so this case cannot be decided until " +
              "someone has looked at that job."
            : "Screening evidence for this applicant could not be recorded, " +
              "so this case cannot be decided yet. Reload to see the details.",
        };
    }
  }


  // Re-render the case and the queue with the new state. Not a redirect: the
  // officer should see the decision they just made recorded on the case,
  // rather than being thrown back to the list wondering whether it worked.
  revalidatePath(`/desk/${applicationId}`);
  revalidatePath("/desk");
  return {};
}
