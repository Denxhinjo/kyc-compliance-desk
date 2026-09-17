-- 001: applications — the case file.
--
-- One row per person going through onboarding. Holds what the applicant told
-- us, where the case currently is, and the link to the identity vendor.

create table applications (
    -- A UUID rather than a sequential integer, because this id appears in URLs
    -- and is handed to the vendor. A sequential id would leak how many
    -- applicants exist and invite enumeration of other people's cases.
    -- gen_random_uuid() is built into Postgres 13+; no extension needed.
    id uuid primary key default gen_random_uuid(),

    -- Where the case currently is. The lifecycle, built out in Phase 4:
    --   started -> submitted -> checking -> screening -> decided
    -- The CHECK means a typo fails loudly instead of quietly creating a sixth
    -- status that nothing downstream knows how to handle.
    status text not null default 'started'
        constraint applications_status_valid
        check (status in ('started', 'submitted', 'checking', 'screening', 'decided')),

    -- What the applicant told us about themselves. Synthetic data only.
    full_name text not null
        constraint applications_full_name_not_blank
        check (length(trim(full_name)) > 0),
    date_of_birth date not null
        constraint applications_date_of_birth_plausible
        check (date_of_birth >= date '1900-01-01'),

    address_line1 text not null,
    address_line2 text,
    address_city text not null,
    address_postcode text not null,
    -- ISO 3166-1 alpha-2, e.g. 'GB', 'DE'. Country drives risk scoring in
    -- Phase 5, so it is worth constraining the shape now.
    address_country char(2) not null
        constraint applications_country_is_iso_alpha2
        check (address_country ~ '^[A-Z]{2}$'),

    -- The vendor's own id for this applicant. Null until Phase 3 creates the
    -- applicant at Sumsub. Unique so one vendor record maps to one case.
    vendor_applicant_id text unique,

    -- Points-based risk score, written by the worker in Phase 5.
    risk_score integer
        constraint applications_risk_score_in_range
        check (risk_score is null or risk_score between 0 and 1000),

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- The review desk (Phase 6) lists pending cases oldest first.
create index applications_status_created_idx on applications (status, created_at);

-- Keep updated_at honest without every caller having to remember it.
create function set_updated_at() returns trigger
language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

create trigger applications_set_updated_at
    before update on applications
    for each row execute function set_updated_at();
