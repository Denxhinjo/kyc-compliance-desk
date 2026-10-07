-- 025: the call-for-action list stops being unsourced.
--
-- DEPLOYMENT ORDER: not file order. See the header of
-- 020_freeze_risk_after_decision.sql and docs/deploy.md. This one is
-- independent of that sequence; it only inserts a row and can go at any point.
--
-- WHAT CHANGED, AND WHAT DID NOT
--
-- Ruleset 2026-09-2 recorded that its call-for-action list — IR, KP, MM — was
-- never fetched from the publisher and never cited to a statement. It said so
-- in its own reference data: sourcing "unsourced", a null publication date,
-- and HTTP 403 recorded as the reason the statement could not be read on
-- 2026-09-24.
--
-- A copy of the statement has now been placed in the repository and read from
-- disk. THE CODES DID NOT CHANGE. The statement lists exactly the DPRK, Iran
-- and Myanmar; the constant held exactly IR, KP and MM. They were compared,
-- not assumed to match — the constant being correct was the hypothesis, and it
-- survived.
--
-- WHY THAT IS STILL A NEW VERSION
--
-- Because nothing about 2026-09-2 may change. Its definition states that its
-- call-for-action list was unsourced, and 915 decisions were taken under
-- rulesets that say so. Editing that row to say the list was sourced all along
-- would be rewriting history to look better, which is the single thing the
-- append-only triggers on this table exist to prevent. So: a new version,
-- identical in scoring effect, different in what it can prove about itself.
--
-- WHAT THE HASH IS WORTH
--
-- The file is a BROWSER CAPTURE of the statement, printed to PDF — its own
-- footer carries the retrieval timestamp and its header carries the source URL.
-- The hash therefore proves "this is the artifact that was read". It does NOT
-- prove "these are FATF's canonical bytes": a capture carries navigation, a
-- timestamp and whatever markup the site served that minute, so two honest
-- captures of the same statement hash differently. reference_data records the
-- format as "pdf-of-saved-webpage" precisely so the field cannot be read as the
-- stronger claim. The publisher's own PDF would be stronger; this is what we
-- have.
--
-- A DISTINCTION THE SCORING FLATTENS
--
-- The statement draws two tiers. Counter-measures are called for on the DPRK
-- and Iran. For Myanmar it says, in terms, enhanced due diligence "and not
-- countermeasures". Our scoring has one set and one number, so a Myanmar
-- applicant attracts exactly what a DPRK applicant attracts.
--
-- That is recorded in reference_data.tiers and deliberately NOT fixed here.
-- Changing who scores what is a policy decision; this migration is a
-- provenance exercise, and mixing the two would make the version mean two
-- things at once. The limitation now lives in the data, where an auditor
-- reading a decision can find it.
--
-- A REVIEW DATE THE PUBLISHER ITSELF GAVE
--
-- The statement says that if Myanmar makes no further progress by October 2026,
-- the FATF will consider countermeasures. That is recorded as
-- next_review_expected, and it is the clearest available argument for pinning a
-- ruleset version onto every decision: this list may move within weeks of being
-- written down, and a decision taken today must still be explainable after it
-- does.

insert into rulesets
    (version, effective_from, points, thresholds, reference_data, change_note)
values (
    '2026-10-1',
    '2026-10-07T09:32:00+02:00'::timestamptz,
    '{"adverse_media": {"confirmed": 10, "probable": 10, "weak": 5}, "country_call_for_action": 40, "country_increased_monitoring": 15, "document": {"Abandoned": 30, "Approved": 0, "Awaiting User": 20, "Declined": 40, "Expired": 30, "In Progress": 20, "In Review": 10, "Kyc Expired": 30, "Resubmitted": 20}, "document_missing": 30, "match_bands": {"confirmed_at": 92.0, "probable_at": 85.0, "weak_at": 80.0}, "pep": {"confirmed": 30, "probable": 15, "weak": 5}, "sanctions": {"confirmed": 60, "probable": 45, "weak": 20}}'::jsonb,
    '{"auto_approve_below": 20, "auto_reject_at_or_above": 80}'::jsonb,
    '{"fatf_call_for_action": {"codes": ["IR", "KP", "MM"], "digest": "a7a319093b050cec1a1cf0db7b087a113acd2e512ffb35c39e96710a5cc3f74c", "flattening_note": "The statement calls for counter-measures on the DPRK and Iran, and for enhanced due diligence AND NOT counter-measures on Myanmar. Scoring flattens this: all three are one set worth COUNTRY_CALL_FOR_ACTION_POINTS, so a Myanmar applicant scores exactly what a DPRK applicant scores. Recorded, not fixed.", "next_review_expected": "2026-10", "next_review_note": "The statement says of Myanmar: ''If no further progress is made by October 2026, the FATF will consider countermeasures.'' This list may therefore move within weeks of being recorded, which is the case pinning a ruleset version onto each decision exists to survive.", "plenary": "17-19 June 2026", "provenance": "Complete set from the FATF statement ''High-Risk Jurisdictions subject to a Call for Action'', Paris, 19 June 2026, read on 2026-10-07 from the copy committed at data/fatf/call-for-action-june-2026-06-19.pdf. All three codes come from that one statement: the DPRK and Iran under the heading calling for countermeasures, Myanmar under the heading calling for enhanced due diligence. None is carried over from an earlier revision or from the constant this replaced.", "published_at": "2026-06-19", "retrieved_at": "2026-10-07", "source_file": "data/fatf/call-for-action-june-2026-06-19.pdf", "source_format": "pdf-of-saved-webpage", "source_sha256": "786f2ffa2e3677dc5f764f2d964589540e57a31fd52d915f43c30ae94c540137", "source_url": "https://www.fatf-gafi.org/en/publications/High-risk-and-other-monitored-jurisdictions/call-for-action-june-2026.html", "sourcing": "sourced", "tiers": {"countermeasures": ["KP", "IR"], "enhanced_due_diligence_only": ["MM"]}}, "fatf_increased_monitoring": {"codes": ["AO", "BA", "BG", "BO", "CD", "CI", "CM", "HT", "IQ", "KE", "KW", "LA", "LB", "MC", "NP", "PG", "SS", "SY", "VE", "VG", "VN", "YE"], "digest": "c8e4e1edab7aec194195b15d30a56fd1c1fb0123c674095d1f3a5156000aea02", "flattening_note": null, "next_review_expected": null, "next_review_note": null, "plenary": "June 2026", "provenance": "Complete set from the FATF statement ''Jurisdictions under Increased Monitoring'', Paris, 19 June 2026, retrieved from source_url on 2026-09-24. All 22 codes come from that one statement; none is carried over from an earlier one.", "published_at": "2026-06-19", "retrieved_at": "2026-09-24", "source_file": null, "source_format": null, "source_sha256": null, "source_url": "https://www.fatf-gafi.org/content/fatf-gafi/en/publications/High-risk-and-other-monitored-jurisdictions/increased-monitoring-june-2026.html", "sourcing": "sourced", "tiers": null}}'::jsonb,
    'The call-for-action list stops being unsourced. It is now the complete set from the FATF statement ''High-Risk Jurisdictions subject to a Call for Action'', Paris, 19 June 2026, from the plenary of 17-19 June 2026, read on 2026-10-07 from a copy committed at data/fatf/call-for-action-june-2026-06-19.pdf (sha256 786f2ffa2e3677dc5f764f2d964589540e57a31fd52d915f43c30ae94c540137). THE CODES DID NOT CHANGE: IR, KP and MM before and after, scoring identically, and the statement''s list was compared against the constant rather than assumed to match it. What changed is what can be proven about them, which is why this is a new version rather than an edit: 2026-09-2''s definition records that its call-for-action list was never fetched and never cited, and that has to stay true of the decisions taken under it. THE HASH IS OF A BROWSER CAPTURE, not of FATF''s own PDF. It proves ''this is the artifact that was read'' and not ''these are the publisher''s canonical bytes'': the capture carries navigation, a retrieval timestamp in its footer and whatever markup the site served that minute, so two honest captures of the same statement hash differently. A DISTINCTION WE FLATTEN: the statement calls for counter-measures on the DPRK and Iran, and for enhanced due diligence AND NOT counter-measures on Myanmar. Scoring has one set and one number, so a Myanmar applicant scores exactly what a DPRK applicant scores. Recorded in reference_data.tiers and deliberately not fixed, because changing who scores what is a policy decision and this was a provenance exercise. NEXT REVIEW EXPECTED 2026-10: the statement says that if Myanmar makes no further progress by October 2026 the FATF will consider countermeasures. This list may move within weeks of being recorded. Points, thresholds and match bands are unchanged from 2026-09-2.'
);
