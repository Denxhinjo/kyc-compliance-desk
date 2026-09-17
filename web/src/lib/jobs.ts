import type { PoolClient } from "pg";

export interface EnqueueJobInput {
  /** Which handler should run this, e.g. "sumsub.fetch_result". */
  type: string;
  /** Arguments for the handler. */
  payload?: Record<string, unknown>;
  /** Earliest time the job may run. Defaults to now. */
  runAfter?: Date;
  /** Overrides the table default of 5. */
  maxAttempts?: number;
}

/**
 * Put a job on the queue.
 *
 * Like logEvent(), this takes the caller's PoolClient rather than opening its
 * own connection — and here that is not just tidiness, it is the entire reason
 * the queue lives in Postgres at all:
 *
 *   await withTransaction(async (client) => {
 *     await client.query("insert into vendor_events (...) values (...)");
 *     await enqueueJob(client, { type: "sumsub.fetch_result", payload });
 *   });
 *
 * The row and the job that processes it commit together. If the queue were
 * Redis, those two writes would live in different systems with no shared
 * transaction, and a crash between them would leave either a stored event
 * nobody will ever process, or a job pointing at a row that does not exist.
 * There is no ordering of those two lines that is correct.
 *
 * Returns the job id as a STRING. Postgres bigint can exceed what a JavaScript
 * number represents exactly (2^53), so node-postgres hands back int8 as text
 * rather than silently losing precision. Converting it to a number here would
 * reintroduce the bug the driver is protecting you from.
 */
export async function enqueueJob(
  client: PoolClient,
  job: EnqueueJobInput,
): Promise<string> {
  const { rows } = await client.query<{ id: string }>(
    `insert into jobs (job_type, payload, run_after, max_attempts)
     values ($1, $2, coalesce($3, now()), coalesce($4, 5))
     returning id`,
    [
      job.type,
      JSON.stringify(job.payload ?? {}),
      job.runAfter ?? null,
      job.maxAttempts ?? null,
    ],
  );
  return rows[0].id;
}
