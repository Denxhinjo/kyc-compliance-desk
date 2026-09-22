/**
 * Drive the review desk, record it, and take the case-study stills.
 *
 * Not run directly — scripts/record-desk.ps1 sets up the throwaway database
 * and server this expects, and converts the video afterwards.
 *
 * ISOLATION. Everything here happens against a separate database on a separate
 * port. It has to: the flow ends by DECIDING a case, and a decision writes to
 * `audit_events`, which refuses DELETE. There is no "roll it back afterwards"
 * available — a decision made on the demo database is permanent, would show in
 * that case's timeline for ever, and would quietly consume one of the pending
 * cases the desk exists to show.
 *
 * THE CURSOR. Playwright renders no pointer, so a recorded click looks like
 * the page changing by itself. A small div, moved with a CSS transition to
 * each target before clicking, fixes that in about twenty lines. It is
 * injected per-navigation rather than once, because every page load replaces
 * the document.
 *
 * PACING. The pauses are the point. This is meant to be read, not admired, so
 * the evidence screen gets five seconds and nothing is rushed to save a frame.
 */
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

const BASE = process.env.GIF_BASE_URL ?? "http://localhost:3002";
const CASE_ID = process.env.GIF_CASE_ID;
const OUT = process.env.GIF_OUT ?? join(import.meta.dirname, "..", "docs", "media");
const EDGE =
  process.env.GIF_BROWSER ??
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

if (!CASE_ID) throw new Error("GIF_CASE_ID is not set");

const VIDEO = { width: 1200, height: 750 };
mkdirSync(OUT, { recursive: true });
const videoDir = join(OUT, "_video");
mkdirSync(videoDir, { recursive: true });

/** The cursor, as CSS. Injected into every document. */
const CURSOR_CSS = `
  #__demo_cursor {
    position: fixed; left: 0; top: 0; width: 22px; height: 22px;
    margin: -11px 0 0 -11px; border-radius: 50%;
    background: rgba(91,157,217,.28);
    border: 2px solid #5b9dd9;
    box-shadow: 0 0 0 4px rgba(91,157,217,.12);
    pointer-events: none; z-index: 2147483647;
    transition: left .55s cubic-bezier(.4,0,.2,1),
                top .55s cubic-bezier(.4,0,.2,1),
                transform .12s ease-out;
  }
  #__demo_cursor.__click { transform: scale(.6); }
`;

async function installCursor(page) {
  await page.evaluate((css) => {
    if (document.getElementById("__demo_cursor")) return;
    const style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);
    const dot = document.createElement("div");
    dot.id = "__demo_cursor";
    // Start off-centre rather than at 0,0, so the first move is a glide
    // rather than a leap in from the corner.
    dot.style.left = "50%";
    dot.style.top = "78%";
    document.body.appendChild(dot);
  }, CURSOR_CSS);
}

/** Glide the pointer to an element, then click it. */
async function pointAndClick(page, locator, { settle = 650 } = {}) {
  const box = await locator.boundingBox();
  if (!box) throw new Error("target has no bounding box");
  const x = Math.round(box.x + box.width / 2);
  const y = Math.round(box.y + box.height / 2);

  await page.evaluate(
    ([px, py]) => {
      const dot = document.getElementById("__demo_cursor");
      if (dot) {
        dot.style.left = `${px}px`;
        dot.style.top = `${py}px`;
      }
    },
    [x, y],
  );
  await page.waitForTimeout(settle);

  await page.evaluate(() => {
    document.getElementById("__demo_cursor")?.classList.add("__click");
  });
  await page.waitForTimeout(120);
  await locator.click({ position: { x: box.width / 2, y: box.height / 2 } });
  await page.evaluate(() => {
    document.getElementById("__demo_cursor")?.classList.remove("__click");
  });
}

const browser = await chromium.launch({ executablePath: EDGE, headless: true });

// ---------------------------------------------------------------------------
// Pass 1: the recording
// ---------------------------------------------------------------------------

// Sign in FIRST, in a context that is not being recorded, and carry the
// cookie across. Playwright records a whole context, so logging in inside the
// recorded one put six seconds of typing a password at the front of the clip —
// six seconds nobody needs to watch, and a third of the GIF's file size.
const auth = await browser.newContext({ viewport: VIDEO });
const authPage = await auth.newPage();
await authPage.goto(`${BASE}/login`, { waitUntil: "networkidle" });
await authPage.fill('input[name="email"]', process.env.STAFF_EMAIL ?? "officer@example.com");
await authPage.fill('input[name="password"]', process.env.STAFF_PASSWORD ?? "demo-desk-password");
await Promise.all([authPage.waitForURL(/\/desk/), authPage.click('button[type="submit"]')]);
const storageState = await auth.storageState();
await auth.close();

const rec = await browser.newContext({
  viewport: VIDEO,
  deviceScaleFactor: 1, // a GIF gains nothing from retina and doubles in size
  colorScheme: "dark",
  storageState,
  recordVideo: { dir: videoDir, size: VIDEO },
});

const page = await rec.newPage();
page.on("load", () => installCursor(page).catch(() => {}));

await page.goto(`${BASE}/desk`, { waitUntil: "networkidle" });
await installCursor(page);
await page.waitForTimeout(600);

// --- 1. the queue, long enough to actually read a few rows -----------------
await page.waitForTimeout(3000);

// --- 2. open the flagged case ----------------------------------------------
const row = page.locator(`tr:has(td)`).filter({ hasNot: page.locator("th") }).first();
const target = page.locator("table.queue tbody tr").filter({
  has: page.locator("td"),
});
// The row for our chosen case, found by the name rendered in it.
const caseName = process.env.GIF_CASE_NAME;
const chosen = caseName
  ? page.locator("table.queue tbody tr", { hasText: caseName }).first()
  : target.first();

await pointAndClick(page, chosen);
await page.waitForURL(/\/desk\/[0-9a-f-]{36}/, { timeout: 15000 });
await page.waitForLoadState("networkidle");
await installCursor(page);

// --- 3. the evidence. The whole reason this screen exists. -----------------
await page.waitForTimeout(4600);

// --- 4. write a reason, in view --------------------------------------------
const reason = page.locator("textarea").first();
await pointAndClick(page, reason, { settle: 500 });
await reason.type(
  "Listed DOB is 1961, applicant is 1994. Different person. Document check passed.",
  { delay: 16 },
);
await page.waitForTimeout(900);

// --- 5. decide --------------------------------------------------------------
const approve = page.locator("button.btn.approve").first();
await pointAndClick(page, approve, { settle: 550 });
await page.waitForTimeout(1500);

// --- 6. back to the queue, with the case gone -------------------------------
await page.goto(`${BASE}/desk`, { waitUntil: "networkidle" });
await installCursor(page);
await page.waitForTimeout(2800);

await rec.close(); // flushes the video file
console.log("recorded");

// ---------------------------------------------------------------------------
// Pass 2: the stills, at 2x for print and the README
// ---------------------------------------------------------------------------

const shots = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
  colorScheme: "dark",
});
const still = await shots.newPage();

await still.goto(`${BASE}/login`, { waitUntil: "networkidle" });
await still.fill('input[name="email"]', process.env.STAFF_EMAIL ?? "officer@example.com");
await still.fill('input[name="password"]', process.env.STAFF_PASSWORD ?? "demo-desk-password");
await Promise.all([still.waitForURL(/\/desk/), still.click('button[type="submit"]')]);

const STILLS = [
  { name: "desk-queue", path: "/desk", full: true },
  { name: "case-view", path: `/desk/${CASE_ID}`, full: true },
  { name: "stats", path: "/stats", full: true },
  { name: "home", path: "/", full: false },
  { name: "apply", path: "/apply", full: false },
];

for (const shot of STILLS) {
  await still.goto(`${BASE}${shot.path}`, { waitUntil: "networkidle" });
  await still.waitForTimeout(500);
  await still.screenshot({
    path: join(OUT, `${shot.name}.png`),
    fullPage: Boolean(shot.full),
  });
  console.log(`  still: ${shot.name}.png`);
}

await browser.close();
console.log("done");
