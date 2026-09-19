-- 017: move the application lifecycle into the schema, where it belongs.
--
-- WHY THIS IS HERE AND NOT IN A SERVICE
--
-- This project's stated principle, from Phase 0, is that /db belongs to neither
-- service: two languages share one database, so the schema cannot be owned by
-- either one's ORM or either one's code.
--
-- Phase 4 then put the lifecycle rules in worker/lifecycle.py and did not
-- follow that principle. The rules were correct and well tested, and the web
-- service transitioned status twice without ever consulting them — held in line
-- by a WHERE clause in each query. That worked, and it was a CONVENTION
-- DUPLICATED ACROSS TWO LANGUAGES, which is precisely the arrangement /db
-- exists to avoid. The next transition added in TypeScript would not have had
-- the WHERE clause, and nothing would have complained.
--
-- So the transition table moves here. Both services are now bound by it whether
-- they consult it or not, in the same way both are bound by the CHECK on
-- applications.status and by the append-only triggers on audit_events.
--
-- THE RULE THIS ENCODES IS A COMPLIANCE RULE, NOT A DATA RULE
--
-- Note what is absent from the edges below: nothing reaches 'decided' except
-- from 'screening'. That is "you may not decide on a customer you have not
-- screened", and it is the kind of rule that should be impossible to break by
-- accident from any direction, in any language, including psql.

-- ---------------------------------------------------------------------------
-- The transitions, as data
-- ---------------------------------------------------------------------------

-- A table rather than a hardcoded list inside the function, for one reason:
-- it can be READ. worker/lifecycle.py keeps a copy of these edges so the Python
-- can decide without a round trip, and a test compares that copy against these
-- rows. A hardcoded plpgsql list would make the Python mirror something we
-- trust; a table makes it something we check.
create table application_transitions (
    from_status text not null,
    to_status   text not null,
    -- Why this edge exists, for whoever reads the table instead of the code.
    note        text not null,
    primary key (from_status, to_status),

    -- The statuses must be real ones. Reusing the same vocabulary as
    -- applications.status, which migration 001 constrains.
    constraint application_transitions_from_valid
        check (from_status in ('started','submitted','checking','screening','decided')),
    constraint application_transitions_to_valid
        check (to_status in ('started','submitted','checking','screening','decided')),
    -- A self-transition is not a transition. The trigger short-circuits those
    -- before it ever looks here, so a row for one would be dead weight.
    constraint application_transitions_not_self
        check (from_status <> to_status)
);

comment on table application_transitions is
    'The application lifecycle, as data. Authoritative: worker/lifecycle.py '
    'mirrors this, not the other way round. Enforced by the BEFORE UPDATE '
    'trigger on applications.';

insert into application_transitions (from_status, to_status, note) values
    ('started',   'submitted', 'a verification session was created'),
    ('started',   'checking',  'a fast vendor reported before we recorded the session'),
    ('started',   'screening', 'a vendor verdict arrived before any intermediate state'),
    ('submitted', 'checking',  'the vendor is reviewing'),
    ('submitted', 'screening', 'the vendor reached a verdict without pausing to review'),
    ('checking',  'screening', 'the vendor verdict arrived; our own checks begin'),
    ('screening', 'decided',   'a decision was recorded — the ONLY route to decided');

-- Forward skips are present because a fast vendor genuinely can jump a stage.
-- Every backward edge is absent, and 'decided' has no outgoing edge at all:
-- it is terminal, which is what stops a late webhook reopening a closed case.

-- ---------------------------------------------------------------------------
-- Enforcement
-- ---------------------------------------------------------------------------

-- Deliberately the same shape as audit_events_immutable() in migration 006: a
-- plpgsql function that raises, attached by a trigger, using a real SQLSTATE.
-- Following the precedent rather than inventing a second style of guard.
create function applications_enforce_lifecycle() returns trigger
language plpgsql as $$
begin
    -- Not every UPDATE is a transition. Writing a risk score, linking a vendor
    -- session, bumping updated_at — none of those touch status, and the
    -- lifecycle has nothing to say about them.
    --
    -- IS NOT DISTINCT FROM rather than =, so a NULL on either side compares
    -- sanely. status is NOT NULL today; relying on that here would make this
    -- quietly wrong the day it is not.
    if new.status is not distinct from old.status then
        return new;
    end if;

    if not exists (
        select 1
          from application_transitions t
         where t.from_status = old.status
           and t.to_status = new.status
    ) then
        raise exception
            'illegal application lifecycle transition: % -> %', old.status, new.status
            using errcode = 'restrict_violation',
                  hint = 'Permitted transitions are rows in application_transitions. '
                         'Nothing reaches ''decided'' except from ''screening''.';
    end if;

    return new;
end;
$$;

-- FOR EACH ROW, unlike the append-only triggers on audit_events.
--
-- That difference is deliberate and worth stating, since it departs from the
-- precedent in one respect. audit_events refuses the STATEMENT — the operation
-- is banned outright, so there is nothing to inspect and a statement-level
-- trigger both fires on zero-row statements and costs less. Here the operation
-- is permitted and it is the VALUES that decide, which can only be examined a
-- row at a time.
create trigger applications_lifecycle_guard
    before update on applications
    for each row execute function applications_enforce_lifecycle();

-- ---------------------------------------------------------------------------
-- What this deliberately does NOT do
-- ---------------------------------------------------------------------------
--
-- It does not constrain INSERT. An insert creates an application; it does not
-- transition one, and there is no OLD row to reason from. In practice every
-- caller inserts at 'started', and a CHECK already limits the column to the
-- five valid values, so the gap is that a direct INSERT could create a row
-- already at 'decided'. Closing that would mean deciding whether backfills and
-- restores are allowed to bypass the lifecycle, which is a larger question than
-- this migration should answer quietly.
