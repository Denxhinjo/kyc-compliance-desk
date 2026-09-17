"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { startVerification } from "./actions";

function Button() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="button" disabled={pending}>
      {pending ? "Contacting provider…" : "Continue to verification"}
    </button>
  );
}

/**
 * On success the action redirects, so it never returns and this state is only
 * ever used to show a failure. Reaching the vendor is exactly the step most
 * likely to fail, and "nothing happened when I clicked" is the worst possible
 * way to report that.
 */
export function StartVerification({ applicationId }: { applicationId: string }) {
  const [state, action] = useActionState<{ error?: string }, FormData>(
    startVerification,
    {},
  );

  return (
    <form action={action}>
      <input type="hidden" name="applicationId" value={applicationId} />
      {state.error && (
        <p className="field-error" role="alert">
          Could not start verification: {state.error}
        </p>
      )}
      <Button />
    </form>
  );
}
