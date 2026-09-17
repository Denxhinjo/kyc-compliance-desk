import Link from "next/link";
import { pool } from "@/lib/db";
import { requireStaff } from "@/lib/session";
import { QueueTable, type QueueRow } from "./queue-table";

export const dynamic = "force-dynamic";

export const metadata = { title: "Queue — Review desk" };

interface Row {
  id: string;
  full_name: string;
  address_country: string;
  risk_score: number | null;
  risk_signals: { signals?: { code: string; points: number; reason: string }[] } | null;
  referred_at: string;
  waiting_seconds: number;
}

/**
 * The pending queue: cases referred by Phase 5 and not yet decided by a human.
 *
 * "Not yet decided" is `not exists a terminal decision` rather than a status
 * check, because a referral leaves the application in 'screening' — a referral
 * is a decision about process, not about the applicant, so the case genuinely
 * is not decided until someone decides it.
 */
async function pendingCases(): Promise<Row[]> {
  const { rows } = await pool.query<Row>(
    `select a.id,
            a.full_name,
            a.address_country,
            a.risk_score,
            a.risk_signals,
            r.decided_at::text as referred_at,
            extract(epoch from (now() - r.decided_at))::int as waiting_seconds
       from applications a
       join decisions r
         on r.application_id = a.id
        and r.outcome = 'referred'
      where a.status = 'screening'
        and not exists (
              select 1 from decisions t
               where t.application_id = a.id
                 and t.outcome in ('approved', 'rejected')
            )
      -- Oldest first, always. The longest-waiting case is the one with the
      -- most regulatory exposure, and a queue that hides it behind a sort
      -- control will eventually hide it for a fortnight.
      order by r.decided_at asc
      limit 200`,
  );
  return rows;
}

async function recentlyDecided(): Promise<
  { id: string; full_name: string; outcome: string; decided_by: string; decided_at: string }[]
> {
  const { rows } = await pool.query(
    `select a.id, a.full_name, d.outcome, d.decided_by, d.decided_at::text
       from decisions d
       join applications a on a.id = d.application_id
      where d.outcome in ('approved', 'rejected')
      order by d.decided_at desc
      limit 15`,
  );
  return rows;
}

/** The single most important thing about a case, in the queue's one line. */
function topReason(row: Row): string {
  const signals = row.risk_signals?.signals ?? [];
  const scoring = signals.filter((s) => s.points > 0);
  if (scoring.length === 0) return "—";
  const worst = scoring.reduce((a, b) => (b.points > a.points ? b : a));
  return worst.reason;
}

export default async function QueuePage() {
  await requireStaff();
  const [pending, decided] = await Promise.all([pendingCases(), recentlyDecided()]);

  const rows: QueueRow[] = pending.map((row) => ({
    id: row.id,
    name: row.full_name,
    country: row.address_country,
    score: row.risk_score ?? 0,
    reason: topReason(row),
    waitingSeconds: row.waiting_seconds,
  }));

  const oldest = rows[0]?.waitingSeconds ?? 0;

  return (
    <main className="desk-main">
      <div className="desk-head">
        <h1>Pending review</h1>
        <div className="desk-stats">
          <span>
            <strong>{rows.length}</strong> waiting
          </span>
          {rows.length > 0 && (
            <span>
              oldest <strong>{formatWait(oldest)}</strong>
            </span>
          )}
        </div>
      </div>

      {rows.length === 0 ? (
        <p className="empty">
          Nothing waiting. Cases arrive here when automatic scoring refers
          them — a score between 20 and 79, or a sanctions match that needs a
          human to confirm identity.
        </p>
      ) : (
        <QueueTable rows={rows} />
      )}

      {decided.length > 0 && (
        <>
          <h2>Recently decided</h2>
          <table className="grid muted-grid">
            <thead>
              <tr>
                <th>Applicant</th>
                <th>Outcome</th>
                <th>By</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {decided.map((row) => (
                <tr key={row.id}>
                  <td>
                    <Link href={`/desk/${row.id}`}>{row.full_name}</Link>
                  </td>
                  <td>
                    <span className={`pill ${row.outcome}`}>{row.outcome}</span>
                  </td>
                  <td className="mono dim">{row.decided_by}</td>
                  <td className="mono dim">{row.decided_at.slice(0, 16)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </main>
  );
}

function formatWait(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}
