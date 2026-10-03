-- 023: an officer may not decide a case whose new evidence could not be recorded.
--
-- DEPLOYMENT ORDER: not file order. 021, then deploy the code, then 022, then
-- 023, then 020 last. The reasoning is in the header of
-- 020_freeze_risk_after_decision.sql and in docs/deploy.md.
--
-- THE GAP THIS FILLS, AND WHY IT IS INTERIM
--
-- Migration 020 froze an application's risk columns once a decision row exists.
-- That is right: a referral records the evidence it was made on, and a later
-- re-score must not rewrite it.
--
-- But a redelivered screening.run can carry GENUINELY NEW evidence, not just a
-- re-derivation of the old. Two routes exist today:
--
--   * A newer sanctions list. run_screening calls active_snapshot(), which
--     returns the newest fully-loaded snapshot rather than the one the referral
--     was made against, so a reload can surface entities that did not exist
--     before.
--
--   * A corrected vendor result. 'Approved', 'Declined', 'Abandoned', 'Expired'
--     and 'Kyc Expired' all map to lifecycle 'screening'. A referred case is
--     already at 'screening', so a late correction writes vendor_status without
--     changing status and enqueues screening.run. Declined -> Approved moves the
--     document component by 40 points.
--
-- With 020 in place that job raises, retries, and parks. Nothing is written —
-- run_once wraps the handler in one transaction, so the matches roll back with
-- the assessment — and nothing is lost, but nothing is RECORDED either. The new
-- evidence exists only in a parked job's payload, where no officer will see it.
--
-- The real fix is supersession: append a new assessment rather than refusing or
-- overwriting, and show the officer both. That is a schema change and a page
-- change, written up as a proposed task in docs/decisions.md. This migration is
-- the interim guarantee that the gap cannot cause a decision to be taken in
-- ignorance: if evidence could not be recorded, the case cannot be decided by a
-- human until someone has looked.
--
-- WHY THIS IS A TRIGGER AND NOT A CHECK
--
-- A CHECK constraint sees one row. This rule is about a row in another table, so
-- it has to be a trigger. It does couple decisions to jobs, which is worth
-- noticing rather than glossing: the queue is normally an implementation detail
-- and this makes it part of a compliance rule. The justification is that a
-- parked screening job is not a queue detail — it is the system stating that it
-- knows something it failed to write down.
--
-- WHY ONLY THE OFFICER'S DECISIONS
--
-- The trigger fires only for decided_by like 'staff:%'. The automatic path is
-- deliberately exempt, for a reason that is easy to miss: a parked job for an
-- application with no decision yet would otherwise block the worker from
-- recording the very screening that just succeeded. The system writing down
-- what it found is never the thing this rule wants to prevent. What it prevents
-- is a human signing off while the system is holding unrecorded evidence.
--
-- WHY 'parked' AND NOT ALSO A RETRYING JOB
--
-- The jobs table has four statuses — queued, running, done, parked — and no
-- 'failed'. A retrying job goes back to 'queued' with last_error set, so
-- 'parked' is the only terminal failure and the only unambiguous signal.
--
-- That leaves a window: a job carrying new evidence is retrying, has not parked
-- yet, and the officer decides during the backoff. The window is bounded by
-- max_attempts and the backoff schedule, and for a frozen referred case parking
-- is deterministic rather than likely — the assessment will raise on every
-- attempt. Blocking on 'queued' instead would freeze the desk for every
-- transient blip, including first deliveries that are about to succeed, which
-- trades a narrow window for a broad outage. The case view warns on the broader
-- condition even though the database refuses only on the narrow one.

create function decisions_refuse_while_evidence_is_stuck() returns trigger
language plpgsql as $$
declare
    stuck_job bigint;
begin
    -- Automatic decisions are exempt; see the header.
    if new.decided_by not like 'staff:%' then
        return new;
    end if;

    select j.id into stuck_job
      from jobs j
     where j.job_type = 'screening.run'
       and j.status = 'parked'
       and j.payload->>'application_id' = new.application_id::text
     limit 1;

    if stuck_job is not null then
        raise exception
            'application % has screening evidence that could not be recorded (job %)',
            new.application_id, stuck_job
            using errcode = 'restrict_violation',
                  hint = 'A screening job for this application is parked, which '
                         'means the system found something it could not write '
                         'down. Deciding now would be deciding without it. '
                         'Investigate the parked job first.';
    end if;

    return new;
end;
$$;

create trigger decisions_evidence_must_not_be_stuck
    before insert on decisions
    for each row execute function decisions_refuse_while_evidence_is_stuck();

-- ---------------------------------------------------------------------------
-- What this deliberately does NOT do
-- ---------------------------------------------------------------------------
--
-- It does not block the officer from READING the case, and it does not hide the
-- queue entry. A case in this state is still visible and still reviewable; only
-- recording a verdict is refused. An officer who cannot see why would be worse
-- off than one who cannot decide, so the case view shows the reason.
--
-- It does not resolve the parked job. Clearing it is a human act with a human
-- judgement attached — re-run it, or accept that the evidence does not change
-- the case — and automating that away is exactly what this exists to prevent.
