-- 013: give the SIMULATOR a controllable result timestamp.
--
-- Simulator scaffolding, like 012. Not part of the domain model.
--
-- mock_vendor_sessions.updated_at is maintained by a trigger, so it always
-- says "now" and cannot be used to stage a result the vendor reached earlier.
-- A separate column, written explicitly, is what lets the out-of-order tests
-- control the VENDOR's clock rather than ours — which matters, because the
-- recency guard compares vendor timestamps to vendor timestamps and would be
-- meaningless if the test could only move our own.

alter table mock_vendor_sessions
    add column result_at timestamptz not null default now();
