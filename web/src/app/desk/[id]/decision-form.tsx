"use client";

import { useEffect, useRef } from "react";
import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { decide, type DecideState } from "./actions";

function Buttons() {
  const { pending } = useFormStatus();
  return (
    <div className="decide-buttons">
      <button
        type="submit"
        name="outcome"
        value="approved"
        className="btn approve"
        disabled={pending}
        data-decide="approve"
      >
        Approve <kbd>a</kbd>
      </button>
      <button
        type="submit"
        name="outcome"
        value="rejected"
        className="btn reject"
        disabled={pending}
        data-decide="reject"
      >
        Reject <kbd>r</kbd>
      </button>
    </div>
  );
}

export function DecisionForm({ applicationId }: { applicationId: string }) {
  const [state, action] = useActionState<DecideState, FormData>(decide, {});
  const reasonRef = useRef<HTMLTextAreaElement>(null);

  // a / r put the cursor in the reason box rather than deciding immediately.
  //
  // Deliberate: a single keystroke must never record a decision. The shortcut
  // saves the reach for the mouse; it does not skip the part where the officer
  // says why.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if (event.key === "a" || event.key === "r") {
        event.preventDefault();
        reasonRef.current?.focus();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  if (state.alreadyDecidedBy) {
    return (
      <div className="already" role="alert">
        <strong>This case was already decided</strong>
        <p>
          Decided by <code>{state.alreadyDecidedBy}</code> while this page was
          open. Your decision was not recorded. Reload to see theirs.
        </p>
      </div>
    );
  }

  return (
    <form action={action} className="decide">
      <input type="hidden" name="applicationId" value={applicationId} />

      <label htmlFor="reason">
        Reason <span className="required">required</span>
      </label>
      <p className="hint">
        What did you check, and what did you conclude? An auditor reads this to
        judge whether the decision was reasonable on the evidence in front of
        you.
      </p>
      <textarea
        id="reason"
        name="reason"
        ref={reasonRef}
        rows={5}
        defaultValue={state.reason ?? ""}
        placeholder="e.g. PEP match is a different individual — listed DOB 1962, applicant 1988, different nationality. Document check passed."
        required
      />

      {state.error && (
        <p className="field-error" role="alert">
          {state.error}
        </p>
      )}

      <Buttons />
    </form>
  );
}
