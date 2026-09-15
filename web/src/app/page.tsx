import { checkDatabase } from "@/lib/db";

/**
 * Force this page to be rendered on every request.
 *
 * By default Next.js would notice this page has no dynamic inputs and render
 * it once at build time — which would mean the "database connected" message
 * reported the state of the world during `next build`, not now. For a health
 * check that is worse than useless.
 */
export const dynamic = "force-dynamic";

// This is a Server Component: it runs only on the server and its code is
// never sent to the browser. That is what lets it hold a Postgres connection
// and still be plain React — there is no API route in between.
export default async function Home() {
  const health = await checkDatabase();

  return (
    <main>
      <h1>KYC Onboarding &amp; Compliance Review Desk</h1>
      <p className="sub">Phase 0 — foundations. Connectivity check only.</p>

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
          <p className="hint">
            Queried live by a React Server Component on this request.
          </p>
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
