import Link from "next/link";
import { checkDatabase, pool } from "@/lib/db";

export const dynamic = "force-dynamic";

/**
 * Both scheduled jobs are expected at least hourly, so three hours of silence
 * is unambiguous rather than borderline — it means at least two runs were
 * missed, which no amount of GitHub's best-effort scheduling explains.
 *
 * WHY THIS IS ON A PAGE AT ALL. GitHub disables scheduled workflows in public
 * repositories after 60 days without repository activity. So the expected way
 * for this demo to break is not a crash but a silence: the drain stops, the
 * sweeper stops, the trickle stops, every page still renders, and nothing
 * anywhere says so. That is the same defect class this project has recorded
 * four times — something that looks like a check and is not one — and the
 * answer is the same: make the absence visible.
 */
const STALE_AFTER_HOURS = 3;

interface JobPulse {
  action: string;
  label: string;
  what: string;
  lastAt: string | null;
  ageSeconds: number | null;
  details: Record<string, unknown> | null;
}

/**
 * When each recurring job last left evidence that it ran.
 *
 * Read from audit_events rather than from the jobs table on purpose. A job row
 * says the worker claimed something; these rows say the work happened and what
 * it found. The sweeper in particular used to write its row only when it had
 * applied or failed something, so three weeks of correct sweeps left no trace
 * at all and a working sweeper was indistinguishable from a stopped one. It now
 * writes on every run, which is what makes this panel possible.
 */
async function jobPulses(): Promise<JobPulse[]> {
  const { rows } = await pool.query<{
    action: string;
    last_at: string | null;
    age_seconds: number | null;
    details: Record<string, unknown> | null;
  }>(
    `select v.action,
            max(e.occurred_at)::text as last_at,
            extract(epoch from (now() - max(e.occurred_at)))::int as age_seconds,
            (array_agg(e.details order by e.occurred_at desc))[1] as details
       from (values ('sweep.completed'), ('trickle.completed')) as v(action)
       left join audit_events e on e.action = v.action
      group by v.action`,
  );

  const byAction = new Map(rows.map((r) => [r.action, r]));
  return [
    {
      action: "trickle.completed",
      label: "Continuous seeder",
      what: "adds a few synthetic applicants and lets the synthetic reviewer work the older seeded queue",
    },
    {
      action: "sweep.completed",
      label: "Vendor sweeper",
      what: "asks the provider about applications no webhook arrived for, and closes the ones it has no record of",
    },
  ].map((job) => {
    const row = byAction.get(job.action);
    return {
      ...job,
      lastAt: row?.last_at ?? null,
      ageSeconds: row?.age_seconds ?? null,
      details: row?.details ?? null,
    };
  });
}

function formatAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

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
  const [health, pulses] = await Promise.all([checkDatabase(), jobPulses()]);
  const stale = pulses.filter(
    (p) => p.ageSeconds === null || p.ageSeconds > STALE_AFTER_HOURS * 3600,
  );

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

      <h2>Scheduled jobs</h2>
      <p className="sub">
        Both run hourly. Flagged after {STALE_AFTER_HOURS} hours of silence —
        GitHub disables scheduled workflows in public repositories after 60 days
        without repository activity, so a silent stop is the expected failure
        here, not an unlikely one.
      </p>

      {stale.length > 0 && (
        <div className="card bad">
          <p className="status bad">
            {stale.length === 1
              ? "One scheduled job has gone quiet"
              : `${stale.length} scheduled jobs have gone quiet`}
          </p>
          <p className="hint">
            Re-enable the workflow in GitHub → Actions, or run it by hand. The
            data is not damaged: the queue holds whatever was not processed and
            it resumes where it left off.
          </p>
        </div>
      )}

      <div className="table-scroll">
        <table className="grid">
          <thead>
            <tr>
              <th>Job</th>
              <th>Last ran</th>
              <th>Age</th>
              <th>Last result</th>
            </tr>
          </thead>
          <tbody>
            {pulses.map((pulse) => {
              const isStale =
                pulse.ageSeconds === null ||
                pulse.ageSeconds > STALE_AFTER_HOURS * 3600;
              return (
                <tr key={pulse.action}>
                  <td>
                    <strong>{pulse.label}</strong>
                    <br />
                    <span className="dim small">{pulse.what}</span>
                  </td>
                  <td className="mono dim">
                    {pulse.lastAt ? pulse.lastAt.slice(0, 16) : "never"}
                  </td>
                  <td className={`mono ${isStale ? "status bad" : "status ok"}`}>
                    {pulse.ageSeconds === null
                      ? "no record"
                      : formatAge(pulse.ageSeconds)}
                  </td>
                  <td className="mono dim small">
                    {pulse.details
                      ? Object.entries(pulse.details)
                          .filter(([key]) => key !== "seeded" && key !== "reviewer")
                          .map(([key, value]) => `${key}=${String(value)}`)
                          .join(" ")
                      : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </main>
  );
}
