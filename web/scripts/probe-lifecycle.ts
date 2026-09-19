/**
 * Proof that the lifecycle rule binds the TypeScript service too.
 *
 *   npm run probe:lifecycle
 *
 * The point of migration 017 is that the rule no longer lives in Python. This
 * goes through `pg` — the same driver, pool and connection the web service uses
 * — and issues the transitions the WHERE-clause convention used to prevent,
 * with the WHERE clause deliberately removed.
 *
 * Before 017 every one of these would have succeeded.
 */

import path from "node:path";
import { config as loadEnv } from "dotenv";

loadEnv({ path: path.resolve(process.cwd(), "..", ".env") });

const { pool, withTransaction } = await import("../src/lib/db.ts");

/** Postgres SQLSTATE for our RAISE ... USING errcode = 'restrict_violation'. */
const RESTRICT_VIOLATION = "23001";

async function attempt(
  label: string,
  applicationId: string,
  from: string,
  to: string,
): Promise<void> {
  try {
    await withTransaction(async (client) => {
      // No `and status = '...'` guard. That guard is the convention this
      // migration replaces, and leaving it in would test the convention
      // rather than the schema.
      await client.query(`update applications set status = $1 where id = $2`, [
        to,
        applicationId,
      ]);
    });
    console.log(`  ${label}\n    ✗ ACCEPTED — the rule is not being enforced`);
    process.exitCode = 1;
  } catch (err) {
    const code = (err as { code?: string }).code;
    const message = (err as { message?: string }).message ?? String(err);
    if (code === RESTRICT_VIOLATION) {
      console.log(`  ${label}\n    ✓ refused by the database: ${message}`);
    } else {
      console.log(`  ${label}\n    ? refused, but with ${code}: ${message}`);
      process.exitCode = 1;
    }
  }
}

const { rows: started } = await pool.query<{ id: string }>(
  `select id from applications where status = 'started' limit 1`,
);
const { rows: decided } = await pool.query<{ id: string }>(
  `select id from applications where status = 'decided' limit 1`,
);

if (!started[0] || !decided[0]) {
  console.error("need one 'started' and one 'decided' application; run seed.py");
  process.exit(1);
}

console.log("Attempting illegal transitions through pg, with no WHERE guard:\n");

await attempt(
  "started -> decided   (deciding on someone never screened)",
  started[0].id,
  "started",
  "decided",
);
await attempt(
  "decided -> checking  (a late webhook reopening a closed case)",
  decided[0].id,
  "decided",
  "checking",
);
await attempt(
  "decided -> submitted (backwards from terminal)",
  decided[0].id,
  "decided",
  "submitted",
);

console.log("\nAnd a legal one, to show the trigger is not simply blocking everything:");
const { rows: fresh } = await pool.query<{ id: string; status: string }>(
  `select id, status from applications where status = 'started' limit 1`,
);
try {
  await withTransaction(async (client) => {
    await client.query(`update applications set status = 'submitted' where id = $1`, [
      fresh[0].id,
    ]);
    const { rows } = await client.query<{ status: string }>(
      `select status from applications where id = $1`,
      [fresh[0].id],
    );
    console.log(`  started -> submitted\n    ✓ accepted, now '${rows[0].status}'`);
    // Roll back so the probe leaves the database as it found it.
    throw new Error("probe rollback");
  });
} catch (err) {
  if ((err as Error).message !== "probe rollback") throw err;
}

await pool.end();
