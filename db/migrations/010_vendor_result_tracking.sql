-- 010: remember what the vendor last told us, and when THEY say it happened.
--
-- Two guards protect an application from a late result overwriting a newer one,
-- and they answer different questions:
--
--   the state machine  -> is this transition LEGAL?    (ours, always available)
--   vendor_result_at   -> is this result NEWER?        (theirs, sometimes null)
--
-- Neither substitutes for the other. Transition rules alone cannot separate two
-- results that map to the same stage — a "Declined" and a corrected "Approved"
-- arriving out of order are both a legal move to 'screening', so the older one
-- would win and the regression would be invisible. Timestamps alone cannot
-- reject an illegal move: a newer message would happily take an application
-- from 'started' to 'decided' without ever being screened.

alter table applications
    -- The vendor's OWN timestamp for the most recent result we applied, never
    -- our clock. Comparing our observation time against their event time mixes
    -- two clocks, and the skew between them is exactly where this kind of guard
    -- silently stops working.
    --
    -- Nullable, and honestly so: Didit's webhook envelope carries an event
    -- timestamp, but their decision endpoint (GET /v3/session/{id}/decision/)
    -- exposes no result timestamp. So results we PULL leave this untouched and
    -- rely on the state machine alone — which is fine, because a pulled result
    -- is by construction the session's current state and cannot be stale.
    add column vendor_result_at timestamptz,

    -- The vendor's own status string, kept verbatim: 'Approved', 'In Review',
    -- 'Kyc Expired'. Deliberately NOT collapsed into our lifecycle, because the
    -- two say different things — "Approved" means the document check passed,
    -- not that we have approved the customer. Phase 5 needs the distinction to
    -- score on, and a reviewer needs to see what the vendor actually said.
    add column vendor_status text;

-- The sweeper's query: applications sitting in a waiting state longer than they
-- should. Partial, because the states it scans are a small minority of rows.
create index applications_awaiting_vendor_idx
    on applications (updated_at)
    where status in ('submitted', 'checking');
