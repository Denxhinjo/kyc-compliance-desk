// `pg` is a CommonJS package. Importing its default export and destructuring
// works both inside Next.js (which bundles) and under plain Node ESM (which
// does not, and cannot reliably see named exports of a CJS module).
import pg from "pg";
import type { Pool, PoolClient } from "pg";

/**
 * A single shared connection pool for the whole web app.
 *
 * Why a pool: opening a TCP connection to Postgres costs a round trip plus
 * authentication. A pool keeps a handful of connections open and hands them
 * out per query, so that cost is paid once rather than per request.
 *
 * Why the globalThis dance: in development Next.js hot-reloads modules on
 * every file save. Without this, each reload would create a brand new pool
 * and leak the old one's connections until Postgres refused new ones.
 */
declare global {
  // eslint-disable-next-line no-var
  var __kycPool: Pool | undefined;
}

function createPool(): Pool {
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error(
      "DATABASE_URL is not set. Copy .env.example to .env at the repo root.",
    );
  }
  return new pg.Pool({
    connectionString,
    // Shows up in Postgres' pg_stat_activity, so you can tell at a glance
    // which service a connection belongs to. Free, and invaluable when
    // something is holding a lock.
    application_name: "kyc_web",
    max: 10,
    // Fail fast rather than hanging if the database is unreachable.
    connectionTimeoutMillis: 5_000,
  });
}

export const pool: Pool = globalThis.__kycPool ?? createPool();

if (process.env.NODE_ENV !== "production") {
  globalThis.__kycPool = pool;
}

export type DbHealth =
  | { ok: true; version: string; serverTime: string; database: string }
  | { ok: false; error: string };

/**
 * Phase 0's only real piece of behaviour: prove the web service can reach
 * Postgres and get an answer back. Never throws — a failed check is a result
 * we want to render, not an exception that blanks the page.
 */
export async function checkDatabase(): Promise<DbHealth> {
  try {
    const result = await pool.query<{
      version: string;
      server_time: string;
      database: string;
    }>(
      "select version() as version, now()::text as server_time, current_database() as database",
    );
    const row = result.rows[0];
    return {
      ok: true,
      version: row.version,
      serverTime: row.server_time,
      database: row.database,
    };
  } catch (err) {
    return { ok: false, error: describeError(err) };
  }
}

/**
 * Turn whatever was thrown into something a human can act on.
 *
 * Not as trivial as `err.message`. When Postgres is simply not running, Node
 * tries IPv6 and IPv4 in parallel, both are refused, and it throws an
 * AggregateError whose own `message` is an empty string — so the naive version
 * renders a blank error box at exactly the moment you most need the detail.
 */
function describeError(err: unknown): string {
  if (err instanceof AggregateError) {
    const inner = err.errors.map(describeError).filter(Boolean);
    const unique = [...new Set(inner)];
    if (unique.length === 0) {
      return `${err.name}: could not connect (no further detail).`;
    }
    const bullets = unique.map((m) => `  - ${m}`).join("\n");
    return `${err.name}: could not connect.\n${bullets}`;
  }
  if (err instanceof Error) {
    // Postgres driver errors carry a `code`: an errno like ECONNREFUSED, or a
    // five-character SQLSTATE such as 28P01 (bad password) or 3D000 (no such
    // database). It is usually the most diagnostic part of the whole error.
    const code = (err as NodeJS.ErrnoException).code;
    const base = err.message || err.name;
    return code ? `${base} (${code})` : base;
  }
  return String(err);
}

/**
 * Run a function inside a single database transaction.
 *
 * Everything the callback does on `client` either commits together or rolls
 * back together. This exists mainly so that a state change and the audit event
 * describing it cannot come apart — see logEvent() in ./audit.ts.
 *
 * The callback gets a PoolClient (one specific connection), not the pool.
 * Queries run on the pool each get an arbitrary connection, so they would not
 * be part of this transaction at all.
 */
export async function withTransaction<T>(
  fn: (client: PoolClient) => Promise<T>,
): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query("begin");
    const result = await fn(client);
    await client.query("commit");
    return result;
  } catch (err) {
    // If the rollback itself fails the connection is already unusable; the
    // original error is the interesting one, so don't let this mask it.
    await client.query("rollback").catch(() => undefined);
    throw err;
  } finally {
    // Always hand the connection back, committed or not. Forgetting this is
    // the classic way to exhaust a pool and hang the whole app.
    client.release();
  }
}
