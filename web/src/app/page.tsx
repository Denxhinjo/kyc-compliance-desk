import Link from "next/link";

export const dynamic = "force-dynamic";

const REPO = "https://github.com/Denxhinjo/kyc-compliance-desk";

export default function Home() {
  return (
    <main>
      <h1>KYC Onboarding &amp; Compliance Review Desk</h1>
      <p className="sub">
        Identity verification and the back-office desk where a compliance
        officer handles what cannot be decided automatically.
      </p>

      {/* The one line that says why any of this is worth clicking. A visitor
          who leaves after reading only this should still know what the demo is
          arguing — which is not "here is a KYC flow" but "here is how a
          regulated system stays correct when the things it depends on are
          unreliable". */}
      <p className="lede">
        Vendors drop webhooks, deliver them twice and deliver them out of
        order. This shows what it takes to stay correct anyway — an append-only
        audit log the database physically refuses to rewrite, a Postgres job
        queue that cannot hand the same work to two workers, and a risk score
        that can always say why.
      </p>

      <div className="home-links">
        <Link className="home-link" href="/desk">
          <strong>Compliance review desk →</strong>
          <span>
            The most important screen here. The queue of cases automation
            referred to a human, and the evidence needed to decide them.{" "}
            <strong>Open with a demo account — one click, no sign-up.</strong>
          </span>
        </Link>
        <Link className="home-link" href="/apply">
          <strong>Open an account →</strong>
          <span>
            The applicant&apos;s journey: details, identity verification,
            waiting for a decision.
          </span>
        </Link>
        <Link className="home-link" href="/stats">
          <strong>Stats →</strong>
          <span>
            Throughput, how much is decided without a human, and how long a
            decision takes. Every figure computed from the database, with its
            method stated.
          </span>
        </Link>
      </div>

      <h2>The code</h2>
      <div className="home-links">
        <a className="home-link" href={REPO}>
          <strong>Source on GitHub →</strong>
          <span>
            Two services and one database. Raw SQL in both, no ORM, because the
            schema is shared by TypeScript and Python and must not be owned by
            either one&apos;s idea of a model.
          </span>
        </a>
        <a className="home-link" href={`${REPO}#readme`}>
          <strong>README →</strong>
          <span>
            What this is, how it is put together, and the async and idempotency
            problem it exists to answer — with the limitations stated rather
            than skipped.
          </span>
        </a>
      </div>

      <p className="hint">
        Synthetic applicants throughout. Nothing here has processed a real
        person&apos;s data. Service status is on{" "}
        <Link href="/health">the health page</Link>.
      </p>
    </main>
  );
}
