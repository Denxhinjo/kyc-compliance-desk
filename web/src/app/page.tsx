import Link from "next/link";
import { HeroQueue } from "./hero-queue";

export const dynamic = "force-dynamic";

const REPO = "https://github.com/Denxhinjo/kyc-compliance-desk";

/**
 * The front door.
 *
 * Two columns at desktop width, as in the reference: the argument on the left,
 * the product on the right. One call to action.
 *
 * THE COPY MAKES NO CLAIM THE DEMO CANNOT SHOW.
 *
 * The reference's page runs on "without the operational chaos" and "reliable,
 * scalable, designed for regulated environments" — adjectives a visitor cannot
 * check and this project has no standing to use. Every claim kept here is
 * either demonstrable in two clicks or measurable in the database, and the
 * four cards below follow the same rule: each names a mechanism, and each is
 * reachable from the nav.
 *
 * The headline's "any" rests on decisions.risk_ruleset_version_at_decision,
 * and the live queue beside it is read from the database on the request. The
 * explanatory subline that used to sit under the headline was removed on
 * 2026-10-17 — the page says less now, which is a stronger position than
 * saying more, since what remains is visible on the screen beside it.
 */
export default function Home() {
  return (
    <main className="landing">
      <section className="hero">
        <div className="hero-text">
          <p className="landing-eyebrow">KYC Compliance Desk</p>

          <h1 className="landing-headline">
            Reconstruct any KYC decision months later
          </h1>

          <div className="landing-cta">
            <Link className="button button-primary" href="/desk">
              Explore the live demo →
            </Link>
            <p className="landing-note">
              Synthetic applicants only. No sign-up — the demo account signs in
              with one click.
            </p>
          </div>
        </div>

        <HeroQueue />
      </section>

      {/* Four mechanisms, not four adjectives. Each is a thing the demo does
          and each is one click away. */}
      <section className="features">
        <article className="feature">
          <span className="feature-icon tint-accent" aria-hidden="true">
            <svg viewBox="0 0 20 20" width="18" height="18" fill="none">
              <path d="M3 10h4l2 5 2-10 2 5h4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
          <h2>Scores that say why</h2>
          <p>Fixed points, each with its reason.</p>
        </article>

        <article className="feature">
          <span className="feature-icon tint-ok" aria-hidden="true">
            <svg viewBox="0 0 20 20" width="18" height="18" fill="none">
              <path d="M4 10.5 8 14l8-8.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
          <h2>An audit trail that cannot be rewritten</h2>
          <p>
            The database refuses UPDATE, DELETE and TRUNCATE on the audit log
            and on recorded decisions. Not a convention — a trigger.
          </p>
        </article>

        <article className="feature">
          <span className="feature-icon tint-warn" aria-hidden="true">
            <svg viewBox="0 0 20 20" width="18" height="18" fill="none">
              <path d="M10 3v7l4 2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              <circle cx="10" cy="10" r="7" stroke="currentColor" strokeWidth="1.6" />
            </svg>
          </span>
          <h2>Correct when the provider is not</h2>
          <p>
            Identity providers drop webhooks, send them twice and send them out
            of order. A sweeper chases what never arrived and closes what the
            provider has lost.
          </p>
        </article>

        <article className="feature">
          <span className="feature-icon tint-neutral" aria-hidden="true">
            <svg viewBox="0 0 20 20" width="18" height="18" fill="none">
              <path d="M3 16V8m4.7 8V4m4.6 12v-6M17 16v-9" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </span>
          <h2>Figures computed, never stated</h2>
          <p>
            Every number on the analytics page is read from the database on the
            request, and says what it was measured over.
          </p>
        </article>
      </section>

      <p className="hint landing-foot">
        Synthetic applicants throughout. Nothing here has processed a real
        person&apos;s data. <a href={REPO}>Source on GitHub</a> ·{" "}
        <Link href="/health">Service health</Link>
      </p>
    </main>
  );
}
