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

---

## The migrations

| File | What it adds |
| --- | --- |
| `001_applications.sql` | The case file: applicant details, lifecycle status, risk score. |
| `002_vendor_events.sql` | The postbox: raw vendor webhooks, with the idempotency key. |
| `003_screening_results.sql` | One row per sanctions/PEP match, with match strength. |
| `004_decisions.sql` | Outcomes with a mandatory written reason. Its header comment says terminal outcomes only; **superseded by 007**. |
| `005_audit_events.sql` | The append-only story of every state change. |
| `006_audit_events_append_only.sql` | The triggers that make "append-only" true. |
| `007_decisions_allow_referred.sql` | Adds `referred` as an outcome; swaps the unique index for a partial one covering terminal verdicts only. |
| `008_jobs.sql` | The job queue: the only channel between the two services. |
| `009_vendor_didit.sql` | Switches the default vendor to Didit. Sumsub needed a business account. |

## The runner

```bash
python db/migrate.py status          # what is applied, what is pending
python db/migrate.py up --dry-run    # what would run
python db/migrate.py up              # run it
```

There is no `down`. Rollback scripts are written when you are calm and run when
you are not, and a `down` that has never been tested is worse than no `down` at
all. To undo something, write a new migration that undoes it — the same rule
that applies to the audit log applies to the schema's own history.

Four things the runner does that a loop over `psql -f` would not:

- **One transaction per migration.** Postgres has transactional DDL, so a
  migration either applies completely or not at all — never half.
- **An advisory lock**, so two runners cannot race. Irrelevant on a laptop,
  essential when a deploy might start two instances.
- **Checksums.** If an already-applied file is edited, the runner refuses to
  continue rather than letting the database and the repository silently
  disagree. Line endings are normalised first, so the same file does not hash
  differently on Windows and Linux.
- **`schema_migrations` is created by the runner**, not by a migration — it
  needs somewhere to record that 001 ran before it can run 001.

## Append-only, and what it does not guarantee

`audit_events` rejects `UPDATE`, `DELETE` and `TRUNCATE` via triggers. Triggers
bind the table owner, which `REVOKE` alone does not.

A database **superuser can disable a trigger**. No in-database mechanism
survives someone with full control of the database. Institutions solve this by
shipping audit records to external append-only (WORM) storage, where the people
who can edit the database do not control the archive. That is out of scope
here, and this file says so rather than implying a guarantee that does not
exist.

One consequence worth knowing: **gaps in `audit_events.id` are normal and do
not mean a row was deleted.** Identity sequences do not give numbers back when
a transaction rolls back, so a rolled-back event burns an id permanently.
