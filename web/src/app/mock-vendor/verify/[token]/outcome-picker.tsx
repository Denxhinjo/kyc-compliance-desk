"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { completeVerification } from "./actions";

/** The Didit session statuses that are worth simulating, spelled exactly. */
const OUTCOMES = [
  { status: "Approved", note: "Document accepted, face matched." },
  { status: "Declined", note: "Document rejected or face did not match." },
  { status: "In Review", note: "Referred to the provider's human reviewers." },
  { status: "Abandoned", note: "Applicant closed the window without finishing." },
];

function Buttons() {
  const { pending } = useFormStatus();
  return (
    <div className="outcomes">
      {OUTCOMES.map((outcome) => (
        <button
          key={outcome.status}
          type="submit"
          name="status"
          value={outcome.status}
          className="outcome"
          disabled={pending}
        >
          <span className="outcome-status">{outcome.status}</span>
          <span className="outcome-note">{outcome.note}</span>
        </button>
      ))}
    </div>
  );
}

export function OutcomePicker({ token }: { token: string }) {
  const [state, action] = useActionState<{ error?: string }, FormData>(
    completeVerification,
    {},
  );

  return (
    <form action={action}>
      <input type="hidden" name="token" value={token} />
      {state.error && (
        <p className="field-error" role="alert">
          {state.error}
        </p>
      )}
      <Buttons />
    </form>
  );
}
