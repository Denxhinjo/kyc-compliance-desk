-- 030: ruleset 2026-10-2. The matcher stops saying "confirmed".
--
-- MUST BE APPLIED BEFORE THE WORKER THAT STAMPS IT IS DEPLOYED.
-- decisions.risk_ruleset_version_at_decision carries a foreign key to
-- rulesets(version) -- asserted in tests/test_rulesets.py -- so a worker
-- stamping a version that has no row cannot record a decision at all.
--
-- THE REFERENCE DATA IS UNCHANGED, AND SAYS SO IN CODE.
--
-- The FATF lists did not move. Rather than copy a four-kilobyte blob and
-- invite it to drift, this selects 2026-10-1's own reference_data. If that row
-- is missing the insert affects zero rows and the migration's final check
-- fails loudly, which is the desired behaviour: this version is defined as
-- "2026-10-1's reference data, new scoring".

insert into rulesets
    (version, effective_from, points, thresholds, reference_data, change_note)
select
    '2026-10-2',
    '2026-10-17T00:00:00+02:00'::timestamptz,
    jsonb_build_object(
        'sanctions',      jsonb_build_object('exact', 60, 'strong', 45, 'partial', 20),
        'pep',            jsonb_build_object('exact', 30, 'strong', 15, 'partial', 5),
        'adverse_media',  jsonb_build_object('exact', 10, 'strong', 10, 'partial', 5),
        'country_call_for_action', 40,
        'country_increased_monitoring', 15,
        'document', r.points -> 'document',
        'document_missing', 30,
        'match_bands', jsonb_build_object(
            'exact_at', 100.0, 'strong_at', 80.0, 'partial_at', 0.0
        ),
        -- THE SWITCH. The scorer reads this; there is no feature flag. A row
        -- that names no definition means v1, which is how 2026-10-1 keeps its
        -- meaning without being edited (rulesets is append-only).
        'name_similarity', 'v2-symmetric-coverage',
        -- Recorded as part of the rules, because it changes outcomes.
        'sanctions_always_reviewed', true
    ),
    r.thresholds,
    r.reference_data,
    'The matcher stops saying "confirmed", and a sanctions hit stops being able to refuse anyone automatically.

WHAT WAS WRONG. Name similarity used max(token_set_ratio, ratio). token_set_ratio returns 100 whenever one name''s tokens are a SUBSET of the other''s, so an applicant''s name appearing inside a longer list name scored a perfect match. Measured on this system''s own queue, 28 pairs sat at 100% while being different names, among them "Mohammed Nasser" against "Abdal Nasser Sharif Mohammed Said JAMOOS" -- two parts of six -- and "Carlos Fernandez" against "Carlos Ariel FERNANDEZ CONCEPCION". Common partial names are the classic source of false positives in sanctions screening, and a reviewer reading 100% had been told the opposite of the truth.

THE NEW DEFINITION (name_similarity = v2-symmetric-coverage) charges for unmatched parts on BOTH sides: coverage is the harmonic mean of precision over the applicant''s parts and recall over the list entry''s, multiplied by how alike the aligned parts are. A score of 100 is reached if and only if the two normalised token multisets are equal, so a reordered name ("KIM Tong-Myo''ng" against "Tong-Myo''ng KIM") still scores 100 and no strict subset ever can. RETRIEVAL IS UNCHANGED: the old scorer still decides which candidates are worth recording, so nothing that surfaced before stops surfacing. Only how a surfaced candidate is scored and labelled has changed.

LABELS. "confirmed" and "probable" are gone. A match now carries a name strength -- exact, strong or partial -- and, separately, a comparison of each secondary identifier the snapshot publishes: agree, conflict, or not available. The system never outputs "confirmed"; "confirmed match" and "false positive" are reviewer outcomes. Points are unchanged in value and move onto the new bands: sanctions 60/45/20, PEP 30/15/5, adverse media 10/10/5. A match contradicted by an identifier scores at its weakest band (20 for sanctions) rather than stepping down one.

DATES OF BIRTH. Every date the publisher lists is now kept; 532 OFAC entries give more than one, and Hasan NASRALLAH gives four. Keeping only the first let the system announce a conflict with a date the list itself also publishes. A conflict now requires disagreement with every listed date. An imprecise value -- "circa 1951", "1950 to 1955", "Mar 1962", 260 values in the current snapshot -- can never produce a conflict, because a value like that cannot rule anybody out; it can still be shown to the reviewer verbatim.

SUFFIXES. "Jr" and "Sr" were discarded as noise, which made "Sergey Ivanov" and "Sergey IVANOV JR." an exact match. They are now compared as an identifier: Jr against Sr is a conflict, and a suffix on one side only caps name strength at strong.

A SANCTIONS HIT NOW ALWAYS GOES TO A HUMAN. This withdraws a rule 2026-10-1 stated deliberately. Its reasoning, recorded in scoring.py, read: "So ''confirmed'' is 60: enough to guarantee review, not enough to refuse anyone by itself. Reaching 80 needs a second, independent signal -- a failed document check, a call-for-action jurisdiction." That second route to automatic refusal is closed. A genuine sanctions match must be escalated and reported, not silently refused: quietly turning away a listed person loses the one case a compliance function exists to act on. And the arithmetic was backwards at the weak end -- of the 720 combinations of a recorded sanctions hit with every other signal, 481 reached the auto-reject threshold, the smallest being a hit the list''s own date of birth contradicts (20) plus a call-for-action country (40) plus an in-progress document check (20). That is an automatic refusal of someone the evidence suggests is a different person. Automatic approval was never reachable, since the weakest sanctions band is 20 and the threshold is "below 20", so only the rejection half needed closing.

NOT RESCORED. No existing decision is recalculated and no stored reason text is rewritten. Decisions taken under 2026-10-1 keep their scores, their labels and their snapshot reference, and the ruleset each names still resolves to the definition it was taken under. Thresholds (auto-approve below 20, auto-reject at or above 80) and the FATF reference data are unchanged from 2026-10-1.'
  from rulesets r
 where r.version = '2026-10-1';

-- Defined as "2026-10-1's reference data, new scoring", so its absence is a
-- failure rather than an empty insert.
do $$
begin
    if not exists (select 1 from rulesets where version = '2026-10-2') then
        raise exception
            'ruleset 2026-10-2 was not inserted: 2026-10-1 is missing, so the '
            'reference data it is defined against could not be copied';
    end if;
end
$$;
