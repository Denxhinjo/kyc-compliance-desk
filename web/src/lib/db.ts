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

/**
 * TLS settings for the connection, or nothing at all locally.
 *
 * Heroku Postgres requires TLS and presents a certificate signed by its own
 * internal authority, which is not in Node's trust store. Left alone, `pg`
 * rejects it with `self-signed certificate in certificate chain` and every page
 * 500s — a deploy that builds perfectly and then cannot reach its database.
 *
 * `rejectUnauthorized: false` is the accepted answer for Heroku Postgres, and
 * it is worth being clear about what it costs: the connection is still
 * encrypted, but the certificate is not verified, so it protects against
 * passive eavesdropping and not against an active attacker who can already
 * intercept traffic inside Heroku's network. For a demo holding synthetic data
 * that is an acceptable trade. For real customer data it would not be — the
 * answer there is to pin Heroku's CA bundle explicitly rather than to stop
 * checking.
 *
 * Applied only when the URL is not local, so development keeps an unencrypted
 * connection to a container on the same machine rather than pretending.
 */
function sslOptions(connectionString: string) {
  const isLocal = /@(localhost|127\.0\.0\.1|db)[:/]/.test(connectionString);
  if (isLocal) return {};
  return { ssl: { rejectUnauthorized: false } };
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
    ...sslOptions(connectionString),
  });
}

/**
 * The pool, created on first use rather than on import.
 *
 * WHY THIS IS LAZY — it is not a micro-optimisation.
 *
 * `next build` imports every route module to collect page data. With the pool
 * built at module scope, that import threw `DATABASE_URL is not set` and the
 * BUILD failed — not the app, the build. It never showed up in development
 * because a local `.env` is always there, and it would not have shown up until
 * the first container build: Heroku does not expose config vars during a
 * container build either, by design, because a build artefact that needs
 * production secrets to be produced is not a build artefact.
 *
 * Deferring construction to the first query means building needs no database
 * and running needs no build-time configuration, which is the separation the
 * twelve-factor "build, release, run" split is actually about.
 *
 * The Proxy keeps the export a `Pool`, so all 26 call sites still read
 * `pool.query(...)` and nothing else in the app had to learn about this.
 */
function getPool(): Pool {
  const existing = globalThis.__kycPool;
  if (existing) return existing;

  const created = createPool();
  // In development Next.js hot-reloads modules on every save; without this,
  // each reload would leak the previous pool's connections. In production the
  // module is evaluated once, but caching here too keeps one code path.
  globalThis.__kycPool = created;
  return created;
}

export const pool: Pool = new Proxy({} as Pool, {
  get(_target, property) {
    const actual = getPool();
    const value = Reflect.get(actual, property, actual);
    // Methods must keep their `this`, or `pool.query()` calls a detached
    // function and pg loses the pool it belongs to.
    return typeof value === "function" ? value.bind(actual) : value;
  },
});

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
