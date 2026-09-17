-- 009: the identity vendor is Didit, not Sumsub.
--
-- Sumsub turned out to require a business account, so there was no way to get
-- sandbox credentials for a portfolio demo. Didit gives self-serve sandbox
-- access, and — more usefully — publishes a webhook contract with a real
-- per-event id and a signed timestamp, which makes the idempotency and replay
-- story in this project concrete rather than theoretical.
--
-- 002_vendor_events.sql is not edited: it is applied, and its checksum is
-- recorded. Only the default changes. The `vendor` column existed precisely so
-- that this would be a one-line migration rather than a reshaping.

alter table vendor_events alter column vendor set default 'didit';

comment on column vendor_events.vendor_event_id is
    'The vendor''s own id for this message. For Didit this is the envelope''s '
    'event_id. Unique per vendor, which is what makes a redelivery harmless.';
