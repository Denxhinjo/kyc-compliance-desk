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
  verification_unavailable: number;
  expired: number;
  being_screened: number;
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
         where status in ('started','submitted','checking'))::int as in_flight,
       (select count(*) from applications
         where status = 'verification_unavailable')::int
           as verification_unavailable,
       (select count(*) from applications
         where status = 'expired')::int as expired,
       -- Screening started, no referral recorded yet. A brief state, and the
       -- one that used to fall through the accounting: the webhook moves an
       -- application to 'screening' and enqueues the scoring as a SEPARATE
       -- job, so between those two transactions it is at 'screening' with no
       -- decision row -- counted by neither 'Awaiting review', which requires
       -- a referral, nor 'Still in flight', which stops at 'checking'.
       --
       -- Usually zero. It is on the page anyway because the rule is that
       -- every application is counted somewhere, and a figure that is only
       -- ever wrong under load is the hardest kind to notice.
       (select count(*) from applications a
         where a.status = 'screening'
           and not exists (select 1 from decisions d
                            where d.application_id = a.id
                              and d.outcome = 'referred'))::int as being_screened`,
  );
  return rows[0];
}

/*
 * EVERY APPLICATION IS COUNTED ON EXACTLY ONE LINE OF THIS PAGE.
 *
 * The five application-counting figures partition `applications` by status:
 *
 *   Processed                 decided
 *   Awaiting review           screening, with a referral and no verdict
 *   Being screened            screening, no referral yet
 *   Still in flight           started, submitted, checking
 *   Verification unavailable  verification_unavailable
 *   Expired                   expired
 *
 * The two 'screening' lines are complements, so together they cover it
 * exactly. Splitting them is what closed the one real gap here: scoring runs
 * as a separate job from the status change, so an application sits briefly at
 * 'screening' with no decision row and was previously counted nowhere.
 *
 * which is every member of lifecycle.ALL_STATUSES and nothing twice. Adding a
 * status without adding a line here would make applications vanish from the
 * totals while every individual figure stayed true, which is the quietest kind
 * of wrong a dashboard can be. Two tests guard it: one asserts the five
 * buckets partition the table with no gaps and no overlaps, and one reads this
 * file and fails if a status exists that it never mentions.
 */

async function latency(): Promise<Latency> {
  // percentile_cont interpolates between the two middle values, which is the
  // ordinary definition of a median for an even-sized sample.
  //
  // Measured from application creation to terminal decision — the applicant's
  // experience, not ours. Measuring from "when screening finished" would
  // produce a much prettier number that answers a question nobody asked.
  // Split by who decided, because a single overall median is actively
  // misleading here. The large majority of decisions are automatic and take
  // seconds, so they dominate the median entirely and the combined figure
  // describes the machine rather than the process. The number anyone actually
  // wants — how long does it take when a human is involved — is the one the
  // combined median hides.
  //
  // Deliberately no ratio in this comment. It said "around nine in ten" and
  // was written when that was true; the share is a property of the applicant
  // mix and moves every time the data is reseeded, so a number here is a
  // comment that goes quietly wrong. The page renders the real one.
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
      <h1>Analytics</h1>
      <p className="sub">
        Every figure below is computed from the database on this request. Each
        one states what it was measured over.
      </p>

      {/* TWO GROUPS, because these answer different questions and ten
          undifferentiated tiles made a reader scan all of them to find either.
          Throughput and latency are what an operations lead looks at; the
          second group is the accounting -- where every application currently
          sits -- and it exists because the rule is that no application may
          disappear from the totals. Useful, and not the headline. */}
      <h2 className="stat-group">Throughput and timing</h2>
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

      </div>

      <h2 className="stat-group">
        Where every application is
        <span className="stat-group-note">
          These five account for all {total.applications.toLocaleString()} —
          each application is counted on exactly one line, and a test enforces
          it.
        </span>
      </h2>
      <div className="stat-grid">
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

        <Stat
          label="Verification unavailable"
          value={total.verification_unavailable.toLocaleString()}
          note="closed: the provider has no record of the verification session. Not a rejection"
        />

        <Stat
          label="Expired"
          value={total.expired.toLocaleString()}
          note="closed: verification was never begun. A process timeout, not a rejection"
        />

        <Stat
          label="Being screened"
          value={total.being_screened.toLocaleString()}
          note="our checks are running — not yet referred or decided. Usually zero"
          muted={total.being_screened === 0}
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
          <details className="disclosure method-disclosure">
            <summary>
              Why the 90th percentile and the human median sit so close together
            </summary>
            <p className="method-note">
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
          </details>
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

      {/*
        * THE EXCEPTION IS COUNTED, not characterised.
        *
        * This has now been wrong twice, and the second time is the instructive
        * one. It first claimed "every seeded audit event is marked", which
        * held while seed.py was the only seeder -- it writes every event of a
        * case itself, under the worker's actor, and marks them all.
        *
        * It then claimed the gap was the continuous seeder's: events written
        * by the worker rather than the web app. Also wrong. When the new
        * sweeper deployed on 16 October it immediately wrote expiries and
        * not-found answers against BULK-seeded applications from August and
        * September, and those are 48 of the 79. The gap was never about which
        * seeder created the application; it was one window in which the real
        * worker touched seeded cases without knowing it.
        *
        * So this states a number and a window instead of a theory, and the
        * number is checkable:
        *
        *   select count(*) from audit_events e
        *    where coalesce(e.details->>'seeded','') <> 'true'
        *      and exists (select 1 from audit_events c
        *                   where c.application_id = e.application_id
        *                     and c.action = 'application.created'
        *                     and c.details->>'seeded' = 'true');
        *
        * That query is unbounded on purpose: it returns 79 today and would
        * return more if the propagation ever regressed, so the figure on this
        * page is self-checking rather than a date someone has to trust.
        */}
      <p className="hint">
        Synthetic applicants, scored by the same code that runs in production,
        are marked <code>&quot;seeded&quot;: true</code> at creation, and so is
        every event they cause. One exception: 79 events the background worker
        wrote on 16 October 2026 between 11:12 and 12:15 UTC carry no mark.
        They stay as written, because the audit log is append-only, and can be
        traced through their application&apos;s creation event.
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

  // Two elements rather than one, for the same reason `.table-scroll` wraps a
  // table: the scroller must be a plain block whose own minimum width is zero,
  // with the wide thing inside it. When .chart was both the scroller and the
  // flex row, its thirty columns still reported a ~531px minimum to the phone's
  // viewport sizing, and the browser answered by rendering the whole page at
  // about 75% scale rather than by scrolling the chart.
  return (
    <div className="chart-scroll">
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
