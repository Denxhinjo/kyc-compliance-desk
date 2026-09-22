import Link from "next/link";
import { checkDatabase } from "@/lib/db";

export const dynamic = "force-dynamic";

export const metadata = { title: "Health — KYC Review Desk" };

/**
 * The database connectivity check, which used to sit on the home page.
 *
 * It was the first thing built in Phase 0 and it stayed on the front door for
 * eight phases, so the first thing a visitor read was a Postgres build string.
 * That is a fact about the deployment, not about the product, and it means
 * nothing to anyone who has not just run `docker compose up`.
 *
 * It is still genuinely useful — when the demo is broken, this is the page
 * that says why — so it moves rather than being deleted. Kept out of the
 * navigation on purpose: the people who need it are the people who know the
 * URL, which is the ordinary arrangement for a health endpoint.
 */
export default async function Health() {
  const health = await checkDatabase();

  return (
    <main>
      <p className="crumb">
        <Link href="/">← Home</Link>
      </p>
      <h1>Health</h1>
      <p className="sub">
        Live connectivity check, run on this request. Not cached.
      </p>

      {health.ok ? (
        <div className="card ok">
          <p className="status ok">Database connected</p>
          <dl>
            <dt>Database</dt>
            <dd className="mono">{health.database}</dd>
            <dt>Server time</dt>
            <dd className="mono">{health.serverTime}</dd>
            <dt>Version</dt>
            <dd className="mono break">{health.version}</dd>
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
