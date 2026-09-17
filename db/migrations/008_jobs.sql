-- 008: jobs — the queue.
--
-- The architectural centrepiece. /web writes rows here; /worker claims them.
-- The two services never speak to each other; this table is the only channel.
--
-- Why a table and not Redis: transactional enqueue. A job can be inserted in
-- the SAME transaction as the row it is about, so "the event was stored but
-- the job was lost" is not a state this system can reach. With the queue in a
-- second datastore there is no ordering of those two writes that is correct.

create table jobs (
    id bigint generated always as identity primary key,

    -- Which handler should run this. 'sumsub.fetch_result', 'screening.run'.
    job_type text not null
        constraint jobs_job_type_not_blank
        check (length(trim(job_type)) > 0),

    -- Arguments for the handler. jsonb so each job type carries what it needs
    -- without the table growing a column per type.
    payload jsonb not null default '{}'::jsonb,

    -- queued  eligible to be claimed once run_after has passed
    -- running claimed by a worker, in progress
    -- done    finished successfully
    -- parked  given up on — retries exhausted, or a permanent error
    --
    -- Note there is no 'failed'. A failure is either "try again later"
    -- (back to queued with run_after in the future) or "stop trying"
    -- (parked). A permanent 'failed' state would be a bin nobody looks in.
    -- 'parked' is a dead letter queue: set aside for a human.
    status text not null default 'queued'
        constraint jobs_status_valid
        check (status in ('queued', 'running', 'done', 'parked')),

    -- Incremented when a job is CLAIMED, not when it fails. This is subtle and
    -- deliberate: if a job kills the worker outright (OOM, segfault, power
    -- loss) no failure handler ever runs. Counting at failure time would retry
    -- such a job forever, killing every worker that touches it. Counting at
    -- claim time means a poison pill eventually parks itself.
    attempts integer not null default 0
        constraint jobs_attempts_non_negative check (attempts >= 0),
    max_attempts integer not null default 5
        constraint jobs_max_attempts_positive check (max_attempts >= 1),

    -- When this job becomes eligible. Set into the future to apply backoff,
    -- which makes retry scheduling a plain WHERE clause rather than a timer.
    run_after timestamptz not null default now(),

    -- Which worker holds it and since when. locked_at is what the stale-job
    -- reaper uses to notice a worker died mid-job.
    locked_at timestamptz,
    locked_by text,

    -- Kept on parked jobs so a human can see why without reading logs.
    last_error text,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz
);

-- The claim index. PARTIAL — only 'queued' rows are in it.
--
-- Completed jobs become the overwhelming majority of this table and are never
-- claimed, so keeping them out keeps the index small enough to stay in memory.
-- The column order matches the claim query's ORDER BY exactly, so Postgres can
-- walk the index and stop at the first free row rather than sorting.
create index jobs_claim_idx on jobs (run_after, id) where status = 'queued';

-- For the stale-job reaper: find 'running' jobs whose worker went away.
create index jobs_running_idx on jobs (locked_at) where status = 'running';

-- For operational questions: how many parked, what finished recently.
create index jobs_status_idx on jobs (status, updated_at);

-- Reuses the function created in 001.
create trigger jobs_set_updated_at
    before update on jobs
    for each row execute function set_updated_at();
