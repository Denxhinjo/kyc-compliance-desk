-- 015: indexes for the review desk's queue.
--
-- The queue asks: which applications are waiting for a human, and which has
-- been waiting longest? That is
--
--   applications in 'screening'
--   that have a 'referred' decision
--   and do NOT yet have a terminal one
--   ordered by when they were referred
--
-- Two indexes, one per half of that.

-- Finding the referrals, in arrival order. Partial, because referrals are a
-- small minority of decisions and the ones already superseded by a verdict are
-- of no interest to the queue.
create index decisions_referred_queue_idx
    on decisions (decided_at)
    where outcome = 'referred';

-- Answering "does this case already have a verdict?" per row.
--
-- The existing decisions_one_terminal_per_application index covers only
-- application_id and cannot answer a question about outcome without visiting
-- the table. This one can.
create index decisions_application_outcome_idx
    on decisions (application_id, outcome);
