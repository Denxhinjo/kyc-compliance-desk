"use server";

import { redirect } from "next/navigation";
import { createApplicationRecord } from "@/lib/applications";
import { validateApplicant } from "@/lib/validation";

/**
 * A SERVER ACTION: a function the browser can call directly, without an API
 * route in between. The "use server" directive at the top of the file is what
 * makes that safe — it marks these exports as server-only entry points, so
 * Next.js generates the endpoint and the fetch call for you. The function body
 * never reaches the browser, which is why it can hold a database connection.
 */

export interface ApplyState {
  errors?: Record<string, string>;
  /** Values typed so far, so a failed submit does not empty the form. */
  values?: Record<string, string>;
}

export async function createApplication(
  _previous: ApplyState,
  form: FormData,
): Promise<ApplyState> {
  const result = validateApplicant(form);

  if (!result.ok) {
    const values: Record<string, string> = {};
    for (const [key, value] of form.entries()) {
      if (typeof value === "string") values[key] = value;
    }
    return { errors: result.errors, values };
  }

  const applicant = result.value;

  // The insert and its audit row live in lib/applications.ts rather than here,
  // because the continuous seeder has to create applications through exactly
  // this code rather than through a second copy of it. See the note in that
  // file on why a reimplementation was the wrong answer.
  const applicationId = await createApplicationRecord(applicant);

  // redirect() works by throwing a special error that Next.js catches. It must
  // therefore be called OUTSIDE the transaction callback — inside, the
  // withTransaction catch block would see the throw, roll the work back, and
  // the application would never be created.
  redirect(`/status/${applicationId}`);
}
