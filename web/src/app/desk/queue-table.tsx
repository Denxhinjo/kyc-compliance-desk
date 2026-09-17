"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

export interface QueueRow {
  id: string;
  name: string;
  country: string;
  score: number;
  reason: string;
  waitingSeconds: number;
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
export function QueueTable({ rows }: { rows: QueueRow[] }) {
  const router = useRouter();
  const [selected, setSelected] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      // Never hijack keys while someone is typing.
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;

      if (event.key === "j" || event.key === "ArrowDown") {
        event.preventDefault();
        setSelected((current) => Math.min(current + 1, rows.length - 1));
      } else if (event.key === "k" || event.key === "ArrowUp") {
        event.preventDefault();
        setSelected((current) => Math.max(current - 1, 0));
      } else if (event.key === "Enter") {
        event.preventDefault();
        const row = rows[selected];
        if (row) router.push(`/desk/${row.id}`);
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [rows, selected, router]);

  // Keep the selected row on screen when moving by keyboard.
  useEffect(() => {
    containerRef.current
      ?.querySelector<HTMLElement>(`[data-index="${selected}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  return (
    <div ref={containerRef}>
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
          {rows.map((row, index) => (
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
              <td className="col-name">{row.name}</td>
              <td className="mono dim">{row.country}</td>
              <td className="reason">{row.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
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

function scoreClass(score: number): string {
  if (score >= 80) return "score-high";
  if (score >= 50) return "score-mid";
  return "score-low";
}
