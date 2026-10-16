import Link from "next/link";

/**
 * The shared pieces every page is built from.
 *
 * One file rather than one per component, deliberately: these are small,
 * they change together, and a reader deciding "what is available to me"
 * should be able to answer it by scrolling once. If any of them grows past
 * about forty lines it moves out.
 *
 * WHAT IS AND IS NOT IN HERE
 *
 * Layout and presentation only. Nothing here queries, decides, or formats a
 * figure — a component that knew what a risk score meant would be a second
 * place for that rule to live. Callers pass finished values in.
 */

/* ------------------------------------------------------------------ Badge */

/**
 * The tone vocabulary is the one the stylesheet ALREADY has.
 *
 * `.pill` and its modifiers predate this component and are used in eight
 * places. Inventing a parallel `.badge` scale would have meant two badge
 * systems drifting apart, which is exactly the duplication this work exists to
 * remove — so `.pill` was restyled to the reference's rounded shape instead,
 * and every existing usage adopted it without its markup being touched.
 */
export type BadgeTone =
  | "neutral"
  | "ok"
  | "bad"
  | "approved"
  | "rejected"
  | "referred"
  | "sanctions"
  | "pep"
  | "adverse_media";

/**
 * A rounded chip. The ONE place a fully-rounded radius is allowed.
 *
 * TONE IS PASSED IN, NEVER DERIVED. A badge that mapped a number to a colour
 * would be a second implementation of the ruleset's thresholds, and the day it
 * disagreed with the scorer it would be telling an officer the opposite of the
 * truth on the screen where it matters most. The caller — which already has
 * the assessment — decides.
 */
export function Badge({
  tone = "neutral",
  children,
  title,
  big,
}: {
  tone?: BadgeTone;
  children: React.ReactNode;
  title?: string;
  /** The case page's headline verdict. */
  big?: boolean;
}) {
  const cls = ["pill", tone === "neutral" ? "" : tone, big ? "big" : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={cls} title={title}>
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------- Card */

/**
 * A surface. NOT called `.card`: that class already exists and means the
 * health page's status callout, with its coloured left border. Two meanings
 * for one class name is how a stylesheet starts producing surprises.
 */
export function Card({
  title,
  action,
  children,
  className = "",
  padded = true,
}: {
  title?: React.ReactNode;
  /** Top-right slot: a count, a link, a disclosure. */
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  /** Off when the child is a table that should meet the card's edges. */
  padded?: boolean;
}) {
  return (
    <section className={`surface ${className}`.trim()}>
      {(title || action) && (
        <div className="surface-head">
          {title ? <h2 className="surface-title">{title}</h2> : <span />}
          {action ? <div className="surface-action">{action}</div> : null}
        </div>
      )}
      <div className={padded ? "surface-body" : "surface-body flush"}>{children}</div>
    </section>
  );
}

/* ------------------------------------------------------------- PageHeader */

export function PageHeader({
  title,
  description,
  actions,
  back,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  back?: { href: string; label: string };
}) {
  return (
    <div className="page-head">
      {back ? (
        <p className="crumb">
          <Link href={back.href}>← {back.label}</Link>
        </p>
      ) : null}
      <div className="page-head-row">
        <div className="page-head-text">
          <h1>{title}</h1>
          {description ? <p className="sub">{description}</p> : null}
        </div>
        {actions ? <div className="page-head-actions">{actions}</div> : null}
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- DataTable */

/**
 * The table shell: horizontal scroll, sticky header, one border vocabulary.
 *
 * SCROLLS RATHER THAN SHRINKS. A dense operations table has a width below
 * which it stops being readable; letting it scroll sideways keeps every column
 * intact instead of crushing all of them. This project has already had to fix
 * that once on mobile, which is why `position: relative` is on the wrapper in
 * the stylesheet and not to be removed.
 */
export function DataTable({
  columns,
  children,
  caption,
  dense,
}: {
  columns: { key: string; label: React.ReactNode; className?: string }[];
  children: React.ReactNode;
  caption?: string;
  dense?: boolean;
}) {
  return (
    <div className="table-scroll">
      <table className={`grid${dense ? " dense" : ""}`}>
        {caption ? <caption className="sr-only">{caption}</caption> : null}
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} className={c.className}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------ MetricStrip */

export function MetricStrip({
  metrics,
}: {
  metrics: { label: string; value: React.ReactNode; note?: string }[];
}) {
  return (
    <div className="metric-strip" style={{ "--metric-count": metrics.length } as React.CSSProperties}>
      {metrics.map((m) => (
        <div className="metric" key={m.label}>
          <span className="metric-value">{m.value}</span>
          <span className="metric-label">{m.label}</span>
          {m.note ? <span className="metric-note">{m.note}</span> : null}
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------- Disclosure */

/**
 * Progressive disclosure, one pattern everywhere: raw payloads, long
 * methodology notes, the decided table.
 *
 * A native <details> rather than a React toggle, so it works before hydration
 * and the browser handles the open state, the keyboard and find-in-page.
 */
export function Disclosure({
  summary,
  count,
  children,
  className = "",
}: {
  summary: React.ReactNode;
  count?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <details className={`disclosure ${className}`.trim()}>
      <summary>
        {summary}
        {count !== undefined ? (
          <span className="disclosure-count">{count}</span>
        ) : null}
      </summary>
      <div className="disclosure-body">{children}</div>
    </details>
  );
}
