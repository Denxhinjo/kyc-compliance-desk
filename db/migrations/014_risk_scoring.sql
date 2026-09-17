-- 014: record the score AND the reasons that produced it.
--
-- A score of 65 with no explanation is useless to the compliance officer in
-- Phase 6 and indefensible to a regulator. Three columns make a decision
-- explainable months later, even after the rules have changed underneath it.

alter table applications
    -- Every signal that contributed: its code, its points, a sentence a human
    -- can read, and the evidence behind it. jsonb rather than a table because
    -- the shape is "whatever this rule needed to say" and the set of rules
    -- changes; a schema per signal type would be a migration per rule.
    add column risk_signals jsonb,

    add column risk_scored_at timestamptz,

    -- Which version of the rules produced this, so an old score can be
    -- reproduced rather than merely believed.
    --
    -- The THRESHOLDS in force are stored inside risk_signals alongside this.
    -- Without them a stored 65 becomes unexplainable the moment the review band
    -- moves: you would know the number and the reasons but no longer why it was
    -- routed the way it was. That is exactly the question an auditor asks.
    add column risk_ruleset_version text;

-- Screening is re-runnable, and at-least-once delivery means it WILL re-run.
--
-- One row per (application, source, list entity). A second screening of the
-- same applicant against the same list finds the same entities and inserts
-- nothing, so ON CONFLICT DO NOTHING makes the handler idempotent without
-- deleting and reinserting — which would destroy the history of what was found
-- and when.
--
-- Partial, because matched_entity_id is nullable: a source that does not
-- publish stable entity ids cannot be deduplicated this way, and NULLs would
-- otherwise all be distinct from each other anyway.
create unique index screening_results_one_per_entity
    on screening_results (application_id, source, matched_entity_id)
    where matched_entity_id is not null;
