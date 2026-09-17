/**
 * Phase 2 evidence: put jobs on the queue from the TypeScript side.
 *
 *   npm run enqueue -- --count 200                     200 demo.noop jobs
 *   npm run enqueue -- --count 1 --type demo.flaky     fails twice, then works
 *   npm run enqueue -- --count 1 --type demo.poison    always fails, parks
 *   npm run enqueue -- --count 1 --type demo.permanent parks on attempt 1
 *   npm run enqueue -- --rollback-demo                 proves transactional enqueue
 */

import path from "node:path";
import { config as loadEnv } from "dotenv";

loadEnv({ path: path.resolve(process.cwd(), "..", ".env") });

const { pool, withTransaction } = await import("../src/lib/db.ts");
const { enqueueJob } = await import("../src/lib/jobs.ts");
const { logEvent } = await import("../src/lib/audit.ts");

function arg(name: string, fallback: string): string {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

/**
 * Enqueue a batch, all in ONE transaction.
 *
 * Not for speed — to make the point that either every job in this batch is on
 * the queue or none of them is. There is no partially-enqueued batch.
 */
async function enqueueBatch(count: number, type: string): Promise<void> {
  const maxAttempts = Number(arg("max-attempts", "5"));

  const ids = await withTransaction(async (client) => {
    const created: string[] = [];
    for (let i = 0; i < count; i++) {
      created.push(
        await enqueueJob(client, {
          type,
          payload: { index: i, note: "phase 2 demo" },
          maxAttempts,
        }),
      );
    }
    return created;
  });

  console.log(`enqueued ${ids.length} × ${type}`);
  console.log(`  ids ${ids[0]}..${ids[ids.length - 1]}`);
}

/**
 * The reason the queue is a Postgres table rather than Redis.
 *
 * Creates an application and enqueues the job that would process it, in one
 * transaction, then throws. Neither survives. With the queue in a separate
 * datastore, the job would already be gone from this process's control and
 * would still be sitting in Redis pointing at an application that was never
 * committed.
 */
async function rollbackDemo(): Promise<void> {
  const before = await countRows();

  try {
    await withTransaction(async (client) => {
      const { rows } = await client.query<{ id: string }>(
        `insert into applications
           (full_name, date_of_birth, address_line1, address_city,
            address_postcode, address_country)
         values ('Rollback Demo', '1990-01-01', '1 Nowhere', 'London', 'E1 6AN', 'GB')
         returning id`,
      );
      const applicationId = rows[0].id;

      await logEvent(client, {
        applicationId,
        actor: "system:web",
        action: "application.created",
        details: { source: "rollback-demo" },
      });

      await enqueueJob(client, {
        type: "demo.noop",
        payload: { applicationId },
      });

      throw new Error("simulated failure after the row, the audit event and the job");
    });
  } catch (err) {
    console.log(`  threw as expected: ${err instanceof Error ? err.message : err}`);
  }

  const after = await countRows();
  console.log(`  applications  ${before.applications} -> ${after.applications}`);
  console.log(`  audit_events  ${before.auditEvents} -> ${after.auditEvents}`);
  console.log(`  jobs          ${before.jobs} -> ${after.jobs}`);
}

async function countRows() {
  const { rows } = await pool.query<{
    applications: string;
    audit_events: string;
    jobs: string;
  }>(
    `select (select count(*) from applications)::text as applications,
            (select count(*) from audit_events)::text as audit_events,
            (select count(*) from jobs)::text         as jobs`,
  );
  return {
    applications: rows[0].applications,
    auditEvents: rows[0].audit_events,
    jobs: rows[0].jobs,
  };
}

if (process.argv.includes("--rollback-demo")) {
  console.log("transactional enqueue: row + audit event + job, then throw");
  await rollbackDemo();
} else {
  await enqueueBatch(Number(arg("count", "1")), arg("type", "demo.noop"));
}

await pool.end();
