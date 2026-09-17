-- 011: allow a job to schedule itself, without ever scheduling itself twice.
--
-- The sweeper runs every fifteen minutes. Two ways to arrange that:
--
--   a timer inside the worker loop — simple, but EVERY worker fires it, so the
--   sweep runs once per worker rather than once;
--
--   a queued job that re-enqueues itself — FOR UPDATE SKIP LOCKED already
--   guarantees exactly one worker claims it, the next run is visible in the
--   jobs table rather than buried in a process, and it survives restarts.
--
-- The second is better, and it needs exactly one safeguard: nothing may
-- accidentally schedule two.

-- A recurring job may have at most one run QUEUED at a time.
--
-- Scoped to 'queued' only, not to ('queued','running'), and that is the whole
-- trick: the sweeper re-enqueues its successor while it is still running, so a
-- constraint covering 'running' too would make a job unable to schedule its own
-- next run. As written, one 'running' plus one 'queued' is fine, two 'queued'
-- is impossible.
--
-- Same technique as decisions_one_terminal_per_application and the claim index:
-- a partial index enforcing a rule over just the rows the rule is about.
create unique index jobs_one_queued_per_recurring_type
    on jobs (job_type)
    where status = 'queued'
      and job_type in ('vendor.sweep_stuck');
