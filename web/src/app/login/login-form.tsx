"use client";

import { useRef } from "react";
import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { signIn, type LoginState } from "./actions";

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="button" disabled={pending}>
      {pending ? "Signing in…" : "Sign in"}
    </button>
  );
}

/**
 * One click into the desk.
 *
 * The review desk is the part of this demo worth seeing and it sits behind a
 * login. Printing the password and expecting a visitor to copy it across two
 * fields is a step at which people simply leave — and the password guards
 * invented applicants, so there is nothing on the other side of it to protect.
 *
 * Fills both fields visibly rather than posting behind the scenes, so it is
 * obvious what just happened and the form still works normally afterwards.
 */
function DemoSignIn({
  email,
  password,
  formRef,
}: {
  email: string;
  password: string;
  formRef: React.RefObject<HTMLFormElement | null>;
}) {
  const { pending } = useFormStatus();

  return (
    <button
      type="button"
      className="button demo-signin"
      disabled={pending}
      onClick={() => {
        const form = formRef.current;
        if (!form) return;
        const emailInput = form.elements.namedItem("email") as HTMLInputElement;
        const passwordInput = form.elements.namedItem(
          "password",
        ) as HTMLInputElement;
        emailInput.value = email;
        passwordInput.value = password;
        // requestSubmit, not submit(): it runs validation and fires the submit
        // event, which is what React needs to see to invoke the action.
        form.requestSubmit();
      }}
    >
      Sign in as demo officer →
    </button>
  );
}

export function LoginForm({
  demoEmail,
  demoPassword,
}: {
  demoEmail: string;
  demoPassword: string;
}) {
  const [state, action] = useActionState<LoginState, FormData>(signIn, {});
  const formRef = useRef<HTMLFormElement>(null);

  return (
    <form action={action} ref={formRef}>
      {state.error && (
        <p className="field-error" role="alert">
          {state.error}
        </p>
      )}

      {demoPassword && (
        <>
          <DemoSignIn
            email={demoEmail}
            password={demoPassword}
            formRef={formRef}
          />
          <p className="or-divider">or sign in manually</p>
        </>
      )}

      <div className="field">
        <label htmlFor="email">Email</label>
        <input
          id="email"
          name="email"
          type="email"
          autoComplete="username"
          defaultValue={demoEmail}
          // A tool someone opens at the start of a shift should be ready to
          // type into.
          autoFocus
          required
        />
      </div>
      <div className="field">
        <label htmlFor="password">Password</label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          defaultValue={demoPassword}
          required
        />
      </div>
      <SubmitButton />
    </form>
  );
}
