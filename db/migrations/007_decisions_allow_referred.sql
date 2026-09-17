-- 007: allow 'referred' as a decision outcome.
--
-- Supersedes the design note at the top of 004_decisions.sql, which said this
-- table held terminal outcomes only. That file is NOT edited: it has already
-- been applied, its checksum is recorded, and editing an applied migration is
-- how a database and a repository start silently disagreeing. The history of
-- the schema follows the same rule as the audit log — you correct it by adding,
-- never by rewriting.
--
-- The change: routing a case to a human is now recorded as a decision in its
-- own right. The system decided something — "I am not deciding this one" — and
-- that is a judgement with a reason and an author, which is exactly what this
-- table exists to hold.

alter table decisions
    drop constraint decisions_outcome_valid;

alter table decisions
    add constraint decisions_outcome_valid
    check (outcome in ('approved', 'rejected', 'referred'));

-- The consequence that matters.
--
-- A referred case gets TWO rows: the system's referral, then the human's
-- verdict. So "one decision per application" can no longer be true, and the old
-- unique index has to go.
--
-- Dropping it outright would give up a real guarantee, so it is replaced with a
-- PARTIAL unique index — one that covers only the rows matching its WHERE
-- clause. This one permits any number of referrals but still allows exactly one
-- terminal verdict per application, which is the guarantee actually worth
-- keeping.
--
-- Referrals are deliberately left unconstrained: a case that is re-screened
-- could legitimately be referred more than once, and audit_events records each
-- one in order.

drop index decisions_one_per_application;

create unique index decisions_one_terminal_per_application
    on decisions (application_id)
    where outcome in ('approved', 'rejected');

-- Queries asking "what was decided?" must now say which kind they mean:
--
--   select * from decisions
--    where application_id = $1
--      and outcome in ('approved', 'rejected');   -- the verdict
--
-- Asking without that filter can return a referral, which is a decision about
-- process rather than about the applicant.
