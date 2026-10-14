import Link from "next/link";
import { ApplyForm } from "./apply-form";

export const metadata = {
  title: "Start verification — KYC Demo",
};

export default function ApplyPage() {
  return (
    <main>
      <p className="crumb">
        <Link href="/">← Home</Link>
      </p>
      <h1>Open an account</h1>
      <p className="sub">
        We need to confirm your identity before your account can be used. This
        takes a couple of minutes.
      </p>

      {/* WHERE AM I, AND HOW MUCH IS LEFT.
          The journey has always had three steps; nothing said so until the
          applicant was already inside it, on the status page. A form that does
          not say how long it goes on for is a form people abandon on the
          second field. The same three labels appear on the status page, so the
          sequence a visitor is shown here is the one they then walk. */}
      <ol className="journey" aria-label="Application steps">
        <li className="journey-step now" aria-current="step">
          <span className="journey-num">1</span> Your details
        </li>
        <li className="journey-step">
          <span className="journey-num">2</span> Verify identity
        </li>
        <li className="journey-step">
          <span className="journey-num">3</span> Decision
        </li>
      </ol>

      <div className="panel">
        <p className="notice">
          Do not enter real personal details. This is a demonstration and every
          applicant in it is invented.
        </p>
        <ApplyForm />
      </div>

      <p className="hint">
        <strong>What happens next.</strong> You will be sent to our verification
        provider to photograph an identity document. Most applications are
        decided within a few minutes; some are passed to a person to look at,
        and those take longer. You will get a link to follow your application
        either way.
      </p>
    </main>
  );
}
