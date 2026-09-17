import Link from "next/link";
import { checkDatabase } from "@/lib/db";

export const dynamic = "force-dynamic";

export default async function Home() {
  const health = await checkDatabase();

  return (
    <main>
      <h1>KYC Onboarding &amp; Compliance Review Desk</h1>
      <p className="sub">
        Identity verification and the back-office desk where a compliance officer
        handles what cannot be decided automatically.
      </p>

      <div className="home-links">
        <Link className="home-link" href="/apply">
          <strong>Open an account →</strong>
          <span>
            The applicant&apos;s journey: details, identity verification, waiting
            for a decision.
          </span>
        </Link>
      </div>

      <h2>System</h2>
      {health.ok ? (
        <div className="card ok">
          <p className="status ok">Database connected</p>
          <dl>
            <dt>Database</dt>
            <dd>{health.database}</dd>
            <dt>Server time</dt>
            <dd>{health.serverTime}</dd>
            <dt>Version</dt>
            <dd>{health.version}</dd>
          </dl>
        </div>
      ) : (
        <div className="card bad">
          <p className="status bad">Database connection failed</p>
          <pre>{health.error}</pre>
          <p className="hint">
            Check that <code>docker compose up -d</code> is running and that{" "}
            <code>DATABASE_URL</code> in the root <code>.env</code> is correct.
          </p>
        </div>
      )}
    </main>
  );
}
