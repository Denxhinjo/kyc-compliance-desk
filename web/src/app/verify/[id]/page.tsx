import Link from "next/link";
import { notFound } from "next/navigation";
import { pool } from "@/lib/db";
import { DIDIT_MODE } from "@/lib/didit";
import { StartVerification } from "./start-verification";

export const dynamic = "force-dynamic";

export default async function VerifyPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  if (!/^[0-9a-f-]{36}$/i.test(id)) notFound();

  const { rows } = await pool.query<{
    id: string;
    status: string;
    full_name: string;
    address_country: string;
  }>(
    `select id, status, full_name, address_country from applications where id = $1`,
    [id],
  );

  const application = rows[0];
  if (!application) notFound();

  return (
    <main>
      <p className="crumb">
        <Link href={`/status/${application.id}`}>← Back to your application</Link>
      </p>
      <h1>Verify your identity</h1>
      <p className="sub">
        You will be taken to our verification provider to photograph an identity
        document.
      </p>

      <div className="panel">
        <dl className="summary">
          <dt>Name</dt>
          <dd>{application.full_name}</dd>
          <dt>Country</dt>
          <dd>{application.address_country}</dd>
        </dl>

        {DIDIT_MODE === "simulator" && (
          <p className="notice">
            Running against a local verification simulator, which speaks Didit&apos;s
            published contract. No third-party account is involved and nothing
            leaves this machine.
          </p>
        )}

        <StartVerification applicationId={application.id} />
      </div>

      <p className="hint">
        We never store your document images. The provider keeps them; we keep
        only a reference and the outcome.
      </p>
    </main>
  );
}
