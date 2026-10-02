-- 022: every NEW decision must say which ruleset it was taken under.
--
-- WHY A CONSTRAINT RATHER THAN FOUR CODE CHANGES
--
-- Migration 021 added the column and the four sites that insert decisions were
-- each taught to populate it: the worker's _insert_decision, the officer's
-- action in web/src/app/desk/[id]/actions.ts, the seeder, and the smoke script.
-- That is four places in two languages agreeing to do something, which is the
-- arrangement that holds until someone adds a fifth.
--
-- This is the same argument migration 017 makes about the lifecycle and 020
-- makes about re-scoring, and it reaches the same answer: if the rule matters,
-- the database states it once rather than each caller remembering it. The
-- officer's path in particular has no automated test — /web has a typechecker
-- and no test runner — so for that site this constraint IS the test.
--
-- NOT VALID, AND WHAT THAT ACTUALLY MEANS
--
-- NOT VALID tells Postgres to enforce the constraint on every INSERT and UPDATE
-- from now on, but NOT to scan the existing rows to check they satisfy it. Both
-- halves matter here:
--
--   * New rows are checked. A decision written without a version is refused,
--     from any language, including psql.
--
--   * Existing rows are left alone. Migration 021 deliberately left the column
--     NULL wherever the data could not establish which version a decision was
--     taken under — where the score was written after the decision, or where
--     risk_scored_at was NULL. A validating constraint would have forced those
--     rows to be filled with something, and the only available something is a
--     guess. The whole point of leaving them NULL was to not guess.
--
-- On the database this was written against, 021 filled all 915 rows and left
-- none NULL, so VALIDATE would currently succeed. It is still NOT VALID,
-- because the deployed database may differ and because the honest meaning of
-- this constraint is "from here on", not "always was".
--
-- If every NULL is ever resolved and the history is known to be complete:
--
--     alter table decisions validate constraint decisions_ruleset_version_present;
--
-- That takes a SHARE UPDATE EXCLUSIVE lock and scans the table; it does not
-- block reads or writes. Do it when the claim is true, not before.

alter table decisions
    add constraint decisions_ruleset_version_present
    check (risk_ruleset_version_at_decision is not null)
    not valid;
