"use server";

import { revalidatePath } from "next/cache";
import { withTransaction } from "@/lib/db";
import { logEvent } from "@/lib/audit";
import { actorFor, requireStaff } from "@/lib/session";

export interface DecideState {
  error?: string;
  /** Set when the case turned out to be decided already. */
  alreadyDecidedBy?: string;
  reason?: string;
}

/** Long enough to be a sentence. Short enough not to be an obstacle. */
const MIN_REASON_LENGTH = 15;

/** Postgres unique-violation. The backstop that makes deciding twice impossible. */
const UNIQUE_VIOLATION = "23505";

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

  try {
    const result = await withTransaction(async (client) => {
      // THE LOCK.
      //
      // Plain FOR UPDATE, not FOR UPDATE SKIP LOCKED — the opposite modifier
      // from the job queue, for the opposite reason.
      //
      // The queue skips contended rows because another worker will take them
      // and nobody needs telling. Here the loser of a race is a PERSON waiting
      // for an answer, and "nothing happened" is the worst possible response.
      // So the second transaction BLOCKS here until the first commits, then
      // re-reads and finds the case already decided — which is what lets it
      // say who decided it.
      const locked = await client.query<{ status: string }>(
        `select status from applications where id = $1 for update`,
        [applicationId],
      );
      if (locked.rows.length === 0) {
        return { error: "No such case." } as DecideState;
      }

      // Re-read AFTER acquiring the lock. Anything read before it is a guess
      // about a row someone else may have been changing.
      const existing = await client.query<{ decided_by: string; outcome: string }>(
        `select decided_by, outcome from decisions
          where application_id = $1 and outcome in ('approved', 'rejected')
          limit 1`,
        [applicationId],
      );
      if (existing.rows.length > 0) {
        return { alreadyDecidedBy: existing.rows[0].decided_by } as DecideState;
      }

      const scored = await client.query<{ risk_score: number | null }>(
        `select risk_score from applications where id = $1`,
        [applicationId],
      );

      await client.query(
        `insert into decisions
           (application_id, outcome, decided_by, reason, risk_score_at_decision)
         values ($1, $2, $3, $4, $5)`,
        [
          applicationId,
          outcome,
          actorFor(session),
          reason,
          scored.rows[0]?.risk_score ?? null,
        ],
      );

      // screening -> decided. Guarded in the WHERE clause as well as by the
      // lock, so the transition cannot be applied to a case that has moved on.
      await client.query(
        `update applications set status = 'decided'
          where id = $1 and status = 'screening'`,
        [applicationId],
      );

      await logEvent(client, {
        applicationId,
        actor: actorFor(session),
        action: "decision.recorded",
        details: {
          outcome,
          decided_by: actorFor(session),
          reason,
          automatic: false,
          risk_score: scored.rows[0]?.risk_score ?? null,
        },
      });

      return {} as DecideState;
    });

    if (result.error || result.alreadyDecidedBy) {
      return result;
    }
  } catch (err) {
    // THE BACKSTOP.
    //
    // decisions_one_terminal_per_application (migration 007) is a partial
    // unique index over approved/rejected rows. Even with a bug in every layer
    // above — the hidden buttons, the lock, the re-read — Postgres refuses the
    // second insert and it does not exist. That is what makes deciding twice
    // impossible rather than merely discouraged.
    //
    // Reached here only if the guards above are bypassed or racing in a way
    // they should not; treated as the same outcome for the officer either way.
    if (
      typeof err === "object" &&
      err !== null &&
      (err as { code?: string }).code === UNIQUE_VIOLATION
    ) {
      return { alreadyDecidedBy: "another officer" };
    }
    throw err;
  }

  // Re-render the case and the queue with the new state. Not a redirect: the
  // officer should see the decision they just made recorded on the case,
  // rather than being thrown back to the list wondering whether it worked.
  revalidatePath(`/desk/${applicationId}`);
  revalidatePath("/desk");
  return {};
}
