import type { PoolClient } from "pg";
import { withTransaction } from "@/lib/db";
import { logEvent } from "@/lib/audit";
import type { ApplicantInput } from "@/lib/validation";

/**
 * Creating an application, in one place.
 *
 * WHY THIS WAS EXTRACTED
 *
 * The apply form's server action used to hold this inline. The continuous
 * seeder (worker/trickle.py, via /api/seed/tick) must create applications
 * through "the same application-creation code the web form uses" — not through
 * a second implementation that drifts.
 *
 * A reimplementation would have been easy and wrong. The previous bulk seeder
 * wrote its own INSERT and, among other things, invented a
 * `vendor_applicant_id` without creating a matching simulator session. That is
 * the direct cause of 38 applications sitting unresolvable in production for
 * three weeks: the sweeper asked the simulator about sessions that had never
 * existed. One function with two callers is how that class of divergence stops
 * being possible.
 *
 * So: one function, two callers, one `seeded` flag between them.
 */

export interface CreateOptions {
  /**
   * Marks every audit row this produces with `"seeded": true`.
   *
   * The same convention the bulk seeder uses, and it is load-bearing rather
   * than cosmetic: the synthetic reviewer refuses to decide anything that is
   * not seeded, and that refusal is only enforceable because every seeded
   * application says so in its own immutable history. It is also how a visitor
   * can tell demo data from anything they created themselves.
   */
  seeded?: boolean;
}

/**
 * Insert the application and record its creation. Returns the new id.
 *
 * Takes a client rather than opening its own transaction, so a caller can do
 * more work in the same atomic unit — which /api/seed/tick needs, because an
 * application that exists without the verification session that follows it is
 * exactly the half-finished state this project keeps proving it does not
 * produce.
 */
export async function insertApplication(
  client: PoolClient,
  applicant: ApplicantInput,
  options: CreateOptions = {},
): Promise<string> {
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
    actor: options.seeded ? "system:seed" : "system:web",
    action: "application.created",
    details: {
      channel: "web_form",
      country: applicant.addressCountry,
      ...(options.seeded ? { seeded: true } : {}),
    },
  });

  return id;
}

/** The whole creation, in its own transaction. What the apply form calls. */
export async function createApplicationRecord(
  applicant: ApplicantInput,
  options: CreateOptions = {},
): Promise<string> {
  return withTransaction((client) => insertApplication(client, applicant, options));
}
