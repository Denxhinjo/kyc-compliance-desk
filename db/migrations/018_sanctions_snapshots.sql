-- 018: record WHICH sanctions list screened each applicant.
--
-- THE QUESTION THIS EXISTS TO ANSWER
--
-- "Was this person screened against the list as it stood on the day you
-- approved them?"
--
-- That is what an auditor actually asks, and until now it was unanswerable.
-- screening_results.source said 'ofac'. It did not say WHICH ofac — no
-- publication date, no version, no way to tell a screening run against
-- Tuesday's list from one against a list three weeks stale.
--
-- The inconsistency is what makes it worth fixing. This project versions the
-- scoring ruleset carefully: every scored application stores
-- risk_ruleset_version and the thresholds in force, precisely so a decision can
-- be reconstructed after the rules change. The list is half the input to that
-- decision and carried no version at all.
--
-- WHAT THIS DOES NOT FIX
--
-- Staleness. A worker still loads the list once per process and never refreshes
-- it, so a long-running worker screens against the list as it stood when it
-- started. This migration makes that visible — a snapshot row with an old
-- publication date is now readable evidence — but visible is not fixed. The
-- README keeps staleness in its limitations section for that reason.

create table sanctions_snapshots (
    id bigint generated always as identity primary key,

    -- 'ofac' | 'synthetic' | 'opensanctions'. Matches screening_results.source.
    source text not null
        constraint sanctions_snapshots_source_not_blank
        check (length(trim(source)) > 0),

    -- The publisher's own version, where they publish one. OFAC's SDN.XML
    -- carries a Publish_Date; the downloader used to parse it and throw it
    -- away.
    --
    -- Nullable, honestly: the synthetic fixture is fabricated and has no
    -- publisher, so it has no publication date. Inventing one would make a
    -- made-up list look like a dated authority, which is the opposite of what
    -- this table is for.
    published_at date,

    -- sha256 of the data file the worker actually loaded.
    --
    -- The file rather than the parsed entries, so anyone holding the same file
    -- can recompute this and confirm. It is the identity of the list: two runs
    -- with the same hash screened against exactly the same content, whatever
    -- either one called it.
    content_hash text not null
        constraint sanctions_snapshots_hash_shape
        check (content_hash ~ '^[0-9a-f]{64}$'),

    -- How many entities the file contained. Not a substitute for the hash —
    -- two different lists can have the same count — but it is the number a
    -- human reads first, and a sudden drop is how you notice a truncated
    -- download.
    record_count integer not null
        constraint sanctions_snapshots_count_non_negative check (record_count >= 0),

    -- When we first loaded this content. Distinct from published_at: the gap
    -- between the two IS the staleness, and having both means it can be
    -- measured rather than guessed.
    downloaded_at timestamptz not null default now(),

    -- Identical content is the same snapshot however many times it is loaded.
    -- Restarting a worker must not create a new row and make it look as though
    -- the list changed.
    constraint sanctions_snapshots_unique_content unique (source, content_hash)
);

comment on table sanctions_snapshots is
    'One row per distinct version of a sanctions list that has been screened '
    'against. Lets a decision be traced back to the exact list content that '
    'informed it.';

create index sanctions_snapshots_source_idx
    on sanctions_snapshots (source, downloaded_at desc);

-- The link. Every match now knows which list version produced it.
--
-- Nullable, and deliberately not backfilled. Rows written before this migration
-- were screened against a list whose version we genuinely do not know, and
-- filling in a plausible value would fabricate exactly the provenance this
-- table exists to establish. A NULL here reads as "unknown", which is true.
alter table screening_results
    add column snapshot_id bigint references sanctions_snapshots (id);

create index screening_results_snapshot_idx
    on screening_results (snapshot_id);

comment on column screening_results.snapshot_id is
    'The sanctions list version this match came from. NULL for rows written '
    'before migration 018, where the version is genuinely unknown.';
