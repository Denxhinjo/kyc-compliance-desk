-- 002: vendor_events — the postbox.
--
-- Every message the identity vendor sends us, stored verbatim before we
-- interpret any of it. The webhook endpoint (Phase 3) writes here and does
-- almost nothing else before returning 200.

create table vendor_events (
    id bigint generated always as identity primary key,

    -- Which vendor sent it. Only Sumsub today; naming the vendor now means a
    -- second one would not need the table reshaped.
    vendor text not null default 'sumsub',

    -- The vendor's own id for this message. Together with `vendor` this is the
    -- idempotency key: vendors resend messages routinely, and the unique
    -- constraint below turns a duplicate delivery into a harmless no-op
    -- instead of a second round of processing.
    vendor_event_id text not null,

    -- e.g. 'applicantReviewed'. The vendor's vocabulary, not ours.
    event_type text not null,

    -- Nullable on purpose: a message can arrive for an applicant we cannot
    -- match yet. Storing it unmatched is better than discarding evidence.
    application_id uuid references applications (id),

    -- Whether the signature check passed. Recorded rather than assumed, so a
    -- run of failures is visible instead of invisible.
    signature_verified boolean not null,

    -- The message is stored TWICE, deliberately:
    --   raw_body  the exact bytes received, which is what the vendor's
    --             signature was computed over
    --   payload   the parsed form, for querying
    -- jsonb is not byte-exact: Postgres reorders object keys, drops duplicate
    -- keys and normalises numbers. Fine for queries, fatal for re-verifying a
    -- signature. So: jsonb to query, text to prove.
    raw_body text not null,
    payload jsonb not null,

    received_at timestamptz not null default now(),

    constraint vendor_events_unique_per_vendor unique (vendor, vendor_event_id)
);

create index vendor_events_application_idx on vendor_events (application_id, received_at);
