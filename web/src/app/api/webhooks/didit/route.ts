import { withTransaction } from "@/lib/db";
import { enqueueJob } from "@/lib/jobs";
import { verifyWebhookSignature, type DiditWebhookEnvelope } from "@/lib/didit";

/**
 * The vendor webhook.
 *
 * WHY THIS ENDPOINT DOES ALMOST NOTHING
 *
 * Didit gives us a few seconds to answer, and retries on 5xx, 404, timeout or
 * connection failure — twice, at roughly one minute and four minutes, then it
 * drops the delivery permanently.
 *
 * So suppose we did the real work here: fetch the full result, screen against
 * sanctions lists, score, decide. Ten seconds. The vendor times out at five,
 * marks the delivery failed, and resends — while the first copy is still
 * halfway through mutating the same rows. Worse, the failure mode is
 * self-reinforcing: the slower we are, the more duplicates we get, which makes
 * us slower.
 *
 * The reframe that makes this obvious:
 *
 *   200 does not mean "I have done the work".
 *   200 means "I have durably taken responsibility for this message,
 *   and you may stop resending it."
 *
 * Once the message is in vendor_events and a job is on the queue, that is
 * true — both are committed, and Phase 2's retry machinery will see the work
 * through however long it takes. So this handler does exactly four fast, local
 * things: verify, store, enqueue, return.
 */

// Never cached, never prerendered: this has side effects and must run per
// request. Route handlers with a POST export are dynamic anyway; stating it
// removes any doubt.
export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  // RAW BYTES FIRST. The signature covers exactly what was transmitted. Parsing
  // to JSON and re-serialising changes whitespace and key order, so the
  // signature would no longer match — and the tempting "fix" is to verify
  // against the re-serialised form, which means verifying a string we built
  // ourselves rather than the one the vendor signed. Nothing parses this body
  // until the signature has passed.
  const rawBody = Buffer.from(await request.arrayBuffer());

  const verdict = verifyWebhookSignature(rawBody, request.headers);
  if (!verdict.ok) {
    // 401 and nothing else. In particular the body is NOT stored: this path is
    // unauthenticated, so anything that writes to the database here is a
    // resource-exhaustion vector — an attacker could fill a table for free.
    // Rejections go to the server log, which is bounded and rotated.
    console.warn(`[webhook] rejected: ${verdict.reason}`);
    return Response.json(
      { error: "signature verification failed" },
      { status: 401 },
    );
  }

  let payload: DiditWebhookEnvelope;
  try {
    payload = JSON.parse(rawBody.toString("utf8")) as DiditWebhookEnvelope;
  } catch {
    return Response.json({ error: "body is not valid JSON" }, { status: 400 });
  }

  // The vendor's own id for this message. This is the idempotency key, so a
  // message without one cannot be deduplicated and must be refused rather than
  // processed unsafely. 400 rather than 500: it will never become valid, so
  // there is no point in the vendor retrying it.
  const eventId = payload.event_id;
  if (typeof eventId !== "string" || eventId.length === 0) {
    return Response.json({ error: "missing event_id" }, { status: 400 });
  }

  const result = await withTransaction(async (client) => {
    // ON CONFLICT DO NOTHING is the whole idempotency mechanism. A redelivery
    // hits the unique constraint on (vendor, vendor_event_id) and returns no
    // row, so the enqueue below never happens.
    const inserted = await client.query<{ id: string }>(
      `insert into vendor_events
         (vendor, vendor_event_id, event_type, signature_verified, raw_body, payload)
       values ('didit', $1, $2, true, $3, $4)
       on conflict (vendor, vendor_event_id) do nothing
       returning id`,
      [
        eventId,
        payload.webhook_type ?? "unknown",
        rawBody.toString("utf8"),
        rawBody.toString("utf8"),
      ],
    );

    if (inserted.rows.length === 0) {
      return { duplicate: true as const };
    }

    const vendorEventId = inserted.rows[0].id;

    // In the SAME transaction. If this throws, the vendor_events row is rolled
    // back too, so we never end up with a stored message that nothing will
    // process — and the vendor's retry gets a clean slate. This is the reason
    // the queue is a Postgres table rather than Redis.
    const jobId = await enqueueJob(client, {
      type: "didit.process_webhook",
      payload: { vendor_event_id: vendorEventId },
    });

    return { duplicate: false as const, vendorEventId, jobId };
  });

  if (result.duplicate) {
    // Still a 200. The vendor did nothing wrong by resending, and telling them
    // it failed would only make them resend again.
    console.log(`[webhook] duplicate event ${eventId} ignored`);
    return Response.json({ received: true, duplicate: true }, { status: 200 });
  }

  console.log(
    `[webhook] stored event ${eventId} as vendor_event ${result.vendorEventId}, job ${result.jobId}`,
  );
  return Response.json(
    { received: true, duplicate: false, job_id: result.jobId },
    { status: 200 },
  );
}
