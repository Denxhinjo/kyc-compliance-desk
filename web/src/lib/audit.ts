import type { PoolClient } from "pg";

/**
 * Who caused an event.
 *
 * `system:*` for automatic action, `vendor:*` for something a third party told
 * us, `staff:*` for a human at the review desk. A plain string rather than an
 * enum because the set grows, and an auditor reading `staff:alice@example.com`
 * needs no lookup table to understand it.
 */
export type Actor = `system:${string}` | `vendor:${string}` | `staff:${string}`;

export interface AuditEventInput {
  /** Null for events about the system rather than one case. */
  applicationId: string | null;
  actor: Actor;
  /** A dotted verb: "application.created", "status.changed". */
  action: string;
  /** Whatever context this action needs. Stored as jsonb. */
  details?: Record<string, unknown>;
}

/**
 * Append one row to the audit log.
 *
 * THE IMPORTANT PART IS THE FIRST ARGUMENT. This takes a PoolClient — one
 * specific connection, already inside a transaction — rather than opening its
 * own. That is not an ergonomic detail, it is the whole design:
 *
 *   await withTransaction(async (client) => {
 *     await client.query("update applications set status = $1 where id = $2", ...);
 *     await logEvent(client, { ... });      // same transaction
 *   });
 *
 * The state change and the record of it commit together or not at all. If this
 * function opened its own connection instead, a crash between the two writes
 * would leave a state change with nothing explaining it — exactly the case an
 * audit log exists for.
 *
 * It is typed as PoolClient and not as "anything with .query()" on purpose:
 * the pool itself would satisfy a looser type, and passing the pool would
 * silently put the audit write outside the caller's transaction. Making that
 * a compile error is cheaper than finding it in production.
 *
 * There is no logEvent-without-a-transaction variant. A standalone event is
 * just a transaction with one statement in it.
 */
export async function logEvent(
  client: PoolClient,
  event: AuditEventInput,
): Promise<void> {
  await client.query(
    `insert into audit_events (application_id, actor, action, details)
     values ($1, $2, $3, $4)`,
    [
      event.applicationId,
      event.actor,
      event.action,
      // node-postgres would send a plain object as a Postgres composite type,
      // not as json. Stringifying makes it unambiguous.
      JSON.stringify(event.details ?? {}),
    ],
  );
}
