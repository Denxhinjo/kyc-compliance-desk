-- 012: storage for the verification SIMULATOR. Not part of the domain model.
--
-- This table belongs to the pretend vendor, not to us. It exists because the
-- sweeper can only be demonstrated if the vendor knows an outcome we were never
-- told — which means the simulator has to remember what happened when it
-- deliberately drops a webhook. Until now it was stateless, with the session
-- token carrying its own contents.
--
-- The honest trade-off: this puts demo scaffolding in the shared schema. The
-- alternative was a JSON file written by the web service and read by the
-- worker, which on Windows means two processes contending for one file lock —
-- worse, and more code. So: a table, named so it is obviously not ours,
-- documented as disposable, and deleted in one migration the day there is a
-- real vendor account.
--
-- Nothing in /web or /worker outside the mock-vendor paths may read this.

create table mock_vendor_sessions (
    session_id uuid primary key,

    -- OUR applications.id, as passed to the session-creation endpoint. Named
    -- vendor_data because that is what the vendor's API calls it.
    vendor_data text not null,

    -- The vendor's own status vocabulary: 'Not Started', 'In Progress',
    -- 'Approved', 'Declined', 'In Review', 'Abandoned', 'Expired',
    -- 'Kyc Expired', 'Resubmitted', 'Awaiting User'.
    status text not null default 'Not Started',

    -- Whether the simulator actually delivered a webhook for the current
    -- status. Setting this false is how a lost delivery is staged.
    webhook_delivered boolean not null default false,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index mock_vendor_sessions_vendor_data_idx
    on mock_vendor_sessions (vendor_data);

create trigger mock_vendor_sessions_set_updated_at
    before update on mock_vendor_sessions
    for each row execute function set_updated_at();
