/**
 * The audit trail, in language an applicant can read — and only the parts an
 * applicant is allowed to read.
 *
 * WHY THIS IS A WHITELIST AND NOT A FORMATTER
 *
 * The officer's case view renders `audit_events.details` as raw JSON, which is
 * right: an auditor needs the unvarnished record, and a pretty-printer that
 * dropped a field would defeat the purpose of an append-only log.
 *
 * The applicant's status page is a different audience with a different rule.
 * The same log contains the risk score, the routing decision and the screening
 * reasons — including sentences of the form "Sanctions match against X on OFAC
 * SDN at 86% name similarity". Showing an applicant that they matched a
 * sanctions list is not merely untidy. In the UK it is TIPPING OFF (Proceeds
 * of Crime Act 2002, s.333A), a criminal offence, and most jurisdictions have
 * an equivalent. A firm that leaks it has a bigger problem than a scruffy page.
 *
 * So this maps each event to a sentence written for the applicant, and
 * FAILS CLOSED: an action with no entry here renders nothing at all. A new
 * event type added later is invisible to applicants until somebody decides
 * what they are allowed to be told, which is the safe direction for that
 * decision to default in.
 *
 * DOES THE PAYLOAD STILL LEAK? No — checked rather than assumed.
 *
 * The page's query does still select `details`, and the worry was that the
 * whole payload would ride along inside the RSC stream even though nothing
 * rendered it. It does not: the status page is a Server Component, so only the
 * rendered tree crosses to the client and `details` stays on the server.
 *
 * Measured against an application whose audit log contains `{"score": 105,
 * "reasons": ["Sanctions match against \"Sudhir KUMAR\" on OFAC SDN at 100%
 * name similarity (confirmed)", ...]}`: the response contains no occurrence of
 * "OFAC", "score", "reasons", "confirmed" or "105". The only hit for the
 * matched name is the applicant's own name in the page heading, which is
 * theirs to see.
 *
 * Worth restating because it is easy to get backwards: this holds because the
 * component is a Server Component. Move this list into a Client Component and
 * the raw rows would be serialised into the payload, and the leak would be
 * back without a single line of rendering code having changed.
 */

export interface ApplicantEvent {
  title: string;
  detail?: string;
}

type Details = Record<string, unknown>;

/** The vendor's own vocabulary, in words an applicant would use.
 *
 * Each entry carries its own TITLE as well as its detail. The first version
 * used one fixed title — "Identity check complete" — with a varying detail
 * underneath, which produced "Identity check complete / The identity check was
 * not completed." on any application the applicant abandoned. A heading that
 * contradicts the sentence under it is worse than either alone.
 */
const VENDOR_STATUS_COPY: Record<string, ApplicantEvent> = {
  Approved: {
    title: "Identity check passed",
    detail: "Your document was accepted.",
  },
  Declined: {
    title: "Identity check failed",
    detail: "Your document could not be accepted.",
  },
  "In Review": {
    title: "Identity check under review",
    detail: "Your document is being checked by hand.",
  },
  "In Progress": {
    title: "Identity check in progress",
    detail: "Your document is being checked.",
  },
  "Awaiting User": {
    title: "Waiting for you",
    detail: "We need something else from you before we can continue.",
  },
  Resubmitted: {
    title: "Replacement document received",
    detail: "You sent a new document and we are checking it.",
  },
  Abandoned: {
    title: "Identity check not completed",
    detail: "The check was started but never finished.",
  },
  Expired: {
    title: "Identity check expired",
    detail: "The check expired before it was finished.",
  },
  "Kyc Expired": {
    title: "Identity check expired",
    detail: "The check expired before it was finished.",
  },
};

function text(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function onStatusChange(details: Details): ApplicantEvent | null {
  const to = text(details.to);
  const vendorStatus = text(details.vendor_status);

  switch (to) {
    case "submitted":
      return {
        title: "Details received",
        detail: "We have your application and have opened an identity check.",
      };
    case "checking":
      return {
        title: "Identity check in progress",
        detail: "Our identity provider is reviewing your document.",
      };
    case "screening":
      // Title and detail both come from the vendor's verdict, so they cannot
      // disagree with each other.
      return (
        (vendorStatus ? VENDOR_STATUS_COPY[vendorStatus] : undefined) ?? {
          title: "Identity check reviewed",
          detail: "Your document was reviewed.",
        }
      );
    case "decided":
      return { title: "Decision recorded" };
    default:
      // Fail closed. An unrecognised transition says nothing rather than
      // guessing at wording for a state nobody has written copy for.
      return null;
  }
}

function onDecision(details: Details): ApplicantEvent | null {
  switch (text(details.outcome)) {
    case "approved":
      return {
        title: "Application approved",
        detail: "Your account is ready to use.",
      };
    case "rejected":
      return {
        title: "Application declined",
        detail:
          "We are not able to open an account on this application. Our support team can tell you what to do next.",
      };
    case "referred":
      return {
        title: "Sent for manual review",
        detail:
          "Some applications need a person to look at them. A member of our team will review yours.",
      };
    default:
      return null;
  }
}

/**
 * One audit event as the applicant should see it, or null to show nothing.
 *
 * Deliberately ignores `details` for the screening event. The applicant is
 * told the checks ran; they are not told what the checks found, what it
 * scored, or which list produced a match.
 */
export function describeForApplicant(
  action: string,
  details: Details,
): ApplicantEvent | null {
  switch (action) {
    case "application.created":
      return {
        title: "Application started",
        detail: "You submitted your name, date of birth and address.",
      };

    case "verification.session_created":
      return {
        title: "Identity check opened",
        detail: "We created a secure session with our identity provider.",
      };

    case "status.changed":
      return onStatusChange(details);

    case "screening.completed":
      return {
        title: "Background checks completed",
        detail: "We ran the standard sanctions and public-official checks.",
      };

    case "decision.recorded":
      return onDecision(details);

    // Everything else is internal: refused vendor results, sweeper runs,
    // retention passes. Not secrets exactly, but not this audience's business
    // either, and nothing is gained by showing them.
    default:
      return null;
  }
}
