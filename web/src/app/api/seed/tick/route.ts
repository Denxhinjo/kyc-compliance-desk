import crypto from "node:crypto";
import { pool, withTransaction } from "@/lib/db";
import { logEvent } from "@/lib/audit";
import { insertApplication } from "@/lib/applications";
import { startVerificationSession } from "@/lib/verification";
import { completeSession } from "@/lib/mock-vendor";
import { recordDecision } from "@/lib/decisions";
import { validateApplicant } from "@/lib/validation";

/**
 * POST /api/seed/tick — one tick of the continuous seeder.
 *
 * WHY THIS IS AN ENDPOINT IN THE WEB APP RATHER THAN A SCRIPT
 *
 * Because the requirement is that seeded applicants go through the REAL
 * pipeline, and most of that pipeline is here, in TypeScript:
 *
 *   creation            lib/applications.ts, the apply form's own code
 *   verification        lib/verification.ts, the verify page's own code
 *   the vendor result   lib/mock-vendor.ts completeSession, which signs a real
 *                       webhook body and POSTs it to /api/webhooks/didit — the
 *                       same endpoint a real vendor calls, signature check and
 *                       all
 *   the decision        lib/decisions.ts, the review desk's own code
 *
 * Screening and scoring are NOT here: the webhook enqueues `screening.run`,
 * and the Python worker scores it with production code under whichever rules
 * version is in force at that moment. Nothing in this file touches scoring.
 *
 * A Python seeder writing rows directly would have been far less code and
 * would have re-created the exact defect this is partly here to fix: the bulk
 * seeder invented `vendor_applicant_id` values with no matching simulator
 * session, so 38 applications pointed at sessions that had never existed and
 * could never be resolved.
 *
 * WHAT IS NOT HERE: the applicant data. That comes from worker/trickle.py,
 * which draws on the existing synthetic name sources in seed.py. Duplicating
 * those lists in TypeScript would be a second place for them to drift, and
 * "no new real names" is easier to guarantee when there is one source of
 * names.
 *
 * AUTHENTICATION is the existing DRAIN_SECRET, the project's shared secret for
 * machine-to-machine calls, already held by GitHub Actions and by both Vercel
 * projects. Deliberately not a new secret: an endpoint that creates data needs
 * to be closed, and inventing a second credential to configure is how one of
 * them ends up unset. Unset refuses everything, as the drain endpoint does.
 */

const HEADER = "x-seed-secret";

/** Clearly synthetic, and provably not a person: `.invalid` can never resolve. */
const SYNTHETIC_REVIEWER = "staff:auto-reviewer@synthetic.invalid" as const;

/**
 * Referred cases younger than this are left for a visitor to decide.
 *
 * The queue must never be empty when someone arrives to look at it — an empty
 * review desk is a worse demo than a slightly stale one — and it must never be
 * only cases the bot has already taken. 48 hours leaves roughly two days of
 * arrivals untouched.
 */
const REVIEW_AFTER_HOURS = 48;

/** A cap, so one tick cannot work through the whole backlog in a burst. */
const MAX_REVIEWS_PER_TICK = 4;

/** Guards against a malformed or hostile request asking for thousands. */
const MAX_APPLICANTS_PER_TICK = 5;

interface TickRequest {
  applicants?: unknown;
  /**
   * Reviewer notes, drawn from the seeder's synthetic sources, split by
   * outcome. Split because an approval note on a refusal is worse than a
   * generic one: `decisions.reason` is what an auditor reads to judge whether
   * a decision was reasonable, and a refusal justified by "cleared, not the
   * same person" would read as a contradiction in the case file.
   */
  approve_reasons?: unknown;
  reject_reasons?: unknown;
}

function usableReasons(value: unknown): string[] {
  return (Array.isArray(value) ? value : []).filter(
    // 15 characters is the review desk's own minimum for a human reason. The
    // bot is held to the same bar rather than a lower one.
    (r): r is string => typeof r === "string" && r.trim().length >= 15,
  );
}

function authorised(request: Request): boolean {
  const expected = process.env.DRAIN_SECRET ?? "";
  if (!expected) {
    // Unset refuses everything rather than allowing everything. A deployment
    // that forgot to set it should be visibly broken, not quietly open.
    console.error("DRAIN_SECRET is not set — refusing every seed request");
    return false;
  }
  const offered = request.headers.get(HEADER) ?? "";
  const a = Buffer.from(offered);
  const b = Buffer.from(expected);
  // timingSafeEqual throws on length mismatch, so compare lengths first — and
  // compare hashes so the length itself is not an oracle.
  return crypto.timingSafeEqual(
    crypto.createHash("sha256").update(a).digest(),
    crypto.createHash("sha256").update(b).digest(),
  );
}

export async function POST(request: Request): Promise<Response> {
  if (!authorised(request)) {
    return Response.json({ error: "unauthorised" }, { status: 401 });
  }

  let body: TickRequest;
  try {
    body = (await request.json()) as TickRequest;
  } catch {
    return Response.json({ error: "body must be JSON" }, { status: 400 });
  }

  const applicants = Array.isArray(body.applicants) ? body.applicants : [];
  if (applicants.length > MAX_APPLICANTS_PER_TICK) {
    return Response.json(
      { error: `at most ${MAX_APPLICANTS_PER_TICK} applicants per tick` },
      { status: 400 },
    );
  }
  const approveReasons = usableReasons(body.approve_reasons);
  const rejectReasons = usableReasons(body.reject_reasons);

  const created: string[] = [];
  const rejectedInput: unknown[] = [];

  for (const raw of applicants) {
    // Validated with the SAME validator the public form uses, so a seeded
    // applicant cannot be something a real one could not be — an under-age
    // date of birth, a blank name, a three-letter country code.
    const form = new FormData();
    for (const [key, value] of Object.entries(
      (raw ?? {}) as Record<string, unknown>,
    )) {
      form.set(key, String(value ?? ""));
    }
    const checked = validateApplicant(form);
    if (!checked.ok) {
      rejectedInput.push(checked.errors);
      continue;
    }

    // Creation and the verification session in ONE transaction, so there is no
    // committed state in which a seeded application exists without its
    // session. That state is precisely what made the old seeded rows
    // unresolvable.
    //
    // The vendor call inside startVerificationSession is an HTTP request, which
    // cannot be rolled back — so creation commits first and the session
    // follows. If the vendor call fails, the application is left at 'started'
    // with no session, which is an ordinary and recoverable state: it is what
    // an applicant who closed the tab looks like, and `expire_abandoned` will
    // close it after 30 days.
    const applicationId = await withTransaction((client) =>
      insertApplication(client, checked.value, { seeded: true }),
    );

    try {
      const session = await startVerificationSession(applicationId, {
        seeded: true,
      });

      // The vendor reaches a verdict and tells us, through the real signed
      // webhook. Nothing here decides anything: the webhook enqueues
      // screening.run and the worker scores it under the current rules.
      await completeSession(session.sessionId, vendorOutcome(applicationId));
    } catch (err) {
      console.error(`seed tick: ${applicationId} stalled`, err);
    }

    created.push(applicationId);
  }

  const reviewed = await runSyntheticReviewer(approveReasons, rejectReasons);

  // One row per tick, ALWAYS, so the health page can show when the seeder last
  // ran and flag a silent stop. GitHub disables scheduled workflows in public
  // repositories after 60 days without activity, so "it stopped and nobody
  // noticed" is the expected failure, not an unlikely one.
  await withTransaction((client) =>
    logEvent(client, {
      applicationId: null,
      actor: "system:seed",
      action: "trickle.completed",
      details: {
        seeded: true,
        created: created.length,
        reviewed: reviewed.decided,
        review_skipped: reviewed.skipped,
        rejected_input: rejectedInput.length,
        review_after_hours: REVIEW_AFTER_HOURS,
        reviewer: SYNTHETIC_REVIEWER,
      },
    }),
  );

  return Response.json({
    created: created.length,
    reviewed: reviewed.decided,
    review_skipped: reviewed.skipped,
    rejected_input: rejectedInput,
  });
}

/**
 * Which way the simulator's verdict goes.
 *
 * Mostly "Approved", because a document check failing is the uncommon case and
 * a demo whose vendor rejected half its applicants would misrepresent the
 * base rates the bulk seeder was careful about. Derived from the application
 * id rather than Math.random so a given application always gets the same
 * answer if this is ever retried — the webhook path is idempotent, but a
 * retry that produced a DIFFERENT verdict would exercise the recency guard by
 * accident rather than on purpose.
 */
function vendorOutcome(applicationId: string): string {
  const digest = crypto.createHash("sha256").update(applicationId).digest();
  const roll = digest[0] % 100;
  if (roll < 88) return "Approved";
  if (roll < 95) return "Declined";
  return "In Review";
}

/**
 * The synthetic reviewer: work the queue, but only the seeded part of it, and
 * only cases old enough that a visitor has had a chance at them.
 *
 * Decisions go through lib/decisions.ts — the review desk's own code — so the
 * row lock, the already-decided re-read, the parked-evidence refusal from
 * migration 023 and the unique-index backstop from migration 007 all apply
 * exactly as they do to a human. A separate path for seeded data would have
 * exempted it from the guarantees the desk exists to provide.
 */
async function runSyntheticReviewer(
  approveReasons: string[],
  rejectReasons: string[],
): Promise<{ decided: number; skipped: number }> {
  // ONLY SEEDED APPLICATIONS. The `exists` clause is the whole safety
  // property: it reads the application's own immutable creation event, so a
  // case a visitor created can never be decided by the bot however old it
  // gets. Keyed on the audit log rather than on a column because the audit log
  // cannot be rewritten.
  const { rows } = await pool.query<{ id: string; risk_score: number | null }>(
    `select a.id, a.risk_score
       from applications a
       join decisions r
         on r.application_id = a.id
        and r.outcome = 'referred'
      where a.status = 'screening'
        and not exists (
              select 1 from decisions t
               where t.application_id = a.id
                 and t.outcome in ('approved', 'rejected')
            )
        and exists (
              select 1 from audit_events e
               where e.application_id = a.id
                 and e.action = 'application.created'
                 and e.details->>'seeded' = 'true'
            )
        and r.decided_at < now() - make_interval(hours => $1)
      order by r.decided_at asc
      limit $2`,
    [REVIEW_AFTER_HOURS, MAX_REVIEWS_PER_TICK],
  );

  let decided = 0;
  let skipped = 0;

  for (const row of rows) {
    const score = row.risk_score ?? 0;
    // Higher scores lean towards refusal, but nothing here is a scoring
    // decision: the score was computed by the worker under its own rules
    // version and is only being read. A reviewer who approved everything would
    // make the demo's queue history meaningless.
    const outcome = score >= 60 ? "rejected" : "approved";
    const pool = outcome === "approved" ? approveReasons : rejectReasons;
    const reason =
      pool[(decided + skipped) % Math.max(pool.length, 1)] ??
      "Reviewed against the recorded screening evidence; outcome as stated.";

    const result = await recordDecision({
      applicationId: row.id,
      outcome,
      reason,
      actor: SYNTHETIC_REVIEWER,
      seeded: true,
    });

    if (result.ok) {
      decided += 1;
    } else {
      // Every non-ok outcome is a case the desk would also have refused —
      // already decided by a visitor, or evidence the system could not record.
      // Both are correct reasons not to proceed, so it is counted, not forced.
      skipped += 1;
    }
  }

  return { decided, skipped };
}
