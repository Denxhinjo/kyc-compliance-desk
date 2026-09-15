# /db — the shared schema

This directory is owned by **neither service**.

`/web` (TypeScript) and `/worker` (Python) both read and write the same
Postgres database. If the schema lived inside either one — managed by that
language's ORM — three things would go wrong:

1. The other service would learn about schema changes at runtime, in
   production, when a query failed.
2. Deployment ordering would become a trap: the worker could start against a
   schema the web app had not migrated yet.
3. Column types would get chosen for what the ORM models nicely rather than
   what Postgres does well.

So the database is treated as a third participant. Migrations are numbered SQL
files, applied in order by a standalone runner, and both services are merely
clients of a schema neither of them defines.

The cost of this, stated honestly: no generated types, and no compile-time
guarantee that application SQL matches the schema. Mismatches surface as
runtime errors. For a two-language system built around a shared database, that
is the right trade.

Migrations arrive in Phase 1.
