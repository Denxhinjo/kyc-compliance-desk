import Link from "next/link";

export const dynamic = "force-dynamic";

const REPO = "https://github.com/Denxhinjo/kyc-compliance-desk";

/**
 * The front door.
 *
 * ONE job: say what this is and get the visitor into the desk. It used to
 * offer five links of equal weight — desk, apply, stats, source, readme — which
 * is not a choice so much as an invitation to leave. A first screen with five
 * CTAs has none.
 *
 * So: name, one line on what it does, one line on why it is hard, one button.
 * Everything else moves below the fold or into the app's own navigation, where
 * someone who wants it will look for it anyway.
 */
export default function Home() {
  return (
    <main className="landing">
      <p className="landing-eyebrow">KYC Compliance Desk</p>

      <h1 className="landing-headline">
        KYC reviews, without the operational chaos.
      </h1>

      <p className="landing-sub">
        Identity verification, risk-based review queues, and decisions you can
        still explain months later — with the evidence attached to each one.
      </p>

      <div className="landing-cta">
        <Link className="button button-primary" href="/desk">
          Explore the live demo
        </Link>
        <p className="landing-note">
          Synthetic applicants only. No sign-up — the demo account signs in with
          one click.
        </p>
      </div>

      {/* The argument, for anyone still reading. Kept to one paragraph and
          placed AFTER the call to action: it is the reason the project is
          interesting, but it is not what gets someone into the product. */}
      <section className="landing-detail">
        <h2>What it is built to survive</h2>
        <p>
          Identity providers drop webhooks, deliver them twice, and deliver them
          out of order. This stays correct anyway: an append-only audit trail
          the database physically refuses to rewrite, a Postgres job queue that
          cannot hand the same work to two workers, and a risk score that can
          always say which signals produced it and under which version of the
          rules.
        </p>

        <div className="landing-links">
          <Link href="/apply">Applicant journey</Link>
          <Link href="/stats">Analytics</Link>
          <a href={REPO}>Source on GitHub</a>
          <Link href="/health">Service health</Link>
        </div>
      </section>
    </main>
  );
}
