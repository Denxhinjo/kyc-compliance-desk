import { withTransaction } from "@/lib/db";
import { logEvent, type Actor } from "@/lib/audit";
import { seededMarker } from "@/lib/seeded";

/**
 * Recording a decision, in one place.
 *
 * WHY THIS WAS EXTRACTED
 *
 * The review desk's server action used to hold all of this inline. The
 * synthetic reviewer that keeps the demo's queue moving must decide cases
 * through the SAME code, so that every check the desk applies also applies to
 * it — the row lock, the already-decided re-read, the parked-evidence refusal
 * from migration 023, the unique-index backstop from migration 007, and the
 * rule that the stored score and ruleset version come from the application
 * rather than being recomputed.
 *
 * A second implementation for the seeder would have been the obvious shortcut
 * and would have quietly exempted demo data from the guarantees the demo is
 * about. Worse, it would have been the version nobody reviewed, running
 * unattended every hour.
 *
 * The only differences between a real decision and a seeded one are the actor
 * and a flag in the audit details. Everything that could go wrong is shared.
 */

/** Postgres unique-violation: decisions_one_terminal_per_application (007). */
const UNIQUE_VIOLATION = "23505";

/** Raised here only by the stuck-evidence guard, migration 023. */
const RESTRICT_VIOLATION = "23001";

export type DecisionOutcome = "approved" | "rejected";

export interface RecordDecisionRequest {
  applicationId: string;
  outcome: DecisionOutcome;
  reason: string;
  /**
   * Who is deciding, as it will be stored in `decisions.decided_by`.
   *
   * Typed as Actor rather than string so the prefix is not optional: an
   * unprefixed value would be indistinguishable from the automatic path, and
   * `decided_by = 'system'` is what the stats page counts as auto-decided. The
   * synthetic reviewer is therefore a `staff:` actor like any other reviewer,
   * which is also what keeps it out of the automatic figures.
   */
  actor: Actor;
}

/*
 * NOTE ON `"seeded"`: it is NOT a parameter here, deliberately.
 *
 * It used to be, and the desk simply did not pass it — so a visitor approving
 * a seeded case wrote an unmarked event while the synthetic reviewer, three
 * lines away in the same file, wrote a marked one. A flag a caller has to
 * remember is a flag a caller will forget.
 *
 * It is now looked up inside the transaction below, from the application's own
 * creation event, so every write path through this function is marked
 * correctly without knowing the rule exists. See lib/seeded.ts for what the
 * mark means: it describes the APPLICATION, not who acted.
 */

export type RecordDecisionResult =
  | { ok: true }
  | { ok: false; kind: "no_such_case" }
  | { ok: false; kind: "already_decided"; decidedBy: string }
  | { ok: false; kind: "evidence_stuck"; jobId: string | null };

/**
 * The transaction. Shared verbatim between the desk and the synthetic reviewer.
 *
 * Returns a discriminated result rather than throwing for the expected
 * outcomes, so each caller can phrase them for its own audience — a sentence
 * for an officer, a counter for a scheduled job — without either having to
 * reimplement the decision itself.
 */
export async function recordDecision(
  request: RecordDecisionRequest,
): Promise<RecordDecisionResult> {
  const { applicationId, outcome, reason, actor } = request;

  try {
    return await withTransaction<RecordDecisionResult>(async (client) => {
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
      //
      // A test reads this file and fails if it ever becomes SKIP LOCKED, since
      // that would leave every database guarantee intact while telling the
      // losing officer the case does not exist.
      const locked = await client.query<{ status: string }>(
        `select status from applications where id = $1 for update`,
        [applicationId],
      );
      if (locked.rows.length === 0) {
        return { ok: false, kind: "no_such_case" };
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
        return {
          ok: false,
          kind: "already_decided",
          decidedBy: existing.rows[0].decided_by,
        };
      }

      // Evidence the system could not record.
      //
      // Migration 023 refuses this insert outright, so the check here is not
      // the guarantee — it exists so the caller gets an explanation rather
      // than a database error. Read inside the same transaction and after the
      // lock, for the same reason the decision check is.
      const stuck = await client.query<{ id: string }>(
        `select id::text from jobs
          where job_type = 'screening.run'
            and status = 'parked'
            and payload->>'application_id' = $1
          limit 1`,
        [applicationId],
      );
      if (stuck.rows.length > 0) {
        return { ok: false, kind: "evidence_stuck", jobId: stuck.rows[0].id };
      }

      // The ruleset version alongside the score, because a decision has to
      // carry its own inputs. Reading rather than recomputing is safe because
      // migration 020 freezes both columns once a decision row exists — and a
      // case reaching a reviewer is a referral, so one already does. What is
      // read here is therefore the value the referral was made on, not a later
      // re-score.
      //
      // This is also what makes a seeded decision carry whichever rules
      // version was in force when the case was SCORED, with nothing
      // backdated and nothing recomputed under today's rules.
      const scored = await client.query<{
        risk_score: number | null;
        risk_ruleset_version: string | null;
      }>(
        `select risk_score, risk_ruleset_version from applications where id = $1`,
        [applicationId],
      );

      await client.query(
        `insert into decisions
           (application_id, outcome, decided_by, reason,
            risk_score_at_decision, risk_ruleset_version_at_decision)
         values ($1, $2, $3, $4, $5, $6)`,
        [
          applicationId,
          outcome,
          actor,
          reason,
          scored.rows[0]?.risk_score ?? null,
          scored.rows[0]?.risk_ruleset_version ?? null,
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
        actor,
        action: "decision.recorded",
        details: {
          outcome,
          decided_by: actor,
          reason,
          automatic: false,
          risk_score: scored.rows[0]?.risk_score ?? null,
          // Read inside the transaction, from the application's immutable
          // creation event. Not supplied by the caller — see the note above.
          ...(await seededMarker(client, applicationId)),
        },
      });

      return { ok: true };
    });
  } catch (err) {
    // THE BACKSTOP.
    //
    // decisions_one_terminal_per_application (migration 007) is a partial
    // unique index over approved/rejected rows. Even with a bug in every layer
    // above — the hidden buttons, the lock, the re-read — Postgres refuses the
    // second insert and it does not exist. That is what makes deciding twice
    // impossible rather than merely discouraged.
    if (
      typeof err === "object" &&
      err !== null &&
      (err as { code?: string }).code === UNIQUE_VIOLATION
    ) {
      return { ok: false, kind: "already_decided", decidedBy: "another officer" };
    }
    // The same backstop for migration 023, catching the race where a screening
    // job parks between the pre-check above and this insert.
    if (
      typeof err === "object" &&
      err !== null &&
      (err as { code?: string }).code === RESTRICT_VIOLATION
    ) {
      return { ok: false, kind: "evidence_stuck", jobId: null };
    }
    throw err;
  }
}
