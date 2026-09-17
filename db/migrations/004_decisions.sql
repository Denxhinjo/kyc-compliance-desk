-- 004: decisions — the verdict.
--
-- Terminal outcomes only. Routing a case to a human is NOT a decision: it is a
-- status change, recorded in audit_events. If this table held both verdicts
-- and deferrals then "what was decided?" would stop having a single answer and
-- every query would need a filter.

create table decisions (
    id bigint generated always as identity primary key,

    application_id uuid not null references applications (id),

    outcome text not null
        constraint decisions_outcome_valid
        check (outcome in ('approved', 'rejected')),

    -- 'system' for an automatic decision, 'staff:<email>' for a human one.
    -- Stored as text rather than a foreign key to a users table: there is one
    -- demo staff account, and inventing a users table now would be building
    -- for a requirement that does not exist.
    decided_by text not null
        constraint decisions_decided_by_not_blank
        check (length(trim(decided_by)) > 0),

    -- Mandatory, and mandatory in the DATABASE rather than only in the form.
    -- A form validation is a request; a constraint is a guarantee. Phase 6
    -- requires a written reason from the compliance officer, and this is what
    -- an auditor reads when asking why a particular person was turned away.
    reason text not null
        constraint decisions_reason_not_blank
        check (length(trim(reason)) > 0),

    -- The score AS IT STOOD when the decision was made. Scoring rules change;
    -- an auditor asking "why was this approved in March?" needs March's
    -- number, not today's recalculation.
    risk_score_at_decision integer,

    decided_at timestamptz not null default now()
);

-- One terminal decision per application. If re-deciding an appealed case ever
-- becomes a requirement, that is a deliberate migration rather than something
-- that quietly starts happening.
create unique index decisions_one_per_application on decisions (application_id);
