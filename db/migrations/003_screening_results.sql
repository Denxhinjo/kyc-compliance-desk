-- 003: screening_results — what the sanctions check found.
--
-- ONE ROW PER MATCH, not one per application. A common name can hit several
-- list entries, and a compliance officer needs to see and dismiss each one
-- separately. Collapsing them into a single "matched: yes" would destroy
-- exactly the detail the review desk exists to show.

create table screening_results (
    id bigint generated always as identity primary key,

    application_id uuid not null references applications (id),

    -- Where the list data came from. OpenSanctions for this project.
    source text not null default 'opensanctions',

    -- sanctions      — on a government list we may not do business with
    -- pep            — Politically Exposed Person: prominent public role, or
    --                  close to one. Not disqualifying; triggers scrutiny.
    -- adverse_media  — negative press coverage
    match_type text not null
        constraint screening_results_match_type_valid
        check (match_type in ('sanctions', 'pep', 'adverse_media')),

    -- The specific list, e.g. 'OFAC SDN', 'EU Consolidated', 'UK HMT'.
    list_name text not null,

    -- The name as it appears on the list, which is rarely spelled exactly the
    -- way the applicant spells it.
    matched_name text not null,

    -- The list entity's own id, so a reviewer can look the entry up at source
    -- rather than taking our word for it.
    matched_entity_id text,

    -- Match STRENGTH, 0-100, not a boolean. Phase 5's whole point is that
    -- "Ahmed Hassan" against "Ahmad Hasan" is a matter of degree, and the
    -- threshold for what counts as a hit is a business decision, not a
    -- property of the data.
    match_score numeric(5, 2) not null
        constraint screening_results_score_in_range
        check (match_score >= 0 and match_score <= 100),

    -- The raw match record from the source, kept so a reviewer can see
    -- everything the list said, not just the fields we thought to model.
    payload jsonb not null default '{}'::jsonb,

    screened_at timestamptz not null default now()
);

-- The case view shows strongest matches first.
create index screening_results_application_idx
    on screening_results (application_id, match_score desc);
