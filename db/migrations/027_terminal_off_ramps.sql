-- 027: two terminal statuses for applications that will never progress.
--
-- THE PROBLEM THIS CLOSES
--
-- The lifecycle had five states and exactly one way out: a decision. Anything
-- that could never reach a decision had nowhere to go, so it stayed where it
-- was, forever, and was counted as "in flight" indefinitely.
--
-- Two such populations existed on production on 2026-10-14:
--
--   * 38 applications in 'submitted' or 'checking' whose verification session
--     the vendor has no record of. The sweeper asked about them on every run,
--     got a 404, logged a line and moved on. No audit event, no escalation, no
--     terminal state. Ages 20 to 55 days.
--   * 47 applications in 'started' with no verification session at all. The
--     sweeper deliberately and correctly ignores these -- they are waiting on
--     the applicant, not on the vendor -- but nothing else looked at them
--     either. Ages 19 to 53 days.
--
-- NEITHER OF THESE IS A REJECTION, and that distinction is the whole point of
-- adding statuses rather than reusing 'decided'. A rejection asserts a risk
-- finding about a person. Here there is no finding: in the first case we could
-- not obtain the evidence, in the second the applicant never supplied it.
-- Recording either as a rejection would put a false adverse decision on a
-- synthetic person's record, and on a real one it would be a reportable
-- outcome invented out of a process timeout.
--
--   verification_unavailable  the verification session cannot be resolved:
--                             the vendor has no record of it. Reached only
--                             from the two states where a session exists.
--   expired                   created, never began verification, and the
--                             abandonment window has passed. A process
--                             timeout.
--
-- Both are TERMINAL -- no outgoing edges, like 'decided'. An application that
-- lands in either is finished, and if the same person wants to proceed they
-- start a new application, which is what the repeat-applicant linking in
-- migration 028 makes visible.
--
-- A NOTE ON THE NAME 'expired', because it collides with something.
--
-- The vendor's own status vocabulary contains "Expired" and "Kyc Expired",
-- and both map to OUR 'screening' (worker/lifecycle.py,
-- VENDOR_STATUS_TO_LIFECYCLE): the vendor saying a session expired is a
-- terminal VENDOR outcome, so their part is done and ours begins. That is a
-- different fact from ours. Ours means the applicant never started. The two
-- vocabularies are deliberately separate -- see the comment on
-- VENDOR_STATUS_TO_LIFECYCLE -- and this is the sharpest example of why.
--
-- EVERY APPLICATION MUST STILL BE COUNTED SOMEWHERE. The stats page gets a
-- line for each of these, and a test asserts the partition is exhaustive, so
-- adding a status can never again quietly remove applications from the totals.

-- ---------------------------------------------------------------------------
-- The status vocabulary
-- ---------------------------------------------------------------------------

-- Replacing the CHECK from migration 001 rather than editing it: applied
-- migrations are history. Dropped and recreated in one transaction, so there
-- is no instant at which the column is unconstrained.
alter table applications
    drop constraint applications_status_valid;

alter table applications
    add constraint applications_status_valid
    check (status in ('started', 'submitted', 'checking', 'screening',
                      'decided', 'verification_unavailable', 'expired'));

-- Same for both ends of application_transitions, from migration 017.
alter table application_transitions
    drop constraint application_transitions_from_valid;

alter table application_transitions
    add constraint application_transitions_from_valid
    check (from_status in ('started', 'submitted', 'checking', 'screening',
                           'decided', 'verification_unavailable', 'expired'));

alter table application_transitions
    drop constraint application_transitions_to_valid;

alter table application_transitions
    add constraint application_transitions_to_valid
    check (to_status in ('started', 'submitted', 'checking', 'screening',
                         'decided', 'verification_unavailable', 'expired'));

-- ---------------------------------------------------------------------------
-- The new edges
-- ---------------------------------------------------------------------------
--
-- No change to the trigger is needed. applications_enforce_lifecycle() reads
-- application_transitions at every transition, so the permitted set is data and
-- these three rows are the whole enforcement change.
insert into application_transitions (from_status, to_status, note) values
    ('submitted', 'verification_unavailable',
     'the vendor has no record of the verification session, confirmed over at least three sweeps an hour or more apart'),
    ('checking', 'verification_unavailable',
     'the vendor has no record of the verification session, confirmed over at least three sweeps an hour or more apart'),
    ('started', 'expired',
     'no verification session was ever created and the abandonment window passed');

-- Deliberately absent:
--
--   started -> verification_unavailable
--     'started' has no session id, so "the vendor has no record of it" is not
--     a statement that can be made. Those applications expire instead.
--
--   screening -> verification_unavailable
--     Reaching 'screening' means a vendor verdict arrived. The evidence exists;
--     it is our own checks that are outstanding, and a parked screening job is
--     already handled by migration 023.
--
--   any edge OUT of either new status
--     Both are terminal. In particular neither reaches 'decided', so the
--     compliance rule that nothing is decided without being screened is
--     untouched -- and test_decided_is_reachable_only_from_screening still
--     finds exactly one source.
--
--   verification_unavailable -> expired, or the reverse
--     They describe different facts about different populations. An
--     application is in one or the other, never both in sequence.

comment on constraint applications_status_valid on applications is
    'Seven statuses: five lifecycle stages plus two terminal off-ramps for '
    'applications that can never reach a decision. Neither off-ramp is a '
    'rejection -- see migration 027.';
