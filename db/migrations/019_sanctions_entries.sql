-- The sanctions list moves out of the worker's memory and into Postgres.
--
-- WHY
--
-- The worker held the whole list in RAM: 19,393 entries, 44,017 searchable
-- spellings, 86MB resident after boot. That is fine for a process that runs
-- for weeks and terrible for one that has to start, answer, and exit — and an
-- on-demand function is the latter. Loading 86MB per invocation is not a
-- slow function, it is a function that cannot exist.
--
-- TWO-STAGE MATCHING, which is how this is done at real volume:
--
--   1. Postgres narrows tens of thousands of candidates to a handful, using a
--      trigram index. Milliseconds, and no data leaves the database.
--   2. rapidfuzz scores that handful precisely, in Python, with exactly the
--      same code as before.
--
-- Stage 2 is unchanged, so the only way behaviour can drift is if stage 1
-- fails to shortlist something the old matcher would have caught. That is a
-- recall question, and it is measured rather than assumed — see
-- worker/tests/test_equivalence.py.
--
-- WHY TOKENS AND NOT WHOLE NAMES
--
-- The obvious design indexes the whole normalised spelling and shortlists on
-- similarity(spelling, query). Measured against pairs the old matcher ACCEPTS,
-- whole-string similarity falls as low as 0.471:
--
--   ahmed hassan / ahmad hasan                    0.471 whole   0.625 per-token
--   mohammed al rashid / mohammed al masri        0.500 whole   1.000 per-token
--   vladimir putin / vladimir vladimirovich putin 0.714 whole   1.000 per-token
--
-- Transliteration drift lives INSIDE words, so it damages a per-token
-- comparison far less than a whole-string one; and an extra middle name wrecks
-- whole-string similarity while leaving every other token identical. A
-- whole-string shortlist would therefore need a threshold near 0.4 across
-- 44,017 spellings — a huge candidate set that still risks misses.
--
-- Indexing tokens also mirrors what the matcher actually gates on. The Python
-- rule is "at least two name parts must correspond" (MIN_ALIGNED_TOKENS); this
-- schema lets that same rule run in SQL, against an index.

-- NOTE: no begin/commit in this file. The migration runner wraps each file in
-- its own transaction and tracks it with a savepoint; a commit in here ends
-- that transaction underneath it, and the runner then fails releasing a
-- savepoint that no longer exists — having already applied the DDL but not
-- recorded the migration. No other migration does it either.

-- Trigram similarity and, crucially, the GIN operator classes that make it
-- indexable. Without the index this is a sequential scan with a similarity()
-- call per row, which is the thing being avoided.
create extension if not exists pg_trgm;

-- --------------------------------------------------------------------------
-- One row per listed entity, per snapshot.
--
-- Per SNAPSHOT, not per entity: the same person appears in every version of
-- the list, and the whole point of sanctions_snapshots is that a decision can
-- be traced to the exact list content that informed it. Sharing entity rows
-- across snapshots would destroy that — a later correction to a name would
-- silently rewrite what an earlier screening had seen.
--
-- The cost is duplication across snapshots, which is the correct trade: this
-- is an audit record, and audit records are allowed to be redundant.
-- --------------------------------------------------------------------------
create table sanctions_entries (
    id              bigserial primary key,
    snapshot_id     bigint not null references sanctions_snapshots(id),
    entity_id       text   not null,
    name            text   not null,
    entity_type     text   not null default 'sanctions',
    list_name       text   not null default 'unknown',
    countries       text[] not null default '{}',
    -- Text, not date: lists publish partial dates ("1975", "1975-00-00") and
    -- coercing those to a real date would either fail or invent precision.
    date_of_birth   text,

    constraint sanctions_entries_unique unique (snapshot_id, entity_id)
);

create index sanctions_entries_snapshot on sanctions_entries (snapshot_id);

-- --------------------------------------------------------------------------
-- Every searchable spelling: the primary name and each published alias.
--
-- Aliases matter more than any threshold. "Mohammed Al-Sayed" against
-- "Muhammad Al Sayyid" scores 80 on string similarity alone, which no sane
-- threshold treats as confirmed — but if the list publishes both spellings,
-- the comparison becomes exact and the problem disappears. So they are rows,
-- searched on equal terms with the primary name.
-- --------------------------------------------------------------------------
create table sanctions_names (
    id          bigserial primary key,
    entry_id    bigint not null references sanctions_entries(id) on delete cascade,
    -- As published. This is what an officer is shown, and it is never replaced
    -- by the normalised form.
    spelling    text   not null,
    -- normalise_name(spelling): accents stripped, punctuation to spaces,
    -- honorifics dropped, lowercased. Matching happens on this.
    normalised  text   not null,
    is_primary  boolean not null default false
);

create index sanctions_names_entry on sanctions_names (entry_id);

-- --------------------------------------------------------------------------
-- One row per token per spelling. This is the table the index is really for.
--
-- "Ahmad Hasan" produces two rows. A query for "Ahmed Hassan" produces two
-- query tokens, each of which finds its partner by trigram similarity, and the
-- entry qualifies because TWO of them matched. That is MIN_ALIGNED_TOKENS,
-- expressed in SQL.
--
-- Single-token entries — 951 of them in the SDN list — can never reach two
-- aligned tokens and so can never qualify. They are still stored, because
-- dropping them would make the snapshot an incomplete record of the list, but
-- they cost nothing at query time.
-- --------------------------------------------------------------------------
create table sanctions_name_tokens (
    name_id  bigint not null references sanctions_names(id) on delete cascade,
    entry_id bigint not null references sanctions_entries(id) on delete cascade,
    token    text   not null
);

-- THE index. gin_trgm_ops is what makes `token % 'ahmed'` an index lookup
-- rather than a scan.
create index sanctions_name_tokens_trgm
    on sanctions_name_tokens using gin (token gin_trgm_ops);

-- Candidates are always resolved back to their entry, and the shortlist query
-- groups by it.
create index sanctions_name_tokens_entry on sanctions_name_tokens (entry_id);

-- --------------------------------------------------------------------------
-- Which snapshot's rows are actually loaded and searchable.
--
-- A snapshot exists as soon as a list version is registered; its entries are
-- inserted afterwards and the insert can fail halfway. Screening against a
-- half-loaded list would silently under-match — the worst failure mode this
-- system has, because it looks like a clean result.
--
-- So loading sets this only once every row is in, and the matcher refuses to
-- use a snapshot that does not have it.
-- --------------------------------------------------------------------------
alter table sanctions_snapshots
    add column entries_loaded_at timestamptz;

comment on column sanctions_snapshots.entries_loaded_at is
    'Set when every entry row for this snapshot has been written. Null means '
    'the load did not finish and this snapshot must not be screened against.';
