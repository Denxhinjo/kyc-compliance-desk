-- 026: make decisions append-only, the same way audit_events is.
--
-- WHY THIS WAS MISSING, AND WHY THAT MATTERED
--
-- Migration 006 made `audit_events` genuinely append-only, and the project has
-- described that guarantee in those words ever since. `decisions` never got
-- the same treatment, and the gap was invisible because everything around it
-- looks like protection:
--
--   * at most one terminal outcome per application
--     (decisions_one_terminal_per_application, a partial unique index)
--   * the named ruleset version must exist and must not be null
--     (decisions_ruleset_version_known, decisions_ruleset_version_present)
--   * reason and decided_by cannot be blank
--   * the scoring inputs behind the decision are frozen once it exists
--     (migration 020)
--
-- Every one of those constrains what a decision may SAY. None of them stops a
-- decision being rewritten afterwards. So a reader could reasonably conclude,
-- from "the audit log is append-only, enforced by the database", that the
-- verdict itself was equally safe — and it was not. `update decisions set
-- outcome = 'approved'` would have succeeded, silently, leaving the audit
-- trail's own record of the original decision intact and now contradicting it.
--
-- That is the most consequential row in the system: it is the answer to "why
-- was this customer refused". It is now the only answer, rather than the
-- current answer.
--
-- THE MECHANISM is migration 006's, deliberately unchanged: a trigger that
-- raises, because a trigger binds the table's OWNER, which is the role the
-- application connects as. REVOKE does not. Convention does not.
--
-- FOR EACH STATEMENT rather than FOR EACH ROW, also as in 006: the trigger
-- then fires even when the statement matches no rows, so
-- `delete from decisions where false` is refused too, and it costs one
-- invocation per statement rather than one per row.
--
-- ORDERING NOTE. Migration 021 backfills this table with an UPDATE. It runs
-- at 021, before this file, so a database rebuilt from scratch applies the
-- backfill and only then locks the table. Nothing needs to change in 021, and
-- this comment exists so that nobody "fixes" the apparent conflict later.
--
-- THE HONEST LIMIT, as in 006: a database superuser can disable a trigger. No
-- in-database mechanism survives someone with full control of the database.
-- Real immutability means write-once storage outside it, where the people who
-- can edit the database do not control the archive. Not built here.
--
-- WHAT THIS FORECLOSES. There is today no way to correct a decision, and after
-- this migration there cannot be one without a further migration. That is
-- deliberate and it is the right default, but it is a real constraint: the
-- correction path must be designed as a NEW decision that references the one
-- it supersedes, never as an edit. See the proposal in docs/decisions.md under
-- "Proposed: superseding assessments, not frozen ones", which is the same
-- shape of problem and is not yet built.

create function decisions_immutable() returns trigger
language plpgsql as $$
begin
    raise exception 'decisions is append-only: % is not permitted', tg_op
        using errcode = 'restrict_violation',
              hint = 'Record a superseding decision as a new row instead.';
end;
$$;

create trigger decisions_no_update
    before update on decisions
    for each statement execute function decisions_immutable();

create trigger decisions_no_delete
    before delete on decisions
    for each statement execute function decisions_immutable();

-- TRUNCATE does not fire DELETE triggers, so without this one the whole
-- decision history could be emptied by a statement the other two never see.
--
-- Unlike `rulesets` (migration 024), this trigger is what actually refuses a
-- TRUNCATE here: nothing holds a foreign key TO decisions, so there is no
-- referential error to fire first. The test asserts restrict_violation for
-- that reason, where the rulesets test had to assert something else.
create trigger decisions_no_truncate
    before truncate on decisions
    for each statement execute function decisions_immutable();

-- Belt and braces, and it does not bind the owner — the triggers above are the
-- enforcement. This closes the door for any role added later, such as a
-- read-only reporting user.
revoke update, delete, truncate on decisions from public;
