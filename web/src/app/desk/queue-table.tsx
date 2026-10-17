"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";

export interface QueueRow {
  id: string;
  name: string;
  country: string;
  score: number;
  reason: string;
  waitingSeconds: number;
  /** Total applications from this person, including this one. 1 means unique. */
  personApplications: number;
}

/**
 * The queue, navigable without the mouse.
 *
 * j/k and the arrow keys move, Enter opens. This is not a flourish: someone
 * working a queue for six hours moves through it hundreds of times, and
 * reaching for the mouse between every case is the difference between a tool
 * and a website. Support inboxes and mail clients have worked this way for
 * decades, and officers arriving from other compliance software will try these
 * keys before reading any documentation.
 */
/**
 * The score bands offered as a filter.
 *
 * These are the RULESET'S OWN numbers, not invented buckets: under 20
 * auto-approves and 80+ auto-rejects, so everything in this queue is 20-79 by
 * construction and the only useful cut is within that band. Offering "low /
 * medium / high" would be inventing a vocabulary the scoring does not use, and
 * a reviewer who filtered by "high" would get cases the system does not call
 * high.
 */
const BANDS = [
  { value: "all", label: "Any score", test: () => true },
  { value: "60", label: "60 and above", test: (s: number) => s >= 60 },
  { value: "40", label: "40 to 59", test: (s: number) => s >= 40 && s < 60 },
  { value: "20", label: "20 to 39", test: (s: number) => s < 40 },
] as const;

export function QueueTable({ rows }: { rows: QueueRow[] }) {
  const router = useRouter();
  const [selected, setSelected] = useState(0);
  const [query, setQuery] = useState("");
  const [band, setBand] = useState<string>("all");
  const containerRef = useRef<HTMLDivElement>(null);

  /*
   * Filtered in the browser, not by a round trip.
   *
   * The queue is capped at 200 rows by the query that loads it, so the whole
   * set is already here and filtering it is a string comparison over a few
   * hundred items. A server round trip per keystroke would add latency and a
   * loading state to something that can be instant, and would need the page to
   * become dynamic on search params for no gain.
   *
   * If the cap is ever raised past a few thousand this should move server-side.
   */
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matcher = BANDS.find((b) => b.value === band) ?? BANDS[0];
    return rows.filter((row) => {
      if (!matcher.test(row.score)) return false;
      if (!needle) return true;
      // Name or case id: a reviewer arriving from an email has the id, a
      // reviewer arriving from a conversation has the name.
      return (
        row.name.toLowerCase().includes(needle) ||
        row.id.toLowerCase().includes(needle)
      );
    });
  }, [rows, query, band]);

  // Keep the cursor inside the filtered list. Without this, filtering down to
  // three rows while the cursor sits on row nine leaves Enter opening nothing.
  useEffect(() => {
    setSelected(0);
  }, [query, band]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      // Never hijack keys while someone is typing.
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;

      if (event.key === "j" || event.key === "ArrowDown") {
        event.preventDefault();
        setSelected((current) => Math.min(current + 1, visible.length - 1));
      } else if (event.key === "k" || event.key === "ArrowUp") {
        event.preventDefault();
        setSelected((current) => Math.max(current - 1, 0));
      } else if (event.key === "Enter") {
        event.preventDefault();
        const row = visible[selected];
        if (row) router.push(`/desk/${row.id}`);
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [visible, selected, router]);

  // Keep the selected row on screen when moving by keyboard.
  useEffect(() => {
    containerRef.current
      ?.querySelector<HTMLElement>(`[data-index="${selected}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  return (
    <div ref={containerRef}>
      {/* Two controls, and no more. Every extra filter on an operations screen
          is another thing to check before trusting that the list in front of
          you is the whole list. */}
      <div className="queue-filters">
        <input
          type="search"
          className="queue-search"
          placeholder="Search applicant or case ID"
          aria-label="Search by applicant name or case ID"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <select
          className="queue-band"
          aria-label="Filter by risk score"
          value={band}
          onChange={(event) => setBand(event.target.value)}
        >
          {BANDS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <span className="queue-count" aria-live="polite">
          {visible.length === rows.length
            ? `${rows.length} ${rows.length === 1 ? "case" : "cases"}`
            : `${visible.length} of ${rows.length}`}
        </span>
      </div>
      {/* Scrollable rather than squeezed. A dense operations table has a width
          below which it stops being readable; letting it scroll sideways keeps
          every column intact instead of crushing all of them.

          Wrapped in a .surface so the queue reads as one object on the page
          rather than rows floating on the background — the same treatment the
          metric strip and every panel get. */}
      <div className="surface">
      <div className="table-scroll">
      <table className="grid queue">
        <thead>
          <tr>
            <th className="col-wait">Waiting</th>
            <th className="col-score">Score</th>
            <th className="col-name">Applicant</th>
            <th className="col-country">Country</th>
            <th>Top flag</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((row, index) => (
            <tr
              key={row.id}
              data-index={index}
              className={index === selected ? "selected" : undefined}
              onClick={() => router.push(`/desk/${row.id}`)}
              onMouseEnter={() => setSelected(index)}
            >
              <td className={`mono ${waitClass(row.waitingSeconds)}`}>
                {formatWait(row.waitingSeconds)}
              </td>
              <td>
                <span className={`score ${scoreClass(row.score)}`}>{row.score}</span>
              </td>
              <td className="col-name">
                {row.name}
                {/* Only when there is more than one, and deliberately plain:
                    this is a fact about the applicant's history, not a flag
                    about their risk. The case page lists the others with
                    their dates. */}
                {row.personApplications > 1 && (
                  <span
                    className="repeat-badge"
                    title={`${row.personApplications} applications from this person (same name and date of birth). Each was screened separately.`}
                  >
                    {row.personApplications} applications
                  </span>
                )}
              </td>
              <td className="mono dim">{row.country}</td>
              {/* Clamped to one line so every row is the same height. The full
                  sentence is here on hover and printed in full on the case
                  view, which is where it is read rather than scanned. */}
              <td className="reason" title={row.reason}>
                {row.reason}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {/* An empty result from a filter is not an empty queue, and must not
          look like one -- "the queue is clear" is a different and much better
          piece of news than "your search matched nothing". */}
      {visible.length === 0 && (
        <p className="queue-empty">
          No case matches those filters.{" "}
          <button
            type="button"
            className="linkish"
            onClick={() => {
              setQuery("");
              setBand("all");
            }}
          >
            Clear filters
          </button>
        </p>
      )}
      </div>
      </div>
      <p className="keys">
        <kbd>j</kbd> <kbd>k</kbd> move · <kbd>↵</kbd> open case
      </p>
    </div>
  );
}

function formatWait(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

/** Colour carries meaning only. A case waiting days is a compliance problem. */
function waitClass(seconds: number): string {
  if (seconds >= 3 * 86400) return "wait-bad";
  if (seconds >= 86400) return "wait-warn";
  return "dim";
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
