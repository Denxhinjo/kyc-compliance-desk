"use client";

import { usePathname } from "next/navigation";
import { PUBLIC_NAV, TopBar } from "./TopBar";

/**
 * The public top bar, rendered by the root layout — and suppressed under
 * /desk, which supplies its own.
 *
 * WHY NOT A ROUTE GROUP
 *
 * The tidy Next.js answer is `(public)` and `(staff)` route groups, each with
 * its own layout. That means moving nine page directories, and every move is a
 * chance to change a URL by accident on a demo whose links are already out in
 * the world. This is a visual change; it should not rename a route.
 *
 * So the root layout renders this, and it returns null where the desk's own
 * bar takes over. One bar on every page, two sets of links, no directories
 * moved.
 *
 * WHY THE DESK'S BAR IS SEPARATE AND NOT A PROP
 *
 * It carries the signed-in account and the sign-out form. Sign-out is a server
 * action, and the email comes from a server-side session read — neither can
 * cross into a client component that the root layout renders for every visitor,
 * including the ones who are not signed in.
 */
export function SiteChrome() {
  const pathname = usePathname();

  // The desk layout renders the staff bar, with the account and sign-out.
  if (pathname === "/desk" || pathname.startsWith("/desk/")) return null;

  return (
    <TopBar
      items={PUBLIC_NAV}
      right={
        <span className="demo-pill" title="Every applicant in this demo is invented">
          Demo
        </span>
      }
    />
  );
}
