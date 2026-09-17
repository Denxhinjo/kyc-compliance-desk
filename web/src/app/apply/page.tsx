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

      <div className="panel">
        <p className="notice">
          Do not enter real personal details. This is a demonstration and every
          applicant in it is invented.
        </p>
        <ApplyForm />
      </div>
    </main>
  );
}
