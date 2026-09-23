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

      {/* Above the form, not below it. It used to sit underneath as a small
          amber footnote, which is a good way to publish something nobody
          reads — the credentials were on the page the whole time and still
          read as "no way in". */}
      {demoPassword && (
        <p className="notice">
          <strong>Open demo.</strong> Sign in with the button below, or use{" "}
          <code>{demoEmail}</code> / <code>{demoPassword}</code>. The password
          is printed because there is nothing behind it to protect: every
          applicant in here is invented and no real person&apos;s data has ever
          been in this system.
        </p>
      )}

      <div className="panel">
        <LoginForm demoEmail={demoEmail} demoPassword={demoPassword} />
      </div>
    </main>
  );
}
