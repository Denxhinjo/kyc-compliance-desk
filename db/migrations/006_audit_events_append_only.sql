-- 006: make audit_events genuinely append-only.
--
-- Kept as its own migration because the enforcement mechanism is the point,
-- and it deserves to be readable on its own.
--
-- "Append-only" means rows go in, rows never change, rows never leave. A
-- correction is a NEW row saying something was corrected — never an edit to
-- the original. Three ways to enforce that, in increasing order of strength:
--
--   1. Convention — "we just don't do that". Worthless; someone will, at 2am,
--      during an incident.
--   2. Permissions — REVOKE UPDATE, DELETE. Real, but does not bind the
--      table's OWNER, which in our setup is the role the app connects as.
--   3. A trigger that raises — binds everyone, owner included. This.
--
-- The honest limit: a database SUPERUSER can disable a trigger. No in-database
-- mechanism survives someone with full control of the database. Institutions
-- solve that by shipping audit records to external append-only (WORM,
-- write-once-read-many) storage, where the people who can edit the database do
-- not control the archive. That is out of scope here, and this file says so
-- rather than pretending otherwise.

create function audit_events_immutable() returns trigger
language plpgsql as $$
begin
    raise exception 'audit_events is append-only: % is not permitted', tg_op
        using errcode = 'restrict_violation',
              hint = 'Record a correcting event as a new row instead.';
end;
$$;

-- FOR EACH STATEMENT, not FOR EACH ROW: the trigger then fires even when the
-- statement matches zero rows, so `delete from audit_events where false` is
-- refused too. It is also cheaper, since it fires once per statement.
create trigger audit_events_no_update
    before update on audit_events
    for each statement execute function audit_events_immutable();

create trigger audit_events_no_delete
    before delete on audit_events
    for each statement execute function audit_events_immutable();

-- TRUNCATE does not fire DELETE triggers. Without this one, the entire audit
-- log could be emptied by a statement the other two triggers never see.
create trigger audit_events_no_truncate
    before truncate on audit_events
    for each statement execute function audit_events_immutable();

-- Belt and braces. This does not bind the table owner, so the triggers above
-- are what actually enforce the rule; this closes the door for any role we
-- add later (a read-only reporting user, for instance).
revoke update, delete, truncate on audit_events from public;
