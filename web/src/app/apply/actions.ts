"use server";

import { redirect } from "next/navigation";
import { withTransaction } from "@/lib/db";
import { logEvent } from "@/lib/audit";
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

  const applicationId = await withTransaction(async (client) => {
    const { rows } = await client.query<{ id: string }>(
      `insert into applications
         (status, full_name, date_of_birth, address_line1, address_line2,
          address_city, address_postcode, address_country)
       values ('started', $1, $2, $3, $4, $5, $6, $7)
       returning id`,
      [
        applicant.fullName,
        applicant.dateOfBirth,
        applicant.addressLine1,
        applicant.addressLine2,
        applicant.addressCity,
        applicant.addressPostcode,
        applicant.addressCountry,
      ],
    );
    const id = rows[0].id;

    // Same transaction as the insert. An application that exists with no record
    // of its creation is exactly the gap the audit log exists to close.
    await logEvent(client, {
      applicationId: id,
      actor: "system:web",
      action: "application.created",
      details: {
        channel: "web_form",
        country: applicant.addressCountry,
      },
    });

    return id;
  });

  // redirect() works by throwing a special error that Next.js catches. It must
  // therefore be called OUTSIDE the transaction callback — inside, the
  // withTransaction catch block would see the throw, roll the work back, and
  // the application would never be created.
  redirect(`/status/${applicationId}`);
}
