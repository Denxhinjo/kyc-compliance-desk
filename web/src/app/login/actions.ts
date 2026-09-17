"use server";

import { redirect } from "next/navigation";
import {
  clearFailures,
  clientKey,
  constantTimeEquals,
  rateLimitExceeded,
  recordFailure,
  startSession,
} from "@/lib/session";

export interface LoginState {
  error?: string;
}

export async function signIn(
  _previous: LoginState,
  form: FormData,
): Promise<LoginState> {
  const key = await clientKey();
  if (rateLimitExceeded(key)) {
    return { error: "Too many attempts. Wait a few minutes and try again." };
  }

  const email = String(form.get("email") ?? "").trim().toLowerCase();
  const password = String(form.get("password") ?? "");

  const expectedEmail = (process.env.STAFF_EMAIL ?? "").trim().toLowerCase();
  const expectedPassword = process.env.STAFF_PASSWORD ?? "";

  if (!expectedEmail || !expectedPassword) {
    return {
      error: "No staff account is configured. Set STAFF_EMAIL and STAFF_PASSWORD.",
    };
  }

  // BOTH comparisons run, and both are constant-time.
  //
  // The obvious version returns early when the email is wrong, which tells an
  // attacker whether an address exists — and short-circuiting on `&&` would
  // skip the password comparison entirely, so a wrong email would answer
  // measurably faster than a wrong password.
  const emailOk = constantTimeEquals(email, expectedEmail);
  const passwordOk = constantTimeEquals(password, expectedPassword);

  if (!emailOk || !passwordOk) {
    recordFailure(key);
    // One message for both failures. "No such user" versus "wrong password"
    // is free reconnaissance.
    return { error: "Those details do not match an account." };
  }

  clearFailures(key);
  await startSession(expectedEmail);

  // Outside any try/catch: redirect() works by throwing a control-flow signal
  // that Next.js catches, so swallowing it would silently do nothing.
  redirect("/desk");
}
