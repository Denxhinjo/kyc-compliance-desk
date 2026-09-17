-- 016: a second recurring job, for queue retention.
--
-- Migration 011 created a partial unique index that keeps at most one run of a
-- recurring job type queued at a time, and it lists those types explicitly:
--
--   where status = 'queued' and job_type in ('vendor.sweep_stuck')
--
-- Listing them explicitly is deliberate — it means adding a recurring job is a
-- reviewable migration rather than a string appearing in application code — but
-- it does mean the index has to be replaced to add one.

drop index jobs_one_queued_per_recurring_type;

create unique index jobs_one_queued_per_recurring_type
    on jobs (job_type)
    where status = 'queued'
      and job_type in ('vendor.sweep_stuck', 'jobs.cleanup');

-- Retention deletes completed jobs by age, so the index it walks should be by
-- age within status rather than the other way round.
--
-- jobs_status_idx (status, updated_at) already serves this, but completed_at is
-- the column that actually means "finished at" — updated_at moves for reasons
-- that have nothing to do with completion.
create index jobs_done_completed_idx
    on jobs (completed_at)
    where status = 'done';
