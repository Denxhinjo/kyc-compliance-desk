import { notFound } from "next/navigation";
import { decodeToken } from "./envelope";
import { OutcomePicker } from "./outcome-picker";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "Verification — simulator",
};

/**
 * SIMULATOR — stands in for Didit's hosted verification screen.
 *
 * The real thing photographs a document and runs liveness checks. There is no
 * value in imitating any of that: what the rest of the system cares about is
 * which status comes back, so this asks directly.
 */
export default async function MockVerifyPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  if ((process.env.DIDIT_MODE ?? "simulator") === "live") notFound();

  const { token } = await params;
  const session = decodeToken(token);
  if (!session) notFound();

  return (
    <main>
      <div className="vendor-frame">
        <p className="vendor-brand">Verification provider · simulator</p>
        <h1>Confirm your identity</h1>
        <p className="sub">
          The real provider would photograph an identity document and check it
          against your face. This simulator skips to the outcome, because the
          outcome is the only part the rest of the system reacts to.
        </p>

        <dl className="summary">
          <dt>Session</dt>
          <dd>
            <code>{session.sessionId}</code>
          </dd>
        </dl>

        <OutcomePicker token={token} />

        <p className="hint">
          Choosing an outcome sends a signed <code>status.updated</code> webhook
          to this application, exactly as Didit would.
        </p>
      </div>
    </main>
  );
}
