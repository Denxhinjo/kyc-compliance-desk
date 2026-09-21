import Link from "next/link";
import { notFound } from "next/navigation";
import { pool } from "@/lib/db";
import { requireStaff } from "@/lib/session";
import { DecisionForm } from "./decision-form";

export const dynamic = "force-dynamic";

interface Application {
  id: string;
  status: string;
  full_name: string;
  date_of_birth: string;
  address_line1: string;
  address_line2: string | null;
  address_city: string;
  address_postcode: string;
  address_country: string;
  vendor_status: string | null;
  vendor_applicant_id: string | null;
  vendor_result_at: string | null;
  risk_score: number | null;
  risk_ruleset_version: string | null;
  risk_signals: {
    score?: number;
    routing?: string;
    thresholds?: Record<string, number>;
    signals?: { code: string; points: number; reason: string; evidence?: Record<string, unknown> }[];
  } | null;
  created_at: string;
}

interface ScreeningMatch {
  id: string;
  match_type: string;
  list_name: string;
  matched_name: string;
  matched_entity_id: string | null;
  match_score: string;
  payload: {
    primary_name?: string;
    aliases?: string[];
    countries?: string[];
    listed_date_of_birth?: string | null;
    date_of_birth_conflict?: boolean;
  };
}

interface Decision {
  outcome: string;
  decided_by: string;
  reason: string;
  risk_score_at_decision: number | null;
  decided_at: string;
}

/** Which version of the sanctions list produced the matches on this case. */
interface Snapshot {
  source: string;
  published_at: string | null;
  content_hash: string;
  record_count: number;
  downloaded_at: string;
}

interface AuditRow {
  id: string;
  occurred_at: string;
  actor: string;
  action: string;
  details: Record<string, unknown>;
}

export default async function CasePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  await requireStaff();
  const { id } = await params;
  if (!/^[0-9a-f-]{36}$/i.test(id)) notFound();

  const [appResult, matchResult, decisionResult, snapshotResult, auditResult] =
    await Promise.all([
    pool.query<Application>(
      `select id, status, full_name, date_of_birth::text, address_line1,
              address_line2, address_city, address_postcode, address_country,
              vendor_status, vendor_applicant_id, vendor_result_at::text,
              risk_score, risk_ruleset_version, risk_signals, created_at::text
         from applications where id = $1`,
      [id],
    ),
    pool.query<ScreeningMatch>(
      `select id::text, match_type, list_name, matched_name, matched_entity_id,
              match_score::text, payload
         from screening_results
        where application_id = $1
        order by match_score desc`,
      [id],
    ),
    pool.query<Decision>(
      `select outcome, decided_by, reason, risk_score_at_decision, decided_at::text
         from decisions where application_id = $1 order by id`,
      [id],
    ),
    pool.query<Snapshot>(
      // The list version behind this case's matches. Joined through
      // screening_results rather than read from applications, because it is a
      // property of the screening run, not of the applicant.
      `select distinct s.source, s.published_at::text, s.content_hash,
              s.record_count, s.downloaded_at::text
         from screening_results r
         join sanctions_snapshots s on s.id = r.snapshot_id
        where r.application_id = $1`,
      [id],
    ),
    pool.query<AuditRow>(
      // Ordered by id, not occurred_at: several events written in one
      // transaction share a timestamp, because now() is the transaction's
      // start time.
      `select id::text, occurred_at::text, actor, action, details
         from audit_events where application_id = $1 order by id`,
      [id],
    ),
    ]);

  const application = appResult.rows[0];
  const snapshot = snapshotResult.rows[0];
  if (!application) notFound();

  const verdict = decisionResult.rows.find(
    (d) => d.outcome === "approved" || d.outcome === "rejected",
  );
  const referral = decisionResult.rows.find((d) => d.outcome === "referred");
  const signals = application.risk_signals?.signals ?? [];
  const score = application.risk_score ?? 0;

  return (
    <main className="case">
      {/* The header carries the three things that decide how much attention
          this case deserves, and it never scrolls away. */}
      <div className="case-head">
        <div className="case-head-left">
          <p className="crumb">
            <Link href="/desk">← Queue</Link>
          </p>
          <h1>{application.full_name}</h1>
          <p className="case-meta mono dim">
            {application.id} · opened {application.created_at.slice(0, 16)}
          </p>
        </div>
        <div className="case-head-right">
          <div className={`score-block ${scoreClass(score)}`}>
            <span className="score-number">{score}</span>
            <span className="score-label">risk score</span>
          </div>
          {verdict ? (
            <span className={`pill big ${verdict.outcome}`}>{verdict.outcome}</span>
          ) : (
            <span className="pill big referred">awaiting review</span>
          )}
        </div>
      </div>

      {/* Reasons immediately under the score. This is what the officer reads
          first and what they are ultimately judging. */}
      <section className="flags">
        <h2>Why this was flagged</h2>
        {signals.length === 0 ? (
          <p className="dim">No risk signals were recorded.</p>
        ) : (
          <ul className="flag-list">
            {signals.map((signal, index) => (
              <li key={index} className={signal.points > 0 ? "" : "context"}>
                <span className="flag-points">
                  {signal.points > 0 ? `+${signal.points}` : "·"}
                </span>
                <span className="flag-reason">{signal.reason}</span>
              </li>
            ))}
          </ul>
        )}
        {application.risk_signals?.thresholds && (
          <p className="dim small">
            Ruleset {application.risk_ruleset_version} · auto-approve below{" "}
            {application.risk_signals.thresholds.auto_approve_below} · auto-reject
            at {application.risk_signals.thresholds.auto_reject_at_or_above}
            {referral && ` · referred ${referral.decided_at.slice(0, 16)}`}
          </p>
        )}
      </section>

      {/* Everything side by side. The officer's job is COMPARISON — is this
          the person on the list, or someone who shares their name? — and the
          date of birth they are comparing must be visible at the same moment
          as the one they are comparing it against. Tabs would make them hold a
          date in their head, and at case ninety of a shift that is where
          mistakes come from. */}
      <div className="case-grid">
        <section className="panel-block">
          <h2>Applicant</h2>
          <dl className="facts">
            <dt>Name</dt>
            <dd>{application.full_name}</dd>
            <dt>Date of birth</dt>
            <dd className="mono">{application.date_of_birth}</dd>
            <dt>Address</dt>
            <dd>
              {application.address_line1}
              {application.address_line2 && <>, {application.address_line2}</>}
              <br />
              {application.address_city} {application.address_postcode}
              <br />
              {application.address_country}
            </dd>
          </dl>
        </section>

        <section className="panel-block">
          <h2>Document check</h2>
          <dl className="facts">
            <dt>Vendor result</dt>
            <dd>
              <span className={`pill ${vendorClass(application.vendor_status)}`}>
                {application.vendor_status ?? "none"}
              </span>
            </dd>
            <dt>Received</dt>
            <dd className="mono dim">
              {application.vendor_result_at?.slice(0, 16) ?? "—"}
            </dd>
            <dt>Session</dt>
            <dd className="mono dim break">
              {application.vendor_applicant_id ?? "—"}
            </dd>
          </dl>
          <p className="dim small">
            We keep the vendor&apos;s reference, never the document images.
          </p>
        </section>

        <section className="panel-block wide">
          <h2>
            Screening matches{" "}
            <span className="dim">({matchResult.rows.length})</span>
          </h2>
          {matchResult.rows.length === 0 ? (
            <p className="dim">No list entry came close enough to record.</p>
          ) : (
            <div className="table-scroll">
            <table className="grid matches">
              <thead>
                <tr>
                  <th className="col-score">Strength</th>
                  <th>Type</th>
                  <th>List</th>
                  <th>Matched name</th>
                  <th>Listed DOB</th>
                  <th>Countries</th>
                </tr>
              </thead>
              <tbody>
                {matchResult.rows.map((match) => {
                  const strength = Number(match.match_score);
                  return (
                    <tr key={match.id}>
                      <td>
                        <span className={`score ${strengthClass(strength)}`}>
                          {strength.toFixed(0)}%
                        </span>
                      </td>
                      <td>
                        <span className={`pill ${match.match_type}`}>
                          {match.match_type}
                        </span>
                      </td>
                      <td className="mono dim">{match.list_name}</td>
                      <td>
                        {match.matched_name}
                        {match.payload?.primary_name &&
                          match.payload.primary_name !== match.matched_name && (
                            <span className="dim small">
                              {" "}
                              (alias of {match.payload.primary_name})
                            </span>
                          )}
                      </td>
                      <td
                        className={`mono ${
                          match.payload?.date_of_birth_conflict ? "conflict" : "dim"
                        }`}
                      >
                        {match.payload?.listed_date_of_birth ?? "—"}
                        {match.payload?.date_of_birth_conflict && " ≠"}
                      </td>
                      <td className="mono dim">
                        {(match.payload?.countries ?? []).join(", ") || "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </div>
          )}
          {/* Which list, and which version of it. Without this the matches
              above are unprovable months later: an auditor asking "was this
              person screened against the list as it stood that day?" needs a
              publication date, not just the word "OFAC". */}
          {snapshot ? (
            <p className="provenance">
              Screened against <strong>{snapshot.source.toUpperCase()}</strong>
              {snapshot.published_at ? (
                <>
                  {" "}published <strong>{snapshot.published_at}</strong>
                </>
              ) : (
                <> (no publication date — this source does not publish one)</>
              )}{" "}
              · {snapshot.record_count.toLocaleString()} entries · loaded{" "}
              {snapshot.downloaded_at.slice(0, 16)}
              <br />
              <span className="mono dim small">
                sha256 {snapshot.content_hash.slice(0, 24)}…
              </span>
            </p>
          ) : (
            <p className="provenance unknown">
              The list version behind these matches was not recorded. Rows
              written before list versioning was added carry no snapshot, and a
              value has deliberately not been invented for them.
            </p>
          )}
          <p className="dim small">
            A strength is a name similarity, not a verdict. Weak matches are
            shown deliberately: whether the strong one is a coincidence is
            easier to judge when you can see how many others came close.
          </p>
        </section>

        <section className="panel-block decide-block">
          <h2>Decision</h2>
          {verdict ? (
            /* Already decided: no buttons at all.
               This is the convenience layer. The transaction and the unique
               index are what make a second decision impossible; hiding the
               buttons just stops the honest mistake. */
            <div className="decided">
              <span className={`pill big ${verdict.outcome}`}>{verdict.outcome}</span>
              <dl className="facts">
                <dt>By</dt>
                <dd className="mono">{verdict.decided_by}</dd>
                <dt>When</dt>
                <dd className="mono dim">{verdict.decided_at.slice(0, 16)}</dd>
                <dt>Score then</dt>
                <dd className="mono dim">{verdict.risk_score_at_decision ?? "—"}</dd>
              </dl>
              <p className="reason-quote">{verdict.reason}</p>
              <p className="dim small">
                Decided cases cannot be decided again. The record is closed.
              </p>
            </div>
          ) : (
            <DecisionForm applicationId={application.id} />
          )}
        </section>
      </div>

      <section className="timeline-block">
        <h2>Timeline</h2>
        <p className="dim small">
          Every recorded change to this case, in order. Read-only — not by
          convention, but because <code>audit_events</code> rejects UPDATE,
          DELETE and TRUNCATE at the database level. There is no edit control
          to build, because the write would be refused.
        </p>
        <ol className="timeline">
          {auditResult.rows.map((event) => (
            <li key={event.id}>
              <span className="t-time mono">{event.occurred_at.slice(0, 19)}</span>
              <span className="t-action">{event.action}</span>
              <span className="t-actor mono dim">{event.actor}</span>
              {Object.keys(event.details).length > 0 && (
                <code className="t-details">{JSON.stringify(event.details)}</code>
              )}
            </li>
          ))}
        </ol>
      </section>
    </main>
  );
}

/**
 * Colour band for a risk score.
 *
 * These are the RULESET'S OWN thresholds, not an invented scale: under 20
 * auto-approves, 20-79 goes to a human, 80 and over auto-rejects. The case
 * view prints those same two numbers under the flags, so the colour and the
 * stated rule agree.
 *
 * The mid boundary used to be 50, which meant every case in the review queue
 * — all of which score 20-79 by definition — rendered green. A green 45 on a
 * case that is sitting in the queue waiting for a human tells the officer the
 * opposite of the truth, so this is a correctness fix rather than a palette
 * preference.
 */
function scoreClass(score: number): string {
  if (score >= 80) return "score-high";
  if (score >= 20) return "score-mid";
  return "score-low";
}

function strengthClass(strength: number): string {
  if (strength >= 92) return "score-high";
  if (strength >= 85) return "score-mid";
  return "score-low";
}

function vendorClass(status: string | null): string {
  if (status === "Approved") return "approved";
  if (status === "Declined") return "rejected";
  return "referred";
}
