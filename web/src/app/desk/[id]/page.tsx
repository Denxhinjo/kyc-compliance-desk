import { Fragment } from "react";
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

/**
 * Another application from the same person.
 *
 * "Same person" is an EXACT match on normalised name and date of birth,
 * computed by the `person_key` generated column (migration 028). Display only:
 * it changes no score, merges nothing and decides nothing.
 *
 * It exists because five applications from one person, each correctly handled
 * once, look exactly like one application processed five times to anyone
 * reading the queue — and that is the failure this project claims to prevent.
 * Saying "5 applications from this person" costs one line and removes the
 * ambiguity.
 */
interface SiblingApplication {
  id: string;
  status: string;
  created_at: string;
  risk_score: number | null;
}

interface StuckJob {
  id: string;
  last_error: string | null;
  updated_at: string;
}

interface RetryingJob {
  id: string;
  last_error: string | null;
  run_after: string;
}

export default async function CasePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  await requireStaff();
  const { id } = await params;
  if (!/^[0-9a-f-]{36}$/i.test(id)) notFound();

  const [
    appResult,
    matchResult,
    decisionResult,
    snapshotResult,
    auditResult,
    stuckResult,
    retryingResult,
    siblingResult,
  ] = await Promise.all([
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
    // Screening work that failed for good and was parked.
    //
    // A parked screening.run is the system saying it found something it could
    // not write down — a newer sanctions list, or a corrected vendor result
    // that would move the score. Migration 020 freezes the risk columns once a
    // decision row exists, so on a referred case that job raises and parks
    // rather than overwriting the evidence the referral was made on.
    //
    // Migration 023 refuses an officer's decision while one of these exists.
    // This query is what lets the page say so instead of the officer meeting a
    // database error with no explanation.
    pool.query<StuckJob>(
      `select id::text, last_error, updated_at::text
         from jobs
        where job_type = 'screening.run'
          and status = 'parked'
          and payload->>'application_id' = $1
        order by id`,
      [id],
    ),
    // Screening work that failed and is still retrying.
    //
    // 'queued' with a last_error means an attempt failed and another is
    // scheduled. It may yet succeed, so migration 023 does NOT block a decision
    // on it — blocking on 'queued' would freeze the desk for every transient
    // blip, including first deliveries about to succeed.
    //
    // But for a referred case the retry is likely to be carrying evidence the
    // freeze will refuse, in which case it will exhaust its attempts and park.
    // The officer should know that before signing off, which is why the page
    // warns on the broader condition than the database refuses on.
    pool.query<RetryingJob>(
      `select id::text, last_error, run_after::text
         from jobs
        where job_type = 'screening.run'
          and status = 'queued'
          and last_error is not null
          and payload->>'application_id' = $1
        order by id`,
      [id],
    ),
    // Other applications from the same person. Matched on the generated
    // person_key, so the definition of "same person" lives in the database and
    // both services get the identical answer.
    pool.query<SiblingApplication>(
      `select other.id, other.status, other.created_at::text, other.risk_score
         from applications self
         join applications other on other.person_key = self.person_key
        where self.id = $1
          and other.id <> self.id
        order by other.created_at`,
      [id],
    ),
    ]);

  const application = appResult.rows[0];
  const snapshot = snapshotResult.rows[0];
  const stuckJobs = stuckResult.rows;
  const retryingJobs = retryingResult.rows;
  const siblings = siblingResult.rows;
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
            {siblings.length > 0 && (
              <>
                <dt>Repeat applications</dt>
                <dd>
                  <strong>
                    {siblings.length + 1} applications from this person
                  </strong>
                  <p className="dim small repeat-note">
                    Same name and date of birth. Each was screened and decided
                    separately — this is not one application processed more than
                    once. Nothing here changes the score.
                  </p>
                  <ul className="repeat-list">
                    {siblings.map((other) => (
                      <li key={other.id}>
                        <Link href={`/desk/${other.id}`}>
                          {other.created_at.slice(0, 16)}
                        </Link>{" "}
                        <span className="mono dim">{other.status}</span>
                        {other.risk_score !== null && (
                          <span className="mono dim"> · score {other.risk_score}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </dd>
              </>
            )}
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
          ) : stuckJobs.length > 0 ? (
            /* New evidence arrived and could not be recorded.
               The database refuses this decision (migration 023), so the form
               is replaced rather than merely annotated — offering buttons that
               are guaranteed to fail would be worse than offering none. The
               reason is shown in full, because an officer who cannot decide and
               cannot see why is worse off than one who simply cannot decide. */
            <div className="notice">
              <p>
                <strong>This case cannot be decided yet.</strong> Screening ran
                again for this applicant and could not record what it found, so
                the evidence in front of you may be incomplete.
              </p>
              <p className="dim small">
                That happens when something changed since the referral — a newer
                sanctions list, or a corrected result from the identity vendor.
                The earlier evidence is deliberately not overwritten, which is
                why the new result had nowhere to go.
              </p>
              <dl className="facts">
                {stuckJobs.map((job) => (
                  <Fragment key={job.id}>
                    <dt>Job</dt>
                    <dd className="mono">#{job.id}</dd>
                    <dt>Failed</dt>
                    <dd className="mono dim">{job.updated_at.slice(0, 16)}</dd>
                    <dt>Error</dt>
                    <dd className="mono dim">{job.last_error ?? "—"}</dd>
                  </Fragment>
                ))}
              </dl>
              <p className="dim small">
                Someone needs to look at the parked job before this case is
                decided. Deciding now would be deciding without it.
              </p>
            </div>
          ) : (
            <>
              {retryingJobs.length > 0 && (
                /* A WARNING, NOT A BLOCK.
                   The job may still succeed, so the database permits this
                   decision and so does this page. What the officer gets is the
                   knowledge that evidence may still be arriving — which is the
                   whole difference between a judgement call and an accident. */
                <div className="notice" data-testid="evidence-retrying">
                  <p>
                    <strong>Screening is retrying for this applicant.</strong>{" "}
                    An attempt failed and another is scheduled, so evidence may
                    still be arriving. You can decide now; it is worth knowing
                    that you are deciding before that finishes.
                  </p>
                  <dl className="facts">
                    {retryingJobs.map((job) => (
                      <Fragment key={job.id}>
                        <dt>Job</dt>
                        <dd className="mono">#{job.id}</dd>
                        <dt>Next try</dt>
                        <dd className="mono dim">{job.run_after.slice(0, 16)}</dd>
                        <dt>Last error</dt>
                        <dd className="mono dim">{job.last_error ?? "—"}</dd>
                      </Fragment>
                    ))}
                  </dl>
                </div>
              )}
              <DecisionForm applicationId={application.id} />
            </>
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
          {auditResult.rows.map((event) => {
            const hasPayload = Object.keys(event.details).length > 0;
            return (
              <li key={event.id}>
                <span className="t-time mono">
                  {event.occurred_at.slice(0, 19)}
                </span>
                <span className="t-action">{describeEvent(event)}</span>
                <span className="t-actor mono dim">{event.actor}</span>
                {/* PROGRESSIVE DISCLOSURE.
                    The payload used to print in full on every row, so reading
                    a five-event history meant reading five lines of JSON to
                    find the two words that mattered. It is still one click
                    away and still complete -- an auditor needs the unvarnished
                    record, and a pretty-printer that dropped a field would
                    defeat the purpose of keeping one. */}
                {hasPayload && (
                  <details className="t-raw">
                    <summary>Event data</summary>
                    <pre>{JSON.stringify(event.details, null, 2)}</pre>
                  </details>
                )}
              </li>
            );
          })}
        </ol>
      </section>
    </main>
  );
}

/**
 * One line of plain English for an audit event.
 *
 * The dotted verb -- `vendor.result_applied` -- is the right name in the
 * record and the wrong thing to read fifteen of. It stays visible in the
 * payload behind each row; this is what the row itself says.
 *
 * UNRECOGNISED ACTIONS FALL BACK TO THE RAW NAME rather than to a generic
 * phrase. An event type added later would otherwise render as "Updated" and
 * read as though the system understood it, which is the same failure as the
 * applicant status page showing a sentence it had not been given -- except
 * here it misleads an auditor rather than an applicant.
 */
function describeEvent(event: { action: string; details: Record<string, unknown> }): string {
  const detail = event.details as Record<string, string | number | undefined>;

  switch (event.action) {
    case "application.created":
      return "Application submitted";
    case "verification.session_created":
      return "Identity verification started";
    case "status.changed":
      return detail.to === "screening"
        ? "Provider returned a result — screening began"
        : `Moved to ${String(detail.to ?? "a new state")}`;
    case "screening.completed":
      return `Screening completed — score ${String(detail.score ?? "?")}`;
    case "decision.recorded":
      /*
       * OUTCOME FIRST, because `automatic` does not distinguish these three.
       *
       * A referral carries no `automatic` key at all -- 167 of them in the
       * demo data -- so branching on that alone rendered "Decided by a
       * reviewer" on an event written by the worker, for a case no reviewer
       * had yet seen. Exactly the wrong thing to tell an auditor, and the same
       * mistake as a status page printing a sentence it was never given.
       */
      if (detail.outcome === "referred") {
        return "Referred for review — automation did not decide this";
      }
      return detail.automatic
        ? `Decided automatically — ${String(detail.outcome ?? "")}`
        : `Decided by a reviewer — ${String(detail.outcome ?? "")}`;
    case "vendor.session_not_found":
      return "Provider had no record of the verification session";
    case "verification.unavailable":
      return "Closed — verification could not be completed";
    case "application.expired":
      return "Closed — verification was never started";
    default:
      return event.action;
  }
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
