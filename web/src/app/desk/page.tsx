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
  risk_ruleset_version: string | null;
  risk_signals: { signals?: { code: string; points: number; reason: string }[] } | null;
  referred_at: string;
  waiting_seconds: number;
  /**
   * How many applications exist from this person in total, including this one.
   *
   * Exact match on normalised name and date of birth, via the `person_key`
   * generated column (migration 028). Shown so that several rows with the same
   * name read as "this person applied repeatedly" rather than as "the system
   * processed one application several times" — which is what four identical
   * Sergey Ivanov rows looked like, and is the one thing this project claims
   * cannot happen.
   *
   * Display only. It is not a risk signal and does not reach scoring.
   */
  person_applications: number;
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
            a.risk_ruleset_version,
            a.risk_signals,
            r.decided_at::text as referred_at,
            extract(epoch from (now() - r.decided_at))::int as waiting_seconds,
            (select count(*) from applications p
              where p.person_key = a.person_key)::int as person_applications
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

/**
 * How many cases a person has decided. One of the three summary figures.
 *
 * Counted from `decisions` rather than kept as a running total anywhere:
 * `decided_by <> 'system'` is exactly the definition the stats page uses for
 * "human decided", so the two screens cannot drift apart and report different
 * numbers for the same word.
 */
async function reviewedByAPerson(): Promise<number> {
  const { rows } = await pool.query<{ n: number }>(
    `select count(*)::int as n from decisions
      where outcome in ('approved', 'rejected')
        and decided_by <> 'system'`,
  );
  return rows[0]?.n ?? 0;
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
  const [pending, decided, reviewed] = await Promise.all([
    pendingCases(),
    recentlyDecided(),
    reviewedByAPerson(),
  ]);

  const rows: QueueRow[] = pending.map((row) => ({
    id: row.id,
    name: row.full_name,
    country: row.address_country,
    score: row.risk_score ?? 0,
    ruleset: row.risk_ruleset_version,
    reason: topReason(row),
    waitingSeconds: row.waiting_seconds,
    personApplications: row.person_applications,
  }));

  const oldest = rows[0]?.waitingSeconds ?? 0;

  /*
   * HIGHER RISK, counted from the rows already fetched rather than by a second
   * query, and labelled with its own threshold rather than as "high risk".
   *
   * Every case in this queue scores 20-79 by construction -- that band IS the
   * referral rule -- so "high risk" would be a word invented on top of the
   * ruleset rather than read out of it. 60 is the top third of the referral
   * band, and the label on the tile says so, because a figure whose definition
   * is not on screen is a figure a reviewer has to guess at.
   */
  const HIGHER_RISK_AT = 60;
  const higherRisk = rows.filter((row) => row.score >= HIGHER_RISK_AT).length;

  return (
    <main className="desk-main">
      <div className="desk-head">
        <h1>Review queue</h1>
      </div>

      {/* Three figures, deliberately small. The queue is the screen; these are
          context for it, so they are a single quiet strip rather than a row of
          cards. Each is computed on this request from the same tables the
          table below reads -- none is cached, estimated or hardcoded. */}
      <div className="metric-strip">
        <div className="metric">
          <span className="metric-value">{rows.length}</span>
          <span className="metric-label">Pending review</span>
        </div>
        <div className="metric">
          <span className="metric-value">{higherRisk}</span>
          <span className="metric-label">Scoring {HIGHER_RISK_AT}+</span>
        </div>
        <div className="metric">
          <span className="metric-value">
            {rows.length > 0 ? formatWait(oldest) : "—"}
          </span>
          <span className="metric-label">Oldest waiting</span>
        </div>
        <div className="metric">
          <span className="metric-value">{reviewed.toLocaleString()}</span>
          <span className="metric-label">Reviewed by a person</span>
        </div>
      </div>

      {rows.length === 0 ? (
        /* An empty queue is success, not an error, and it is a state a
           visitor may well land on. It should read as "all clear" and explain
           what would put a case here — not look like something failed. */
        <div className="empty queue-clear">
          <strong>The queue is clear.</strong>
          <p>
            Nothing is waiting for a human right now. A case arrives here when
            automatic scoring refers it — a risk score between 20 and 79, or a
            sanctions name match, which never refuses anyone on its own and
            always needs a person to confirm identity.
          </p>
          <p className="dim small">
            <Link href="/apply">Create an application</Link> and complete
            verification to put one here.
          </p>
        </div>
      ) : (
        <QueueTable rows={rows} />
      )}

      {/* DEMOTED, not removed.
          Fifteen decided cases below fourteen pending ones made the screen
          read as two equal tables, and the one that matters is the queue. It
          is still one click away, still links to every case, and still shows
          who decided what -- which is the part worth keeping, because
          "decided by" is how a visitor sees that the synthetic reviewer and a
          real officer are distinguishable in the record. */}
      {decided.length > 0 && (
        <details className="disclosure decided-disclosure">
          <summary>
            Recently decided
            <span className="disclosure-count">{decided.length}</span>
          </summary>
          <div className="table-scroll">
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
          </div>
        </details>
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
