/**
 * Phase 1 evidence: the TypeScript side of the audit log.
 *
 * Creates one application and, in the SAME transaction, the audit event that
 * records its creation. Then deliberately fails a second transaction to show
 * that a state change and its audit row cannot come apart.
 *
 * Run from /web:
 *   npm run smoke:audit
 */

import path from "node:path";
import { config as loadEnv } from "dotenv";

// This script runs outside Next.js, so next.config.ts is not loaded and the
// root .env has to be read explicitly.
loadEnv({ path: path.resolve(process.cwd(), "..", ".env") });

const { pool, withTransaction } = await import("../src/lib/db.ts");
const { logEvent } = await import("../src/lib/audit.ts");

async function createApplication(): Promise<string> {
  return withTransaction(async (client) => {
    const { rows } = await client.query<{ id: string }>(
      `insert into applications
         (full_name, date_of_birth,
          address_line1, address_city, address_postcode, address_country)
       values ($1, $2, $3, $4, $5, $6)
       returning id`,
      [
        "Fatima Al-Rashid",
        "1988-03-14",
        "12 Bishopsgate",
        "London",
        "EC2N 4AJ",
        "GB",
      ],
    );
    const id = rows[0].id;

    await logEvent(client, {
      applicationId: id,
      actor: "system:web",
      action: "application.created",
      details: { source: "smoke-audit.ts", channel: "web_form" },
    });

    return id;
  });
}

/**
 * Prove the transaction really does bind the two writes together.
 *
 * Moves the application to 'submitted', logs the transition, then throws. Both
 * writes should vanish. If logEvent() opened its own connection instead of
 * joining this transaction, the audit row would survive and would be claiming
 * a status change that never happened.
 */
async function failedTransition(applicationId: string): Promise<void> {
  try {
    await withTransaction(async (client) => {
      await client.query(
        "update applications set status = 'submitted' where id = $1",
        [applicationId],
      );
      await logEvent(client, {
        applicationId,
        actor: "system:web",
        action: "status.changed",
        details: { from: "started", to: "submitted" },
      });
      throw new Error("simulated failure after both writes");
    });
  } catch (err) {
    console.log(
      `  rolled back as expected: ${err instanceof Error ? err.message : err}`,
    );
  }
}

async function main(): Promise<void> {
  console.log("1. creating an application + its audit event in one transaction");
  const id = await createApplication();
  console.log(`   application ${id}`);

  console.log("\n2. a transition that fails partway through");
  await failedTransition(id);

  const { rows } = await pool.query<{
    status: string;
    audit_rows: string;
  }>(
    `select a.status,
            (select count(*) from audit_events e where e.application_id = a.id)::text
              as audit_rows
       from applications a
      where a.id = $1`,
    [id],
  );

  console.log(`   status is still '${rows[0].status}' (not 'submitted')`);
  console.log(`   audit rows for this application: ${rows[0].audit_rows} (not 2)`);

  await pool.end();
}

await main();
