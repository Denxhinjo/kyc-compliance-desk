-- 020: a scored application stops being re-scorable once it has been decided.
--
-- THE HOLE THIS CLOSES
--
-- Migration 017 moved the lifecycle into the schema so that "you may not decide
-- on a customer you have not screened" could not be broken from any language.
-- It guards the status column and says so plainly: its trigger returns early
-- when status is unchanged, because writing a risk score is not a transition
-- and the lifecycle has nothing to say about it.
--
-- That left the scoring columns unguarded. An UPDATE setting risk_score,
-- risk_signals or risk_ruleset_version on an application at status 'decided'
-- succeeded, measured against this database:
--
--     risk_ruleset_version   -> SUCCEEDED
--     risk_score             -> SUCCEEDED
--     risk_signals           -> SUCCEEDED
--     status (control)       -> REJECTED: decided -> screening
--
-- The control is the point: 017's trigger was active throughout. It simply does
-- not cover these columns.
--
-- WHY THIS IS NOT THEORETICAL
--
-- A 'review' routing writes a decision row with outcome 'referred' and
-- DELIBERATELY leaves the application at 'screening' — a referral is a decision
-- about process, not about the applicant, and the case is not decided until a
-- human decides it. See _route in worker/screening_handler.py.
--
-- But run_screening's only guard is `status == 'screening'`, which those cases
-- satisfy. Jobs are at-least-once. A redelivered screening.run therefore
-- re-enters _store_assessment, whose UPDATE carries no status predicate, and
-- rewrites all three columns; _route then sees the existing referral and
-- returns without a second decision row. The referral keeps the score it was
-- made on while the application's ruleset version and signals are replaced
-- underneath it.
--
-- At the time this migration was written, 19 applications were in exactly that
-- state, and one of them — a UAE applicant — would re-score 60 -> 45, because
-- the United Arab Emirates left the FATF increased-monitoring list between
-- ruleset 2026-09-1 and 2026-09-2.
--
-- WHY A TRIGGER AND NOT A WHERE CLAUSE
--
-- Adding `and status = 'screening'` to that one UPDATE would have worked, and
-- would have been the duplicated-convention arrangement migration 017 exists to
-- remove: a rule enforced by a WHERE clause in one function in one language,
-- which the next writer in the other language does not inherit. The schema is
-- shared by TypeScript and Python and is owned by neither, so the guarantee
-- belongs here. worker/screening_handler.py is deliberately left unchanged.
--
-- WHAT THIS COSTS, STATED PLAINLY
--
-- A redelivered screening.run job for a referred case whose score WOULD change
-- now raises instead of writing. The job fails, retries, exhausts its attempts
-- and is parked. That is a louder failure than the silent rewrite it replaces,
-- and it is the direction this project chooses every time: prefer a visible
-- failure to a silent degradation. A redelivery that produces the SAME score
-- never reaches the UPDATE at all — _store_assessment short-circuits when
-- nothing changed — so ordinary duplicate delivery is unaffected.

create function applications_freeze_risk_after_decision() returns trigger
language plpgsql as $$
begin
    -- Not every UPDATE touches the scoring columns. Linking a vendor session,
    -- moving status, bumping updated_at — none of those are a re-score, and a
    -- decided application must still be able to do all of them.
    --
    -- IS DISTINCT FROM rather than <>, so a NULL on either side compares sanely:
    -- all four of these columns are nullable, and a write that sets one from
    -- NULL to a value is exactly the write this needs to catch.
    if new.risk_score          is not distinct from old.risk_score
       and new.risk_signals    is not distinct from old.risk_signals
       and new.risk_ruleset_version is not distinct from old.risk_ruleset_version
       and new.risk_scored_at  is not distinct from old.risk_scored_at
    then
        return new;
    end if;

    -- The existence of a decision row, not status = 'decided'.
    --
    -- Those are different sets and the difference is the whole point: a
    -- referred case has a decision row while still sitting at 'screening', and
    -- it is precisely the referred cases that the current code can re-score.
    -- Keying on status would leave the reachable path open and close only the
    -- unreachable one.
    if exists (select 1 from decisions d where d.application_id = new.id) then
        raise exception
            'application % has been decided: its risk columns are frozen', new.id
            using errcode = 'restrict_violation',
                  hint = 'A decision records the score it was made on. '
                         'Re-scoring afterwards would make the decision appear '
                         'to have been taken under rules it was never taken '
                         'under. Record a new assessment as a new decision '
                         'instead of rewriting this one''s inputs.';
    end if;

    return new;
end;
$$;

-- FOR EACH ROW, following 017 rather than 006.
--
-- 006 refuses the statement outright, so it has nothing to inspect and fires
-- statement-level. Here, as in 017, the operation is permitted and it is the
-- VALUES that decide, which can only be examined a row at a time.
create trigger applications_freeze_risk
    before update on applications
    for each row execute function applications_freeze_risk_after_decision();

-- ---------------------------------------------------------------------------
-- What this deliberately does NOT do
-- ---------------------------------------------------------------------------
--
-- It does not freeze the columns against a DELETE of the decision row. Removing
-- a decision would unfreeze the application, and nothing here prevents that —
-- but decisions carries a unique index per application and no code path deletes
-- from it, and a guard against deleting decisions is a different migration with
-- a different argument.
--
-- It does not constrain INSERT, for the same reason 017 does not: an insert
-- creates an application rather than re-scoring one, and there is no decision
-- row to find at that moment.
