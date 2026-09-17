import { redirect } from "next/navigation";
import { readSession } from "@/lib/session";
import { LoginForm } from "./login-form";

export const dynamic = "force-dynamic";

export const metadata = { title: "Sign in — Review desk" };

export default async function LoginPage() {
  // Already signed in? Skip the form.
  if (await readSession()) {
    redirect("/desk");
  }

  const demoEmail = process.env.STAFF_EMAIL ?? "";
  const demoPassword = process.env.STAFF_PASSWORD ?? "";

  return (
    <main className="login-main">
      <h1>Compliance review desk</h1>
      <p className="sub">Staff sign-in.</p>

      <div className="panel">
        <LoginForm demoEmail={demoEmail} />
      </div>

      {demoPassword && (
        <p className="notice">
          Demo credentials: <code>{demoEmail}</code> / <code>{demoPassword}</code>
          . Printed here because this is a demonstration with invented
          applicants and no real data behind it.
        </p>
      )}
    </main>
  );
}
