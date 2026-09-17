/**
 * Phase 3 evidence: send the same webhook repeatedly, and send broken ones.
 *
 *   npm run webhook -- --application <uuid> --times 3
 *   npm run webhook -- --application <uuid> --tamper
 *   npm run webhook -- --application <uuid> --bad-secret
 *   npm run webhook -- --application <uuid> --no-signature
 *   npm run webhook -- --application <uuid> --stale
 *
 * Every variant signs with the SAME function the webhook verifies with, so a
 * passing test cannot be passing against a different algorithm than production.
 */

import path from "node:path";
import crypto from "node:crypto";
import { config as loadEnv } from "dotenv";

loadEnv({ path: path.resolve(process.cwd(), "..", ".env") });

const { signWebhookBody } = await import("../src/lib/didit.ts");

function arg(name: string, fallback = ""): string {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const flag = (name: string) => process.argv.includes(`--${name}`);

const applicationId = arg("application");
if (!applicationId) {
  console.error("need --application <uuid>");
  process.exit(1);
}

const baseUrl = process.env.PUBLIC_BASE_URL ?? "http://localhost:3001";
const status = arg("status", "Approved");
const times = Number(arg("times", "1"));
const stale = flag("stale");

// Built ONCE, outside the loop. That is the whole point: replaying means
// sending the identical message again, so event_id must not change between
// attempts — which is exactly what a vendor redelivery looks like.
const seconds = Math.floor(Date.now() / 1000) - (stale ? 360 : 0);
const envelope = {
  event_id: crypto.randomUUID(),
  webhook_type: "status.updated",
  timestamp: seconds,
  created_at: seconds,
  application_id: "00000000-0000-4000-8000-00000000abcd",
  environment: "sandbox",
  status,
  session_id: crypto.randomUUID(),
  workflow_id: "simulator-workflow",
  vendor_data: applicationId,
  metadata: {},
};

const rawBody = Buffer.from(JSON.stringify(envelope), "utf8");

let signature: string;
if (flag("bad-secret")) {
  signature = signWebhookBody(rawBody, "definitely-not-the-shared-secret");
} else {
  signature = signWebhookBody(rawBody);
}

// Sign the real body, then send a different one. This is the case a signature
// exists to catch: a message that was genuinely issued by the vendor but
// altered in flight.
const bodyToSend = flag("tamper")
  ? Buffer.from(
      JSON.stringify({ ...envelope, status: "Approved", tampered: true }),
      "utf8",
    )
  : rawBody;

const headers: Record<string, string> = {
  "content-type": "application/json",
  "user-agent": "DiditWebhook/2.0 +https://didit.me",
  "x-timestamp": String(seconds),
};
if (!flag("no-signature")) {
  headers["x-signature"] = signature;
}

console.log(`event_id ${envelope.event_id}`);
console.log(`POST ${baseUrl}/api/webhooks/didit  × ${times}`);
if (flag("tamper")) console.log("  body altered after signing");
if (flag("bad-secret")) console.log("  signed with the wrong secret");
if (flag("no-signature")) console.log("  x-signature header omitted");
if (stale) console.log("  timestamp backdated by 6 minutes");
console.log();

for (let attempt = 1; attempt <= times; attempt++) {
  const startedAt = performance.now();
  const response = await fetch(`${baseUrl}/api/webhooks/didit`, {
    method: "POST",
    headers,
    body: bodyToSend,
  });
  const elapsed = performance.now() - startedAt;
  const body = await response.text();
  console.log(
    `  attempt ${attempt}: HTTP ${response.status}  ${elapsed.toFixed(0)}ms  ${body}`,
  );
}
