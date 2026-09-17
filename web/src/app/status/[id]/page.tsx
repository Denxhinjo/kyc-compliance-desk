import Link from "next/link";
import { notFound } from "next/navigation";
import { pool } from "@/lib/db";
import { AutoRefresh } from "./auto-refresh";

export const dynamic = "force-dynamic";

interface ApplicationRow {
  id: string;
  status: string;
  full_name: string;
  date_of_birth: string;
  address_city: string;
  address_country: string;
  vendor_applicant_id: string | null;
  created_at: string;
}

interface AuditRow {
  id: string;
  occurred_at: string;
  actor: string;
  action: string;
  details: Record<string, unknown>;
}

/** What the applicant is told at each stage, in their language rather than ours. */
const STATUS_COPY: Record<string, { label: string; detail: string }> = {
  started: {
    label: "Not started",
    detail: "We have your details. The next step is to verify your identity.",
  },
  submitted: {
    label: "Documents received",
    detail: "Your documents are with our verification provider.",
  },
  checking: {
    label: "Checking your documents",
    detail: "This usually takes a few minutes.",
  },
  screening: {
    label: "Final checks",
    detail: "We are completing the last of our required checks.",
  },
  decided: {
    label: "Complete",
    detail: "A decision has been made on your application.",
  },
};

const LIFECYCLE = ["started", "submitted", "checking", "screening", "decided"];

export default async function StatusPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  // In Next 15 params is a Promise — route params can arrive after the
  // component starts rendering, so they are awaited like any other async value.
  const { id } = await params;

  // A malformed id would make Postgres raise a type error rather than return
  // nothing, so it is rejected before the query runs.
  if (!/^[0-9a-f-]{36}$/i.test(id)) {
    notFound();
  }

  const [applicationResult, auditResult] = await Promise.all([
    pool.query<ApplicationRow>(
      `select id, status, full_name, date_of_birth::text, address_city,
              address_country, vendor_applicant_id, created_at::text
         from applications where id = $1`,
      [id],
    ),
    pool.query<AuditRow>(
      // Ordered by id, not occurred_at: events written in one transaction share
      // a timestamp, because now() is the transaction's start time.
      `select id::text, occurred_at::text, actor, action, details
         from audit_events where application_id = $1 order by id`,
      [id],
    ),
  ]);

  const application = applicationResult.rows[0];
  if (!application) {
    notFound();
  }

  const copy = STATUS_COPY[application.status] ?? {
    label: application.status,
    detail: "",
  };
  const stageIndex = LIFECYCLE.indexOf(application.status);

  return (
    <main>
      <AutoRefresh />
      <p className="crumb">
        <Link href="/">← Home</Link>
      </p>
      <h1>Your application</h1>
      <p className="sub">
        {application.full_name} · opened {application.created_at.slice(0, 16)}
      </p>

      <div className="panel">
        <p className="status-label">{copy.label}</p>
        <p className="status-detail">{copy.detail}</p>

        <ol className="stages">
          {LIFECYCLE.map((stage, i) => (
            <li
              key={stage}
              className={
                i < stageIndex ? "stage done" : i === stageIndex ? "stage now" : "stage"
              }
            >
              {STATUS_COPY[stage].label}
            </li>
          ))}
        </ol>

        {application.status === "started" && (
          <p>
            <Link className="button" href={`/verify/${application.id}`}>
              Verify my identity
            </Link>
          </p>
        )}
      </div>

      <h2>History</h2>
      <p className="sub">
        Every change to this application, in the order it happened. This is read
        straight from the append-only audit log.
      </p>

      {auditResult.rows.length === 0 ? (
        <p className="empty">Nothing has happened yet.</p>
      ) : (
        <ol className="timeline">
          {auditResult.rows.map((event) => (
            <li key={event.id}>
              <span className="t-time">{event.occurred_at.slice(11, 19)}</span>
              <span className="t-action">{event.action}</span>
              <span className="t-actor">{event.actor}</span>
              {Object.keys(event.details).length > 0 && (
                <code className="t-details">{JSON.stringify(event.details)}</code>
              )}
            </li>
          ))}
        </ol>
      )}

      <p className="hint">
        Application id <code>{application.id}</code>
        {application.vendor_applicant_id && (
          <>
            {" · "}vendor session <code>{application.vendor_applicant_id}</code>
          </>
        )}
      </p>
    </main>
  );
}
