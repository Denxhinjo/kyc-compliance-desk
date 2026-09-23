/**
 * End-to-end proof against the LIVE deployment.
 *
 * Not "does the endpoint answer 200" — that proves only that a process
 * started. This submits a real application through the real form, completes
 * verification through the vendor simulator, and then waits for a DECISION to
 * appear, which can only happen if the drain function actually claimed the job
 * and the screening ran against Postgres.
 *
 *   node scripts/verify-live.mjs https://kyc-compliance-desk.vercel.app
 */
import { chromium } from "playwright-core";

const BASE = process.argv[2] ?? "https://kyc-compliance-desk.vercel.app";
const EDGE =
  process.env.GIF_BROWSER ??
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

const STAFF_EMAIL = process.env.STAFF_EMAIL ?? "officer@example.com";
const STAFF_PASSWORD = process.env.STAFF_PASSWORD;

const t0 = Date.now();
const since = () => `${((Date.now() - t0) / 1000).toFixed(1)}s`;
const step = (n, text) => console.log(`\n[${since()}] ${n}. ${text}`);
const ok = (text) => console.log(`         ✓ ${text}`);

const browser = await chromium.launch({ executablePath: EDGE, headless: true });
const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
const page = await context.newPage();

let applicationId = null;

try {
  // --- 1. submit an application -------------------------------------------
  step(1, "Submit an application through the live form");
  await page.goto(`${BASE}/apply`, { waitUntil: "networkidle", timeout: 90000 });

  const unique = `Sergey Ivanov`; // deliberately collides with the OFAC list
  await page.fill('input[name="fullName"]', unique);
  await page.fill('input[name="dateOfBirth"]', "1975-03-02");
  await page.fill('input[name="addressLine1"]', "14 Bishopsgate");
  await page.fill('input[name="addressCity"]', "London");
  await page.fill('input[name="addressPostcode"]', "EC2N 4AJ");
  await page.fill('input[name="addressCountry"]', "GB");
  await Promise.all([
    page.waitForURL(/\/(verify|status)\//, { timeout: 90000 }),
    page.click('button[type="submit"]'),
  ]);

  applicationId = page.url().match(/[0-9a-f-]{36}/)?.[0];
  ok(`application ${applicationId} created — ${page.url().replace(BASE, "")}`);

  // --- 2. complete verification -------------------------------------------
  step(2, "Complete identity verification (vendor simulator)");
  // Submitting the form lands on /status, which offers the verification link.
  // Going straight to /verify/<id> works too; the button there is what creates
  // the vendor session and redirects to the simulator.
  await page.goto(`${BASE}/verify/${applicationId}`, { waitUntil: "networkidle" });
  await Promise.all([
    page.waitForURL(/mock-vendor/, { timeout: 90000 }),
    page.click('button[type="submit"]'),
  ]);
  ok("vendor session created, on the simulator's screen");

  const approve = page.locator("button.outcome").first();
  await approve.click();
  await page.waitForLoadState("networkidle");
  ok("outcome submitted — the vendor will now call our webhook");

  // --- 3. wait for a DECISION ---------------------------------------------
  step(3, "Wait for the pipeline to reach a decision");
  console.log("         (webhook -> job enqueued -> nudge -> drain -> screen -> decide)");

  let decided = false;
  for (let attempt = 1; attempt <= 40; attempt += 1) {
    await page.goto(`${BASE}/status/${applicationId}`, { waitUntil: "networkidle" });
    const body = await page.content();
    if (/Application (approved|declined)|Sent for manual review/.test(body)) {
      decided = true;
      const outcome = body.match(/Application approved|Application declined|Sent for manual review/)[0];
      ok(`DECIDED after ${since()} — "${outcome}"`);
      break;
    }
    await page.waitForTimeout(3000);
  }
  if (!decided) throw new Error("no decision within ~2 minutes");

  // --- 4. the officer's view ----------------------------------------------
  step(4, "Sign in to the review desk");
  if (!STAFF_PASSWORD) throw new Error("STAFF_PASSWORD not set");
  await page.goto(`${BASE}/login`, { waitUntil: "networkidle" });
  await page.fill('input[name="email"]', STAFF_EMAIL);
  await page.fill('input[name="password"]', STAFF_PASSWORD);
  await Promise.all([
    page.waitForURL(/\/desk/, { timeout: 90000 }),
    page.click('button[type="submit"]'),
  ]);
  ok("signed in");

  step(5, "Open the case and read the evidence");
  await page.goto(`${BASE}/desk/${applicationId}`, { waitUntil: "networkidle" });
  const caseBody = await page.content();
  const matches = [...caseBody.matchAll(/(\d{2,3}(?:\.\d+)?)%/g)].map((m) => m[1]);
  const hasOfac = /OFAC/.test(caseBody);
  const score = caseBody.match(/class="score-number">(\d+)</)?.[1];
  ok(`risk score ${score}, OFAC provenance shown: ${hasOfac}`);
  ok(`match strengths on the page: ${matches.slice(0, 5).join(", ") || "none"}`);

  step(6, "Stats page recomputes");
  await page.goto(`${BASE}/stats`, { waitUntil: "networkidle" });
  const stats = await page.content();
  const processed = stats.match(/PROCESSED[\s\S]{0,400}?stat-value[^>]*>([\d,]+)</i)?.[1];
  const awaiting = stats.match(/AWAITING REVIEW[\s\S]{0,400}?stat-value[^>]*>([\d,]+)</i)?.[1];
  ok(`processed ${processed ?? "?"}, awaiting review ${awaiting ?? "?"}`);

  console.log(`\n[${since()}] PASS — application ${applicationId} went in and came out decided.`);
} finally {
  await browser.close();
}
