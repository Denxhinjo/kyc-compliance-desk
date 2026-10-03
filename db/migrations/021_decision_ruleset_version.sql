-- 021: record the ruleset version ON the decision, not only on the application.
--
-- DEPLOYMENT ORDER: not file order. 021, then deploy the code, then 022, then
-- 023, then 020 last. The reasoning is in the header of
-- 020_freeze_risk_after_decision.sql and in docs/deploy.md.
--
-- WHY THE APPLICATION'S COPY IS NOT ENOUGH
--
-- decisions already stores risk_score_at_decision, for the reason migration 004
-- gives: "an auditor asking 'why was this approved in March?' needs March's
-- number, not today's recalculation." The ruleset version was left on the
-- application, where it is one column of a mutable row rather than part of the
-- immutable record of what was decided.
--
-- Migration 020 froze those columns once a decision exists, which stops the
-- drift. This stores the answer where the question is asked: a decision row
-- should carry its own inputs, so reconstructing it never depends on another
-- table still saying what it said at the time.
--
-- Mirrors risk_score_at_decision deliberately — same table, same naming, same
-- "as it stood" meaning — rather than inventing a second idiom for the same
-- idea.

alter table decisions
    add column risk_ruleset_version_at_decision text;

-- ---------------------------------------------------------------------------
-- Backfill: verified, not assumed
-- ---------------------------------------------------------------------------
--
-- The honest version of this backfill is that it cannot be complete.
--
-- Copying applications.risk_ruleset_version onto every decision row would be
-- asserting that no application was re-scored after it was decided. Before
-- migration 020 that was possible — see its header, where it is demonstrated
-- against this database — so the assertion needs evidence, not confidence.
--
-- The evidence used here is risk_scored_at against decided_at. Where the score
-- was written BEFORE the decision was taken, the version on the application is
-- the version the decision was made under, and it is copied. Where the score
-- was written AFTER, the application may have been re-scored since, so its
-- version is not trustworthy for this purpose and the column is left NULL.
-- Where risk_scored_at is NULL there is nothing to compare, which is also not
-- trustworthy, and that is left NULL too.
--
-- NULL here means "not established from the data", which is a true statement.
-- A filled-in value that might be wrong is not, and would defeat the point of
-- the column — this exists so an auditor can rely on it.
--
-- MEASURED ON THE DEVELOPMENT DATABASE WHEN THIS WAS WRITTEN (2026-10-02):
--
--     decision rows                                   915
--     backfilled (risk_scored_at <= decided_at)       915
--     left NULL (risk_scored_at > decided_at)           0
--     left NULL (risk_scored_at is null)                0
--
-- So on that data the backfill is complete and nothing was left unknown. The
-- WHERE clause is still written to exclude the untrustworthy rows, because this
-- migration also runs against the deployed database, where the counts may
-- differ. The DO block below reports the actual figures for whichever database
-- it is applied to, so the result is in the migration log rather than inferred.
--
-- WHAT THIS EVIDENCE DOES NOT COVER, SINCE IT MATTERS
--
-- risk_scored_at moves only when the scoring code writes it. A direct
-- `update applications set risk_ruleset_version = ...` in psql would change the
-- version while leaving risk_scored_at untouched, and this backfill would copy
-- the new value believing it to be the old one. audit_events corroborates —
-- it is append-only, and on this database no application carries more than one
-- screening.completed row, nor any dated after its decision — but corroboration
-- is not proof. The claim this backfill makes is "consistent with every record
-- the system kept", not "certain".

update decisions d
   set risk_ruleset_version_at_decision = a.risk_ruleset_version
  from applications a
 where a.id = d.application_id
   and a.risk_scored_at is not null
   and a.risk_scored_at <= d.decided_at;

do $$
declare
    total      bigint;
    filled     bigint;
    after_dec  bigint;
    never      bigint;
begin
    select count(*) into total from decisions;
    select count(*) into filled from decisions
        where risk_ruleset_version_at_decision is not null;
    select count(*) into after_dec
      from decisions d join applications a on a.id = d.application_id
     where a.risk_scored_at > d.decided_at;
    select count(*) into never
      from decisions d join applications a on a.id = d.application_id
     where a.risk_scored_at is null;

    raise notice '021 backfill: % of % decision rows carry a ruleset version', filled, total;
    raise notice '021 backfill: % left NULL (scored after the decision, version not trustworthy)', after_dec;
    raise notice '021 backfill: % left NULL (never scored, nothing to compare)', never;
end;
$$;

-- No NOT NULL constraint, now or later by default.
--
-- The column is nullable because "we do not know" is a state this data genuinely
-- has, and forcing a value would mean inventing one — which is the one thing
-- this column exists to avoid.
--
-- New decisions populate it at every site that writes one. There are four, and
-- they are worth listing because a NOT NULL would have had to wait for all of
-- them anyway:
--
--     worker/screening_handler.py  _insert_decision — automatic and referral
--     web/src/app/desk/[id]/actions.ts              — the officer's verdict
--     worker/seed.py                                — synthetic history
--     worker/smoke_audit.py                         — diagnostic script
--
-- The first two are the production paths and carry the version from the
-- assessment and the application respectively. A test asserts that a decision
-- taken through each of them arrives with a version attached.
