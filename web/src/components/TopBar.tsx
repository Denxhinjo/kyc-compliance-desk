"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * The application's one navigation bar, on every page.
 *
 * A client component for one reason only: the current item. A server layout
 * cannot know which page is rendering beneath it, and "which of these am I
 * looking at" is the entire job of a nav bar — a set of links with no current
 * state is a list.
 *
 * TWO AUDIENCES, TWO SETS OF LINKS, AND THAT IS NOT A STYLE DECISION.
 *
 * The reference shows one bar with one set of destinations, because it is a
 * marketing page for a product with one kind of user. This application has
 * two, and they must not see the same bar:
 *
 *   staff    signed in at the desk. Review queue, rules versions, analytics.
 *   public   an applicant filling in a form, or a visitor on the landing page.
 *            Must NOT be offered staff destinations: /desk redirects to a
 *            login, so a nav full of links that bounce an applicant to a
 *            sign-in screen is worse than no nav at all.
 *
 * So the bar is one component with one appearance and a caller-supplied set of
 * items. Same furniture, different doors.
 *
 * LINKS ONLY TO PAGES THAT EXIST. The reference's nav has "Applicants" and
 * "Activity Log"; neither has a page here. Applicants are reached through the
 * queue and its search, and the audit trail is per-case, on the case, where it
 * is read. Inventing two pages to fill a nav would be building navigation for
 * a product that does not exist.
 */

export interface NavItem {
  href: string;
  label: string;
  /** Exact match only; otherwise the path prefix marks it current. */
  exact?: boolean;
  /** Also treat these as "inside" this section, e.g. a case under the queue. */
  alsoCurrent?: RegExp;
}

export const STAFF_NAV: NavItem[] = [
  {
    href: "/desk",
    label: "Review queue",
    exact: true,
    // A case page belongs to the queue's section; without this the bar has
    // nothing highlighted the moment you open a case, which is most of the
    // time anyone spends here.
    alsoCurrent: /^\/desk\/[0-9a-f-]{36}$/i,
  },
  { href: "/desk/rulesets", label: "Rules versions" },
  { href: "/stats", label: "Analytics" },
];

export const PUBLIC_NAV: NavItem[] = [
  { href: "/apply", label: "Open an account" },
  { href: "/stats", label: "Analytics" },
];

export function TopBar({
  items,
  right,
}: {
  items: NavItem[];
  /** Sign-out form, account email, or the demo marker. */
  right?: React.ReactNode;
}) {
  const pathname = usePathname();

  return (
    <header className="topbar">
      <div className="topbar-inner">
        <Link href="/" className="brand">
          {/* Inline SVG rather than an asset: one shape, no network request,
              and it inherits currentColor so it cannot drift from the text
              beside it. */}
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 20 20" width="16" height="16" fill="none">
              <path
                d="M10 1.5 3 4.3v5.1c0 4 2.9 7.5 7 9.1 4.1-1.6 7-5.1 7-9.1V4.3L10 1.5Z"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinejoin="round"
              />
              <path
                d="m6.8 9.9 2.3 2.3 4.1-4.4"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <span className="brand-name">KYC Compliance Desk</span>
        </Link>

        <nav className="topnav" aria-label="Sections">
          {items.map((item) => {
            const current = item.exact
              ? pathname === item.href || Boolean(item.alsoCurrent?.test(pathname))
              : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`topnav-item${current ? " current" : ""}`}
                aria-current={current ? "page" : undefined}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        {right ? <div className="topbar-right">{right}</div> : null}
      </div>
    </header>
  );
}
