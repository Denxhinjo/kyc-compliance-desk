import Link from "next/link";
import { pool } from "@/lib/db";

export const dynamic = "force-dynamic";

export const metadata = { title: "Stats — KYC Demo" };

/**
 * Every number on this page is computed from the database. Nothing is
 * hardcoded, and each figure states what it was measured over.
 *
 * Two rules that this page exists to demonstrate:
 *
 *   MEDIAN, NOT MEAN, for time to decision. One case stuck for three weeks
 *   moves a mean enormously and a median not at all — and the mean is the one
 *   that would be quoted. The p90 is shown alongside, because a median on its
 *   own can hide a bad tail, and the tail is where the operational problem is.
 *
 *   PERCENTAGES CARRY THEIR DENOMINATOR. "68% auto-decided" means nothing
 *   without "of 415 decided". A percentage with no base is a number you cannot
 *   check, and the whole point of publishing these is that they can be checked.
 */

/** Below this, a percentage or a median says more than it knows. */
const MIN_N_FOR_STATS = 20;

/** The window for the latency figures, so the label is never a guess. */
const LATENCY_WINDOW = 500;

interface Totals {
  applications: number;
  decided: number;
  auto_decided: number;
  human_decided: number;
  approved: number;
  rejected: number;
  in_flight: number;
}

interface Latency {
  sample: number;
  median_seconds: number | null;
  p90_seconds: number | null;
  auto_sample: number;
  auto_median_seconds: number | null;
  human_sample: number;
  human_median_seconds: number | null;
}

interface QueueState {
  pending: number;
  oldest_seconds: number | null;
}

interface DayRow {
  day: string;
  approved: number;
  rejected: number;
}

async function totals(): Promise<Totals> {
  const { rows } = await pool.query<Totals>(
    `select
       (select count(*) from applications)::int as applications,
       (select count(*) from decisions
         where outcome in ('approved','rejected'))::int as decided,
       (select count(*) from decisions
         where outcome in ('approved','rejected') and decided_by = 'system')::int
           as auto_decided,
       (select count(*) from decisions
         where outcome in ('approved','rejected') and decided_by <> 'system')::int
           as human_decided,
       (select count(*) from decisions where outcome = 'approved')::int as approved,
       (select count(*) from decisions where outcome = 'rejected')::int as rejected,
       (select count(*) from applications
         where status in ('started','submitted','checking'))::int as in_flight`,
  );
  return rows[0];
}

async function latency(): Promise<Latency> {
  // percentile_cont interpolates between the two middle values, which is the
  // ordinary definition of a median for an even-sized sample.
  //
  // Measured from application creation to terminal decision — the applicant's
  // experience, not ours. Measuring from "when screening finished" would
  // produce a much prettier number that answers a question nobody asked.
  // Split by who decided, because a single overall median is actively
  // misleading here. Around nine in ten decisions are automatic and take
  // seconds, so they dominate the median entirely and the combined figure
  // describes the machine rather than the process. The number anyone actually
  // wants — how long does it take when a human is involved — is the one the
  // combined median hides.
  const { rows } = await pool.query<Latency>(
    `with recent as (
         select extract(epoch from (d.decided_at - a.created_at)) as seconds,
                d.decided_by = 'system' as automatic
           from decisions d
           join applications a on a.id = d.application_id
          where d.outcome in ('approved','rejected')
          order by d.decided_at desc
          limit $1
     )
     select count(*)::int as sample,
            percentile_cont(0.5) within group (order by seconds) as median_seconds,
            percentile_cont(0.9) within group (order by seconds) as p90_seconds,
            count(*) filter (where automatic)::int as auto_sample,
            percentile_cont(0.5) within group (order by seconds)
              filter (where automatic) as auto_median_seconds,
            count(*) filter (where not automatic)::int as human_sample,
            percentile_cont(0.5) within group (order by seconds)
              filter (where not automatic) as human_median_seconds
       from recent`,
    [LATENCY_WINDOW],
  );
  return rows[0];
}

async function queueState(): Promise<QueueState> {
  const { rows } = await pool.query<QueueState>(
    `select count(*)::int as pending,
            max(extract(epoch from (now() - r.decided_at))) as oldest_seconds
       from applications a
       join decisions r on r.application_id = a.id and r.outcome = 'referred'
      where a.status = 'screening'
        and not exists (select 1 from decisions t
                         where t.application_id = a.id
                           and t.outcome in ('approved','rejected'))`,
  );
  return rows[0];
}

async function decisionsByDay(): Promise<DayRow[]> {
  // generate_series so days with no decisions appear as zero rather than
  // vanishing — a chart that silently omits quiet days misrepresents the shape.
  const { rows } = await pool.query<DayRow>(
    `with days as (
         select generate_series(
             (current_date - interval '29 days')::date, current_date, interval '1 day'
         )::date as day
     )
     select to_char(days.day, 'YYYY-MM-DD') as day,
            coalesce(sum(case when d.outcome = 'approved' then 1 else 0 end), 0)::int
              as approved,
            coalesce(sum(case when d.outcome = 'rejected' then 1 else 0 end), 0)::int
              as rejected
       from days
       left join decisions d
         on d.decided_at::date = days.day
        and d.outcome in ('approved','rejected')
      group by days.day
      order by days.day`,
  );
  return rows;
}

export default async function StatsPage() {
  const [total, lat, queue, byDay] = await Promise.all([
    totals(),
    latency(),
    queueState(),
    decisionsByDay(),
  ]);

  const enoughData = total.decided >= MIN_N_FOR_STATS;
  const autoPercent = total.decided
    ? Math.round((total.auto_decided / total.decided) * 1000) / 10
    : 0;

  if (total.applications === 0) {
    return (
      <main>
        <h1>Stats</h1>
        <p className="empty">
          No applications yet. Run the seed script, or{" "}
          <Link href="/apply">create one</Link>, and these figures will fill in.
        </p>
      </main>
    );
  }

  return (
    <main>
      <p className="crumb">
        <Link href="/">← Home</Link>
      </p>
      <h1>Stats</h1>
      <p className="sub">
        Every figure below is computed from the database on this request. Each
        one states what it was measured over.
      </p>

      <div className="stat-grid">
        <Stat
          label="Processed"
          value={total.decided.toLocaleString()}
          note={`of ${total.applications.toLocaleString()} applications created`}
        />

        <Stat
          label="Auto-decided"
          value={enoughData ? `${autoPercent}%` : "—"}
          note={
            enoughData
              ? `${total.auto_decided.toLocaleString()} of ${total.decided.toLocaleString()} decided, without a human`
              : `too few decisions to report a percentage (${total.decided} of ${MIN_N_FOR_STATS} needed)`
          }
          muted={!enoughData}
        />

        <Stat
          label="Median, decided automatically"
          value={
            enoughData && lat.auto_median_seconds !== null
              ? formatDuration(lat.auto_median_seconds)
              : "—"
          }
          note={
            enoughData && lat.auto_median_seconds !== null
              ? `median across ${lat.auto_sample} automatic decisions in the last ${lat.sample}, from creation to decision`
              : `too few decisions to report a median (${total.decided} of ${MIN_N_FOR_STATS} needed)`
          }
          muted={!enoughData}
        />

        <Stat
          label="Median, decided by a human"
          value={
            lat.human_sample >= MIN_N_FOR_STATS && lat.human_median_seconds !== null
              ? formatDuration(lat.human_median_seconds)
              : "—"
          }
          note={
            lat.human_sample >= MIN_N_FOR_STATS && lat.human_median_seconds !== null
              ? `median across ${lat.human_sample} reviewed cases in the last ${lat.sample}`
              : `only ${lat.human_sample} reviewed case${lat.human_sample === 1 ? "" : "s"} — too few for a median to mean anything`
          }
          muted={lat.human_sample < MIN_N_FOR_STATS}
        />

        <Stat
          label="90th percentile, overall"
          value={
            enoughData && lat.p90_seconds !== null
              ? formatDuration(lat.p90_seconds)
              : "—"
          }
          note={
            enoughData && lat.p90_seconds !== null
              ? `one in ten of the last ${lat.sample} took at least this long — read it against the human median with the note below`
              : "shown once there are enough decisions"
          }
          muted={!enoughData}
        />

        <Stat
          label="Awaiting review"
          value={queue.pending.toLocaleString()}
          note={
            queue.pending === 0
              ? "the queue is clear"
              : `oldest has been waiting ${formatDuration(queue.oldest_seconds ?? 0)}`
          }
        />

        <Stat
          label="Still in flight"
          value={total.in_flight.toLocaleString()}
          note="created but not yet through verification — not counted as processed"
        />
      </div>

      {/* This belongs ON the page, not in a commit message. The relationship
          between these two figures is counter-intuitive in BOTH directions, so
          it renders whenever there is enough data rather than only when the
          numbers happen to look wrong — a reader who arrives on a day the p90
          sits just above the human median is no less puzzled than one who
          arrives on a day it sits below. */}
      {enoughData &&
        lat.p90_seconds !== null &&
        lat.human_median_seconds !== null && (
          <p className="method-note">
            <strong>
              Why the 90th percentile and the human median sit so close
              together.
            </strong>{" "}
            They measure different populations and are easy to misread against
            each other. {autoPercent}% of these decisions are automatic and
            finish in minutes, so the 90th percentile of <em>everything</em>{" "}
            lands only a little way into the reviewed cases rather than at
            their middle — which means it can fall <em>below</em> the human
            median entirely when the reviewed share is small. The two groups
            also overlap rather than sitting one after the other, because every
            figure here is measured from when the applicant applied, so the
            vendor&apos;s own latency is inside all of them: an automatic
            decision on an application the vendor sat on for hours is slower
            than a reviewed one the vendor answered in a minute. So the p90
            answers &ldquo;how long does a slow case take?&rdquo; across the
            whole population, while the human median answers &ldquo;how long
            does a reviewed case take?&rdquo;, and which of the two is larger
            moves with the mix rather than with how fast anyone is working.
          </p>
        )}

      <h2>Decisions over time</h2>
      <p className="sub small">
        Daily counts for the last 30 days. Days with no decisions are shown as
        zero rather than omitted.
      </p>
      <DayChart rows={byDay} />

      <h2>Outcome split</h2>
      {enoughData ? (
        <div className="table-scroll">
        <table className="grid">
          <thead>
            <tr>
              <th>Outcome</th>
              <th className="num">Count</th>
              <th className="num">Share</th>
              <th>Of</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>
                <span className="pill approved">approved</span>
              </td>
              <td className="num mono">{total.approved}</td>
              <td className="num mono">
                {percent(total.approved, total.decided)}
              </td>
              <td className="dim">{total.decided} decided</td>
            </tr>
            <tr>
              <td>
                <span className="pill rejected">rejected</span>
              </td>
              <td className="num mono">{total.rejected}</td>
              <td className="num mono">
                {percent(total.rejected, total.decided)}
              </td>
              <td className="dim">{total.decided} decided</td>
            </tr>
            <tr>
              <td>
                <span className="pill referred">decided by a human</span>
              </td>
              <td className="num mono">{total.human_decided}</td>
              <td className="num mono">
                {percent(total.human_decided, total.decided)}
              </td>
              <td className="dim">{total.decided} decided</td>
            </tr>
          </tbody>
        </table>
        </div>
      ) : (
        <p className="empty">
          Only {total.decided} decision
          {total.decided === 1 ? "" : "s"} so far. Shares of a base this small
          would mislead more than they inform, so they are not shown.
        </p>
      )}

      <p className="hint">
        Synthetic applicants, generated by <code>worker/seed.py</code> and
        scored by the same code that runs in production. Every seeded audit
        event is marked <code>&quot;seeded&quot;: true</code>.
      </p>
    </main>
  );
}

function Stat({
  label,
  value,
  note,
  muted,
}: {
  label: string;
  value: string;
  note: string;
  muted?: boolean;
}) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-value${muted ? " muted" : ""}`}>{value}</span>
      <span className="stat-note">{note}</span>
    </div>
  );
}

/** Plain CSS bars. A chart library would be a dependency for six rectangles. */
function DayChart({ rows }: { rows: DayRow[] }) {
  const peak = Math.max(1, ...rows.map((r) => r.approved + r.rejected));
  const total = rows.reduce((sum, r) => sum + r.approved + r.rejected, 0);

  if (total === 0) {
    return (
      <p className="empty">No decisions in the last 30 days.</p>
    );
  }

  return (
    <div className="chart" role="img" aria-label={`${total} decisions over the last 30 days`}>
      {rows.map((row) => {
        const sum = row.approved + row.rejected;
        return (
          <div className="chart-col" key={row.day} title={`${row.day}: ${row.approved} approved, ${row.rejected} rejected`}>
            <div className="chart-stack">
              <div
                className="bar rejected"
                style={{ height: `${(row.rejected / peak) * 100}%` }}
              />
              <div
                className="bar approved"
                style={{ height: `${(row.approved / peak) * 100}%` }}
              />
            </div>
            <span className="chart-label">{row.day.slice(8)}</span>
            <span className="sr-only">
              {row.day}: {sum} decisions
            </span>
          </div>
        );
      })}
    </div>
  );
}

function percent(part: number, whole: number): string {
  if (whole === 0) return "—";
  return `${Math.round((part / whole) * 1000) / 10}%`;
}

function formatDuration(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}
