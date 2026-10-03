import Link from "next/link";
import { pool } from "@/lib/db";
import { requireStaff } from "@/lib/session";

export const dynamic = "force-dynamic";
export const metadata = { title: "Rules versions — Review desk" };

/**
 * What each ruleset version contained, and what was decided under it.
 *
 * A decision records a version string. Until migration 024 that string was only
 * resolvable by checking out the right commit, which is not something an
 * auditor can do. This page is the lookup.
 *
 * Read-only by construction: it issues two selects and renders them. There is
 * nothing here that writes, because a ruleset is corrected by recording a new
 * version rather than by editing an old one — see the append-only triggers in
 * migration 024.
 */

interface VersionRow {
  version: string;
  effective_from: string;
  change_note: string;
  decision_count: string;
  monitoring_sourcing: string | null;
  monitoring_published_at: string | null;
  call_for_action_sourcing: string | null;
  approve_below: string | null;
  reject_at: string | null;
}

interface DecisionRow {
  application_id: string;
  full_name: string | null;
  outcome: string;
  decided_by: string;
  decided_at: string;
  sanctions_lists: string | null;
}

/** The lists whose provenance a version records, in display order. */
const LISTS: Array<{ key: "monitoring" | "call_for_action"; label: string }> = [
  { key: "monitoring", label: "FATF increased monitoring" },
  { key: "call_for_action", label: "FATF call for action" },
];

function sourcingOf(row: VersionRow, key: "monitoring" | "call_for_action") {
  return key === "monitoring" ? row.monitoring_sourcing : row.call_for_action_sourcing;
}

/** Which of a version's reference lists were never sourced. */
function unsourcedLists(row: VersionRow): string[] {
  return LISTS.filter((l) => sourcingOf(row, l.key) === "unsourced").map((l) => l.label);
}

export default async function RulesetsPage({
  searchParams,
}: {
  searchParams: Promise<{ version?: string }>;
}) {
  // The layout already guards navigation. Repeated here for the same reason the
  // case view repeats it: a page that depends on its parent for authorisation
  // stops being safe the moment it is moved.
  await requireStaff();
  const { version: selected } = await searchParams;

  const versionResult = await pool.query<VersionRow>(
    `select r.version,
            r.effective_from::text,
            r.change_note,
            count(d.id)::text                                            as decision_count,
            r.reference_data->'fatf_increased_monitoring'->>'sourcing'   as monitoring_sourcing,
            r.reference_data->'fatf_increased_monitoring'->>'published_at'
                                                                         as monitoring_published_at,
            r.reference_data->'fatf_call_for_action'->>'sourcing'        as call_for_action_sourcing,
            r.thresholds->>'auto_approve_below'                          as approve_below,
            r.thresholds->>'auto_reject_at_or_above'                     as reject_at
       from rulesets r
       left join decisions d on d.risk_ruleset_version_at_decision = r.version
      group by r.version, r.effective_from, r.change_note, r.reference_data, r.thresholds
      order by r.effective_from desc`,
  );
  const versions = versionResult.rows;
  const active = versions.find((v) => v.version === selected);

  // Only fetched when a version is selected: the full list is 900+ rows and
  // nobody needs it to read the versions table.
  const decisions = active
    ? (
        await pool.query<DecisionRow>(
          `select d.application_id::text,
                  a.full_name,
                  d.outcome,
                  d.decided_by,
                  d.decided_at::text,
                  -- The list version behind this case, joined through the
                  -- screening run rather than read from the application: a
                  -- decision depends on BOTH the rules and the list, and the
                  -- two are versioned in different places because they change
                  -- for different reasons.
                  (select string_agg(distinct
                            s.source || ' ' ||
                            coalesce(s.published_at::date::text, 'undated'), ', ')
                     from screening_results sr
                     join sanctions_snapshots s on s.id = sr.snapshot_id
                    where sr.application_id = d.application_id)   as sanctions_lists
             from decisions d
             join applications a on a.id = d.application_id
            where d.risk_ruleset_version_at_decision = $1
            order by d.decided_at desc
            limit 200`,
          [active.version],
        )
      ).rows
    : [];

  return (
    <main className="desk-main">
      <div className="desk-head">
        <h1>Rules versions</h1>
        <p className="dim small">
          What each version of the scoring rules contained, and what was decided
          under it. A decision depends on the rules and on the sanctions list;
          both are shown.
        </p>
      </div>

      {versions.length === 0 ? (
        <p className="empty">No ruleset versions recorded.</p>
      ) : (
        versions.map((row) => {
          const unsourced = unsourcedLists(row);
          return (
            <section className="panel-block wide" key={row.version}>
              <h2>
                <span className="mono">{row.version}</span>{" "}
                <span className="dim small">
                  in force from {row.effective_from.slice(0, 10)}
                </span>
              </h2>

              {/* SOURCING, STATED ON THE FACE OF THE ROW.
                  Not a tooltip and not an icon: whether the reference data was
                  ever sourced is the first thing someone defending a decision
                  needs, and a fact you have to hover to discover is a fact the
                  page is hiding. */}
              <p className="sourcing-row">
                {LISTS.map((list) => {
                  const state = sourcingOf(row, list.key);
                  const bad = state === "unsourced";
                  return (
                    <span key={list.key} className={`pill ${bad ? "rejected" : "approved"}`}>
                      {list.label}: {bad ? "UNSOURCED" : "sourced"}
                    </span>
                  );
                })}
              </p>

              {unsourced.length > 0 && (
                /* Past or future tense by whether anything was decided under it.
                   "Decisions under 2026-09-2 were scored against..." reads as an
                   accusation about rows that do not exist yet. */
                <p className="notice">
                  <strong>
                    {Number(row.decision_count) > 0
                      ? `Decisions under ${row.version} were scored against `
                      : `Decisions under ${row.version} will be scored against `}
                    {unsourced.length > 1
                      ? "unsourced country lists"
                      : "an unsourced country list"}
                    .
                  </strong>{" "}
                  {unsourced.join(" and ")}{" "}
                  {unsourced.length > 1 ? "were" : "was"} never fetched from the
                  publisher and never cited to a published statement.
                </p>
              )}

              <dl className="facts">
                <dt>Thresholds</dt>
                <dd className="mono">
                  auto-approve below {row.approve_below} · auto-reject at{" "}
                  {row.reject_at}
                </dd>
                <dt>Monitoring list published</dt>
                <dd className="mono dim">
                  {row.monitoring_published_at ?? "— no publication date"}
                </dd>
                <dt>Decisions</dt>
                <dd className="mono">{row.decision_count}</dd>
              </dl>

              {/* IN FULL, not truncated. The change note is the only place that
                  says why a version exists, and a version whose reason has been
                  cut off at 200 characters cannot be defended by whoever has to
                  defend it. */}
              <p className="change-note">{row.change_note}</p>

              <p>
                {Number(row.decision_count) === 0 ? (
                  <span className="dim small">
                    Nothing has been decided under this version yet.
                  </span>
                ) : row.version === selected ? (
                  <Link href="/desk/rulesets">Hide decisions</Link>
                ) : (
                  <Link href={`/desk/rulesets?version=${encodeURIComponent(row.version)}`}>
                    Show the {row.decision_count} decisions made under {row.version} →
                  </Link>
                )}
              </p>
            </section>
          );
        })
      )}

      {active && (
        <section className="panel-block wide">
          <h2>
            Decisions under <span className="mono">{active.version}</span>
          </h2>

          {unsourcedLists(active).length > 0 && (
            <p className="notice">
              <strong>Every decision below was scored against{" "}
              {unsourcedLists(active).length > 1
                ? "unsourced country lists"
                : "an unsourced country list"}.</strong>{" "}
              The rows are unchanged and deliberately so — they record what they
              were actually scored against, which is what a decision record is
              for.
            </p>
          )}

          {decisions.length === 0 ? (
            <p className="empty">No decisions were made under this version.</p>
          ) : (
            <>
              <div className="table-scroll">
                <table className="grid">
                  <thead>
                    <tr>
                      <th>Applicant</th>
                      <th>Outcome</th>
                      <th>Decided by</th>
                      <th>When</th>
                      <th>Sanctions list</th>
                    </tr>
                  </thead>
                  <tbody>
                    {decisions.map((d) => (
                      <tr key={`${d.application_id}-${d.decided_at}`}>
                        <td>
                          <Link href={`/desk/${d.application_id}`}>
                            {d.full_name ?? d.application_id.slice(0, 8)}
                          </Link>
                        </td>
                        <td>
                          <span className={`pill ${d.outcome}`}>{d.outcome}</span>
                          {unsourcedLists(active).length > 0 && (
                            /* The mark the requirement asks for, on the row
                               itself rather than only in the banner above —
                               these rows get copied, filtered and screenshotted
                               away from their heading. */
                            <span className="pill rejected" title="">
                              unsourced rules
                            </span>
                          )}
                        </td>
                        <td className="mono dim">{d.decided_by}</td>
                        <td className="mono dim">{d.decided_at.slice(0, 16)}</td>
                        <td className="mono dim">
                          {d.sanctions_lists ?? "— not screened"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {decisions.length === 200 && (
                <p className="dim small">
                  Showing the 200 most recent of {active.decision_count}.
                </p>
              )}
            </>
          )}
        </section>
      )}
    </main>
  );
}
