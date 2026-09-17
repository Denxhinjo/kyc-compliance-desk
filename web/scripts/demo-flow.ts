/**
 * Phase 4 test harness: drive an applicant through the flow from the command
 * line, using the same code paths the browser uses.
 *
 *   npm run demo -- new                                   create an applicant + session
 *   npm run demo -- complete <session> Approved           vendor decides, webhook delivered
 *   npm run demo -- complete <session> Approved --drop    vendor decides, delivery lost
 *   npm run demo -- complete <session> "In Review" --at <unixSeconds>
 *   npm run demo -- show <application>                    status + timeline
 *
 * --at backdates the vendor's event timestamp, which is how an out-of-order
 * delivery is staged: a result the vendor produced earlier, arriving now.
 */

import path from "node:path";
import { config as loadEnv } from "dotenv";

loadEnv({ path: path.resolve(process.cwd(), "..", ".env") });

const { pool, withTransaction } = await import("../src/lib/db.ts");
const { logEvent } = await import("../src/lib/audit.ts");
const { createVerificationSession } = await import("../src/lib/didit.ts");
const { completeSession } = await import("../src/lib/mock-vendor.ts");

const argv = process.argv.slice(2);

/** Flags that take a value, so their value is not mistaken for a positional. */
const VALUED_FLAGS = new Set(["at"]);

const flags: Record<string, string | true> = {};
const positional: string[] = [];
for (let i = 0; i < argv.length; i++) {
  const token = argv[i];
  if (!token.startsWith("--")) {
    positional.push(token);
    continue;
  }
  const name = token.slice(2);
  if (VALUED_FLAGS.has(name)) {
    flags[name] = argv[++i] ?? "";
  } else {
    flags[name] = true;
  }
}

const [command, ...rest] = positional;

async function createApplicant(): Promise<void> {
  const names = [
    ["Fatima Al-Rashid", "1988-03-14", "London", "EC2N 4AJ", "GB"],
    ["Ahmed Hassan", "1975-11-02", "Paris", "75002", "FR"],
    ["Elena Vasquez", "1992-07-21", "Madrid", "28013", "ES"],
    ["Tomas Novak", "1983-01-09", "Prague", "11000", "CZ"],
  ];
  const [name, dob, city, postcode, country] =
    names[Math.floor(Math.random() * names.length)];

  const applicationId = await withTransaction(async (client) => {
    const { rows } = await client.query<{ id: string }>(
      `insert into applications
         (status, full_name, date_of_birth, address_line1, address_city,
          address_postcode, address_country)
       values ('started', $1, $2, '1 Demo Street', $3, $4, $5)
       returning id`,
      [name, dob, city, postcode, country],
    );
    const id = rows[0].id;
    await logEvent(client, {
      applicationId: id,
      actor: "system:web",
      action: "application.created",
      details: { channel: "demo-flow", country },
    });
    return id;
  });

  const session = await createVerificationSession(applicationId);

  await withTransaction(async (client) => {
    await client.query(
      `update applications
          set status = 'submitted', vendor_applicant_id = $1
        where id = $2 and status = 'started'`,
      [session.sessionId, applicationId],
    );
    await logEvent(client, {
      applicationId,
      actor: "system:web",
      action: "verification.session_created",
      details: {
        vendor: "didit",
        session_id: session.sessionId,
        vendor_status: session.status,
        status_to: "submitted",
      },
    });
  });

  console.log(`application ${applicationId}   (${name})`);
  console.log(`session     ${session.sessionId}`);
}

async function show(applicationId: string): Promise<void> {
  const app = await pool.query<{
    status: string;
    full_name: string;
    vendor_status: string | null;
    vendor_result_at: string | null;
    updated_at: string;
  }>(
    `select status, full_name, vendor_status, vendor_result_at::text,
            updated_at::text
       from applications where id = $1`,
    [applicationId],
  );
  if (!app.rows[0]) {
    console.log("no such application");
    return;
  }
  const row = app.rows[0];
  console.log(`${row.full_name}`);
  console.log(`  status           ${row.status}`);
  console.log(`  vendor_status    ${row.vendor_status ?? "-"}`);
  console.log(`  vendor_result_at ${row.vendor_result_at ?? "-"}`);
  console.log(`  updated_at       ${row.updated_at}`);

  const events = await pool.query<{
    id: string;
    occurred_at: string;
    actor: string;
    action: string;
    details: Record<string, unknown>;
  }>(
    `select id::text, occurred_at::text, actor, action, details
       from audit_events where application_id = $1 order by id`,
    [applicationId],
  );
  console.log(`  timeline (${events.rows.length})`);
  for (const event of events.rows) {
    console.log(
      `    ${event.occurred_at.slice(11, 19)}  ${event.action.padEnd(28)} ${JSON.stringify(event.details)}`,
    );
  }
}

switch (command) {
  case "new":
    await createApplicant();
    break;
  case "complete": {
    const [sessionId, ...statusParts] = rest;
    const at = typeof flags.at === "string" ? flags.at : undefined;
    const status = statusParts.join(" ") || "Approved";
    const outcome = await completeSession(sessionId, status, {
      deliverWebhook: flags.drop !== true,
      timestampSeconds: at ? Number(at) : undefined,
    });
    console.log(
      `vendor recorded "${outcome.status}" for session ${outcome.sessionId}`,
    );
    console.log(
      outcome.delivered
        ? `  webhook delivered: HTTP ${outcome.webhookStatus}  event ${outcome.eventId}`
        : `  webhook DROPPED — the vendor knows, this application does not`,
    );
    break;
  }
  case "show":
    await show(rest[0]);
    break;
  default:
    console.error("commands: new | complete <session> <status> [--drop] [--at <s>] | show <application>");
    process.exitCode = 1;
}

await pool.end();
