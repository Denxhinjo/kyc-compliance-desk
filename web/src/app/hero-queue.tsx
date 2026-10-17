import Link from "next/link";
import { pool } from "@/lib/db";

/**
 * The hero visual: a live slice of the real review queue.
 *
 * WHY NOT A SCREENSHOT, AND WHY NOT THE REFERENCE'S MOCK-UP
 *
 * The reference's hero shows a dashboard with invented applicants, invented
 * case ids and invented counts — "Alex Morgan, KYC-1042, High". That is normal
 * for a marketing page and impossible here: this project's entire argument is
 * that every figure on it is computed from the database, and a fabricated
 * queue on the front door would undercut the thing it is advertising on the
 * first screen a visitor sees.
 *
 * A screenshot would be honest but goes stale, and a stale screenshot of a
 * live demo is a small lie that grows. So this is the real queue, read on the
 * request, showing the same scores and the same reason text the desk shows.
 *
 * IT MUST NOT BREAK THE FRONT DOOR.
 *
 * The landing page is the one page that has to render when the database does
 * not. This project has already watched /stats answer 200 with an empty shell
 * when a credential went stale, and that was on a page nobody arrives at
 * first. So the query is wrapped: if it fails, or returns nothing, the hero
 * renders its frame and says the queue is unavailable rather than taking the
 * page down with it.
 */

interface HeroRow {
  id: string;
  full_name: string;
  address_country: string;
  risk_score: number | null;
  reason: string | null;
  waiting_seconds: number;
}

async function peekQueue(): Promise<HeroRow[] | null> {
  try {
    const { rows } = await pool.query<HeroRow>(
      `select a.id,
              a.full_name,
              a.address_country,
              a.risk_score,
              -- The highest-scoring signal's own words, exactly as the desk
              -- shows them. Not a category: the whole argument is that the
              -- system can say WHY, and "High" says nothing.
              (select s->>'reason'
                 from jsonb_array_elements(coalesce(a.risk_signals->'signals','[]'::jsonb)) s
                where (s->>'points')::int > 0
                order by (s->>'points')::int desc
                limit 1) as reason,
              extract(epoch from (now() - r.decided_at))::int as waiting_seconds
         from applications a
         join decisions r on r.application_id = a.id and r.outcome = 'referred'
        where a.status = 'screening'
          and not exists (select 1 from decisions t
                           where t.application_id = a.id
                             and t.outcome in ('approved','rejected'))
        order by r.decided_at asc
        limit 4`,
    );
    return rows;
  } catch {
    // Deliberately swallowed. A broken database must not take the front door
    // with it; /health is where that failure is reported loudly.
    return null;
  }
}

function waited(seconds: number): string {
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

export async function HeroQueue() {
  const rows = await peekQueue();

  return (
    <div className="hero-visual" aria-label="A live slice of the review queue">
      <div className="hero-visual-bar">
        <span className="hero-visual-dot" aria-hidden="true" />
        <span className="hero-visual-title">Review queue</span>
        <span className="hero-visual-live">live</span>
      </div>

      {rows === null || rows.length === 0 ? (
        <p className="hero-visual-empty">
          {rows === null
            ? "The queue is not reachable right now."
            : "No case is waiting for a reviewer."}
        </p>
      ) : (
        <table className="hero-table">
          <thead>
            <tr>
              <th>Waiting</th>
              <th>Score</th>
              <th>Applicant</th>
              <th>Why it was referred</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td className="mono dim">{waited(row.waiting_seconds)}</td>
                <td>
                  {/* The number, not a band. Keeping the score is the point. */}
                  <span className="pill referred mono">{row.risk_score ?? "—"}</span>
                </td>
                <td className="hero-name">
                  {row.full_name}
                  <span className="mono dim"> · {row.address_country}</span>
                </td>
                <td className="hero-reason" title={row.reason ?? undefined}>
                  {/* The clamp lives on a span, not the cell: -webkit-box on a
                      <td> fights the table layout and leaks a third line. */}
                  <span className="clamp-2">{row.reason ?? "—"}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <p className="hero-visual-foot">
        Real rows from this demo&apos;s database, read when you loaded the page.{" "}
        <Link href="/desk">Open the full queue →</Link>
      </p>
    </div>
  );
}
