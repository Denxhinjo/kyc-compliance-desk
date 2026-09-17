import Link from "next/link";
import { redirect } from "next/navigation";
import { endSession, requireStaff } from "@/lib/session";

export const dynamic = "force-dynamic";

/**
 * The desk shell, and the navigation guard.
 *
 * Guarding here rather than in Next.js middleware, because middleware runs on
 * the Edge runtime where node:crypto does not exist — and the workarounds for
 * that end either in a broken build or in a hand-rolled signature on the wrong
 * primitive. A layout is a Server Component on the Node runtime, it wraps every
 * page beneath it, and there is one of it.
 *
 * This protects NAVIGATION ONLY. Server Actions are separately guarded inside
 * themselves; see the note on requireStaff().
 */
export default async function DeskLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const session = await requireStaff();

  async function signOut() {
    "use server";
    await endSession();
    redirect("/login");
  }

  return (
    <div className="desk">
      <header className="desk-bar">
        <Link href="/desk" className="desk-brand">
          Review desk
        </Link>
        <span className="desk-sep">/</span>
        <span className="desk-demo">demo — synthetic applicants</span>
        <span className="desk-spacer" />
        <span className="desk-user">{session.email}</span>
        <form action={signOut}>
          <button type="submit" className="linkish">
            Sign out
          </button>
        </form>
      </header>
      {children}
    </div>
  );
}
