"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { createApplication, type ApplyState } from "./actions";

/**
 * "use client" marks this as a Client Component — it runs in the browser, which
 * is what lets it hold form state and show validation errors without a full
 * page reload. The page that renders it stays a Server Component.
 */

function SubmitButton() {
  // useFormStatus reports on the nearest enclosing <form>. It only works in a
  // component *inside* that form, which is why this is its own component
  // rather than a line in ApplyForm.
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="button" disabled={pending}>
      {pending ? "Creating…" : "Start verification"}
    </button>
  );
}

function Field({
  label,
  name,
  state,
  type = "text",
  hint,
  autoComplete,
  maxLength,
}: {
  label: string;
  name: string;
  state: ApplyState;
  type?: string;
  hint?: string;
  autoComplete?: string;
  maxLength?: number;
}) {
  const error = state.errors?.[name];
  return (
    <div className="field">
      <label htmlFor={name}>{label}</label>
      <input
        id={name}
        name={name}
        type={type}
        defaultValue={state.values?.[name] ?? ""}
        autoComplete={autoComplete}
        maxLength={maxLength}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${name}-error` : hint ? `${name}-hint` : undefined}
      />
      {hint && !error && (
        <p className="hint" id={`${name}-hint`}>
          {hint}
        </p>
      )}
      {error && (
        <p className="field-error" id={`${name}-error`} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export function ApplyForm() {
  // useActionState wires a Server Action to form state: the action's return
  // value becomes `state`, so validation errors come back without the page
  // reloading and without writing any fetch code.
  const [state, formAction] = useActionState<ApplyState, FormData>(
    createApplication,
    {},
  );

  return (
    <form action={formAction} noValidate>
      <Field
        label="Full name"
        name="fullName"
        state={state}
        autoComplete="name"
        maxLength={200}
      />
      <Field
        label="Date of birth"
        name="dateOfBirth"
        type="date"
        state={state}
        autoComplete="bday"
      />
      <Field
        label="Address line 1"
        name="addressLine1"
        state={state}
        autoComplete="address-line1"
      />
      <Field
        label="Address line 2 (optional)"
        name="addressLine2"
        state={state}
        autoComplete="address-line2"
      />
      <div className="field-row">
        <Field
          label="City"
          name="addressCity"
          state={state}
          autoComplete="address-level2"
        />
        <Field
          label="Postcode"
          name="addressPostcode"
          state={state}
          autoComplete="postal-code"
        />
        <Field
          label="Country"
          name="addressCountry"
          state={state}
          hint="Two letters, e.g. GB"
          maxLength={2}
          autoComplete="country"
        />
      </div>
      <SubmitButton />
    </form>
  );
}
