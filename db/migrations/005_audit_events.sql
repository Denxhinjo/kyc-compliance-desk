-- 005: audit_events — the story.
--
-- Every other table records STATE. This records CAUSATION.
--
-- If applications.status says 'decided' and decisions.outcome says 'rejected',
-- you know what happened but not why, in what order, or on whose authority.
-- A regulator's questions are almost always historical, and current state has
-- already forgotten: applications.status is overwritten on every transition,
-- so the intermediate states exist only if something wrote them down as they
-- happened.
--
-- The rule that makes this work is not in the schema, it is in the helpers:
-- the audit write goes in the SAME TRANSACTION as the state change it
-- describes. Otherwise a crash between the two leaves a state change with no
-- trail — precisely the case where you most need one.

create table audit_events (
    id bigint generated always as identity primary key,

    -- now() is the TRANSACTION start time, so several events written in one
    -- transaction all share this value. Order timelines by id, not by this
    -- column, or events from the same transaction come back arbitrarily.
    occurred_at timestamptz not null default now(),

    -- Nullable: some events are about the system rather than a single case.
    application_id uuid references applications (id),

    -- Who caused it: 'system:web', 'system:worker', 'vendor:sumsub',
    -- 'staff:alice@example.com'.
    actor text not null
        constraint audit_events_actor_not_blank
        check (length(trim(actor)) > 0),

    -- What happened, as a dotted verb: 'application.created',
    -- 'status.changed', 'screening.completed', 'decision.recorded'.
    action text not null
        constraint audit_events_action_not_blank
        check (length(trim(action)) > 0),

    -- Whatever context that action needs. jsonb rather than a column per
    -- action type, because the set of actions grows every phase and the table
    -- should not need altering each time one is added.
    details jsonb not null default '{}'::jsonb
);

-- The case timeline (Phase 6): every event for one application, in order.
create index audit_events_application_idx on audit_events (application_id, id);

-- "Show me every case where a human overrode the system" — queries by action
-- across all cases.
create index audit_events_action_idx on audit_events (action, occurred_at);
