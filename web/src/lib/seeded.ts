import type { PoolClient } from "pg";

/**
 * WHAT `"seeded": true` MEANS
 *
 *   The event belongs to a SEEDED APPLICATION — one created by a seeder rather
 *   than by a real visitor.
 *
 *   It does NOT mean "a seeder performed this action". Who acted is recorded
 *   separately, in `actor`, and the two are deliberately independent:
 *
 *     actor = "staff:auto-reviewer@synthetic.invalid"  the synthetic reviewer
 *     actor = "staff:officer@example.com"              a visitor at the desk
 *     actor = "system:worker"                          automatic processing
 *
 *   All three can act on a seeded application, and all three produce events
 *   marked `"seeded": true`, because the mark describes the APPLICATION the
 *   event is about. A visitor deciding a seeded case is a real decision by a
 *   real person about synthetic data, and `actor` is what says so.
 *
 * WHY THE DEFINITION IS WORTH WRITING DOWN
 *
 *   It has been got wrong twice. First the stats page claimed every seeded
 *   event was marked while the worker was writing unmarked ones; then the fix
 *   covered the worker and left the desk, so a visitor approving a seeded case
 *   would have written an unmarked event and falsified the page again. Both
 *   mistakes came from reading "seeded" as "a seeder did this".
 *
 * THE SOURCE OF TRUTH IS THE CREATION EVENT, not a column on the application.
 * `audit_events` is append-only, so a case cannot be made to look seeded, or
 * made to look real, after the fact.
 *
 * The Python twin is `seeded_marker()` in worker/audit.py. Same definition,
 * same lookup, same reason.
 */
export async function seededMarker(
  client: PoolClient,
  applicationId: string,
): Promise<Record<string, true> | Record<string, never>> {
  const { rows } = await client.query(
    `select 1 from audit_events
      where application_id = $1
        and action = 'application.created'
        and details->>'seeded' = 'true'
      limit 1`,
    [applicationId],
  );
  return rows.length > 0 ? { seeded: true } : {};
}
