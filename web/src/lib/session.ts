import crypto from "node:crypto";
import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";

/**
 * Staff sessions: one demo account, a signed cookie, no framework.
 *
 * WHAT THIS IS AND IS NOT
 *
 * The session cookie is SIGNED, not encrypted. Signing stops it being forged —
 * nobody can hand themselves a session for an account they cannot log into.
 * Encryption would add nothing, because the payload contains only an email
 * address and two timestamps, none of which is secret.
 *
 * The password lives in .env in plaintext, and hashing it there would be
 * theatre: the hash and the signing secret would sit in the same file, so an
 * attacker who can read one can read the other. What actually matters at this
 * scale is that the comparison is constant-time, the cookie cannot be forged,
 * and credentials never reach a log.
 *
 * With real users this is not enough, and the gaps are specific rather than
 * vague: per-user rows with scrypt or argon2 hashes, rate limiting that
 * survives a restart, session revocation (a signed cookie is valid until it
 * expires, and there is no way to cancel one), password reset, and a second
 * factor. Saying which pieces are missing is more useful than implying none
 * are.
 */

const COOKIE_NAME = "kyc_desk_session";
const SESSION_HOURS = 12;

export interface StaffSession {
  email: string;
  issuedAt: number;
  expiresAt: number;
}

function secret(): string {
  const value = process.env.SESSION_SECRET;
  if (!value || value.length < 16) {
    throw new Error(
      "SESSION_SECRET is not set (or is too short). Sessions cannot be signed.",
    );
  }
  return value;
}

function sign(payload: string): string {
  return crypto.createHmac("sha256", secret()).update(payload).digest("base64url");
}

/** Compare two strings without leaking how much of them matched. */
export function constantTimeEquals(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  // timingSafeEqual throws on a length mismatch, and the throw would itself
  // leak the length, so lengths are compared first and a mismatch simply loses.
  if (left.length !== right.length) return false;
  return crypto.timingSafeEqual(left, right);
}

function encode(session: StaffSession): string {
  const payload = Buffer.from(JSON.stringify(session), "utf8").toString("base64url");
  return `${payload}.${sign(payload)}`;
}

function decode(value: string): StaffSession | null {
  const [payload, signature] = value.split(".");
  if (!payload || !signature) return null;
  if (!constantTimeEquals(signature, sign(payload))) return null;

  try {
    const session = JSON.parse(
      Buffer.from(payload, "base64url").toString("utf8"),
    ) as StaffSession;
    // Expiry is checked AFTER the signature. Reading an unverified payload to
    // decide anything — even whether to bother verifying — is how signature
    // checks get quietly bypassed.
    if (!session.email || session.expiresAt < Date.now()) return null;
    return session;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Reading and writing the session
// ---------------------------------------------------------------------------

export async function readSession(): Promise<StaffSession | null> {
  const store = await cookies();
  const raw = store.get(COOKIE_NAME)?.value;
  return raw ? decode(raw) : null;
}

export async function startSession(email: string): Promise<void> {
  const now = Date.now();
  const session: StaffSession = {
    email,
    issuedAt: now,
    expiresAt: now + SESSION_HOURS * 60 * 60 * 1000,
  };
  const store = await cookies();
  store.set(COOKIE_NAME, encode(session), {
    // Not readable from JavaScript, so a cross-site scripting bug elsewhere
    // cannot steal the session.
    httpOnly: true,
    // Not sent on cross-site POSTs, which is the basic CSRF defence for the
    // decision actions below.
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: SESSION_HOURS * 60 * 60,
  });
}

export async function endSession(): Promise<void> {
  const store = await cookies();
  store.delete(COOKIE_NAME);
}

/**
 * Require a logged-in officer, or send them to the login page.
 *
 * CALL THIS INSIDE EVERY SERVER ACTION, not only in layouts.
 *
 * A layout guard protects NAVIGATION. It does not protect a Server Action: an
 * action is a POST endpoint that exists independently of any page, and anyone
 * who knows its id can call it without ever rendering the layout that was
 * supposed to be guarding it. The layout check is a convenience for the user;
 * this call is the security boundary.
 */
export async function requireStaff(): Promise<StaffSession> {
  const session = await readSession();
  if (!session) {
    redirect("/login");
  }
  return session;
}

/** The actor string written to audit_events and decisions.decided_by. */
export function actorFor(session: StaffSession): `staff:${string}` {
  return `staff:${session.email}`;
}

// ---------------------------------------------------------------------------
// Login rate limiting
// ---------------------------------------------------------------------------

const ATTEMPT_WINDOW_MS = 5 * 60 * 1000;
const MAX_ATTEMPTS = 8;

/**
 * A fixed window of failed attempts per client.
 *
 * In memory, and therefore honestly limited: it resets on restart and is
 * per-process, so several instances behind a load balancer each get their own
 * allowance. For one demo account that is fine. For real users it belongs in
 * the database or a shared cache — but having none at all would leave a login
 * endpoint anyone can brute-force at line rate, and that is worse than an
 * imperfect one.
 */
const attempts = new Map<string, { count: number; windowStart: number }>();

export async function clientKey(): Promise<string> {
  const store = await headers();
  return (
    store.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    store.get("x-real-ip") ||
    "local"
  );
}

export function rateLimitExceeded(key: string): boolean {
  const entry = attempts.get(key);
  if (!entry) return false;
  if (Date.now() - entry.windowStart > ATTEMPT_WINDOW_MS) {
    attempts.delete(key);
    return false;
  }
  return entry.count >= MAX_ATTEMPTS;
}

export function recordFailure(key: string): void {
  const now = Date.now();
  const entry = attempts.get(key);
  if (!entry || now - entry.windowStart > ATTEMPT_WINDOW_MS) {
    attempts.set(key, { count: 1, windowStart: now });
    return;
  }
  entry.count += 1;
}

export function clearFailures(key: string): void {
  attempts.delete(key);
}
