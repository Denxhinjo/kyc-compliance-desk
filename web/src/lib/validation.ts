/**
 * Validation for the applicant form.
 *
 * Hand-rolled rather than pulling in zod: there are seven fields, and the rules
 * matter enough to be worth reading. The age rule in particular is a KYC rule,
 * not a form rule — you cannot onboard a minor — and burying it in a schema
 * library would make it look like a formatting constraint.
 *
 * This runs on the SERVER. Browser validation is a convenience for the user;
 * anything that matters is checked here, because a form can be bypassed
 * entirely by posting to the endpoint directly.
 */

export interface ApplicantInput {
  fullName: string;
  dateOfBirth: string;
  addressLine1: string;
  addressLine2: string | null;
  addressCity: string;
  addressPostcode: string;
  addressCountry: string;
}

export type ValidationResult =
  | { ok: true; value: ApplicantInput }
  | { ok: false; errors: Record<string, string> };

const MIN_AGE = 18;
const MAX_AGE = 120;

function text(form: FormData, field: string): string {
  const raw = form.get(field);
  return typeof raw === "string" ? raw.trim() : "";
}

/** Whole years between a date of birth and today. */
export function ageOn(dateOfBirth: Date, today: Date): number {
  let age = today.getUTCFullYear() - dateOfBirth.getUTCFullYear();
  const monthDiff = today.getUTCMonth() - dateOfBirth.getUTCMonth();
  if (monthDiff < 0 || (monthDiff === 0 && today.getUTCDate() < dateOfBirth.getUTCDate())) {
    age -= 1;
  }
  return age;
}

export function validateApplicant(
  form: FormData,
  today: Date = new Date(),
): ValidationResult {
  const errors: Record<string, string> = {};

  const fullName = text(form, "fullName");
  if (fullName.length < 2) {
    errors.fullName = "Enter the applicant's full name.";
  } else if (fullName.length > 200) {
    errors.fullName = "That name is too long.";
  }

  const dateOfBirth = text(form, "dateOfBirth");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dateOfBirth)) {
    errors.dateOfBirth = "Enter a date of birth.";
  } else {
    const parsed = new Date(`${dateOfBirth}T00:00:00Z`);
    if (Number.isNaN(parsed.getTime())) {
      errors.dateOfBirth = "That is not a real date.";
    } else {
      const age = ageOn(parsed, today);
      if (age < MIN_AGE) {
        // A KYC rule, not a formatting rule: a regulated business cannot
        // onboard a minor, so this is refused rather than flagged for review.
        errors.dateOfBirth = `Applicants must be at least ${MIN_AGE} years old.`;
      } else if (age > MAX_AGE) {
        errors.dateOfBirth = "Check the year — that date looks wrong.";
      }
    }
  }

  const addressLine1 = text(form, "addressLine1");
  if (addressLine1.length < 2) {
    errors.addressLine1 = "Enter the first line of the address.";
  }

  const addressCity = text(form, "addressCity");
  if (addressCity.length < 2) {
    errors.addressCity = "Enter a city or town.";
  }

  const addressPostcode = text(form, "addressPostcode");
  if (addressPostcode.length < 2) {
    errors.addressPostcode = "Enter a postcode or ZIP code.";
  }

  // ISO 3166-1 alpha-2, matching the database constraint. Uppercased for the
  // user rather than rejected — "gb" is not a mistake worth an error message.
  const addressCountry = text(form, "addressCountry").toUpperCase();
  if (!/^[A-Z]{2}$/.test(addressCountry)) {
    errors.addressCountry = "Use a two-letter country code, such as GB or DE.";
  }

  if (Object.keys(errors).length > 0) {
    return { ok: false, errors };
  }

  const addressLine2 = text(form, "addressLine2");

  return {
    ok: true,
    value: {
      fullName,
      dateOfBirth,
      addressLine1,
      addressLine2: addressLine2 || null,
      addressCity,
      addressPostcode,
      addressCountry,
    },
  };
}
