"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * The desk's primary navigation.
 *
 * A client component for one reason: the active item. A server layout cannot
 * know which page is rendering beneath it, and "which of these am I looking
 * at" is the whole job of a nav bar — a set of links with no current state is
 * a list, not navigation.
 *
 * WHAT IS AND IS NOT HERE
 *
 * Three destinations, because three exist. The brief for this work listed four
 * — Review Queue, Applicants, Activity Log, Analytics — and the second and
 * third have no page behind them: applicants are reached through the queue and
 * through search, and the audit trail is per-case, on the case itself, where it
 * is read. Adding two nav items that lead to pages invented to fill them would
 * be building navigation for a product that does not exist yet.
 *
 * Service health is deliberately absent: it is an operational page, it is
 * linked from the landing page, and a reviewer working a queue has no use for
 * a Postgres version string.
 */

const ITEMS = [
  { href: "/desk", label: "Review queue", exact: true },
  { href: "/desk/rulesets", label: "Rules versions", exact: false },
  { href: "/stats", label: "Analytics", exact: false },
];

export function DeskNav() {
  const pathname = usePathname();

  return (
    <nav className="desk-nav" aria-label="Desk sections">
      {ITEMS.map((item) => {
        // The queue matches exactly; a case page (/desk/<id>) is part of the
        // queue's section, so it keeps the queue marked current rather than
        // leaving the nav with nothing highlighted.
        const current = item.exact
          ? pathname === item.href || /^\/desk\/[0-9a-f-]{36}$/i.test(pathname)
          : pathname.startsWith(item.href);

        return (
          <Link
            key={item.href}
            href={item.href}
            className={`desk-nav-item${current ? " current" : ""}`}
            aria-current={current ? "page" : undefined}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
