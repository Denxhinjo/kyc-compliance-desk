-- 029: the secondary identifiers a list publishes, kept instead of discarded.
--
-- WHY
--
-- Until 2026-10-2 a screening match was labelled "confirmed" on name
-- similarity alone. In compliance a name match is a POTENTIAL match; whether
-- two records are the same person is judged against the identifiers either
-- side publishes, and the call is a human one. The system could not make that
-- comparison because it was not keeping the data to make it with.
--
-- OFAC SDN publishes, for its 7,516 individuals:
--
--     date of birth            99.1%   (kept, but only the first -- see below)
--     nationality              75.6%   discarded
--     place of birth           64.1%   discarded
--     passport / national ID   37.8%   discarded
--     citizenship              13.7%   discarded
--
-- THE MULTI-DATE COLUMN IS A BUG FIX, NOT A FEATURE.
--
-- 532 entries publish more than one date of birth. Hasan NASRALLAH carries
-- four: 31 Aug 1960, 31 Aug 1953, 31 Aug 1955 and 31 Aug 1958. The loader kept
-- the first and threw the rest away, so the system could announce a "conflict"
-- with a date the list ITSELF also publishes, and weaken a genuine match on
-- the strength of it. `dates_of_birth` keeps all of them; a conflict now
-- requires disagreement with every one.
--
-- NULLABLE, AND DELIBERATELY NOT BACKFILLED.
--
-- Existing snapshots keep NULL in these columns and the interface renders
-- "not available", which is the truthful answer: that snapshot was loaded by
-- a parser that never read these fields, and inventing values for it would be
-- reconstructing list data from memory. The identifiers arrive with the next
-- snapshot, loaded from the publisher, with its own hash and publication date.
--
-- WHAT THIS DOES NOT ADD
--
-- No ID-number column. OFAC's idList is overloaded -- its commonest idType
-- values are "Secondary sanctions risk:", "Gender", "Registration Number" and
-- "Organization Established Date", with Passport and National ID No. among
-- them -- so a column called id_numbers would quietly fill with people's
-- gender. If identity documents are ever compared, the types have to be
-- separated first, and a public application form must not be inviting real
-- passport numbers anyway.

alter table sanctions_entries
    -- Every date the publisher gives, in the order it gives them, verbatim.
    -- Text, not date: "circa 1951", "1950 to 1955" and "Mar 1962" are real
    -- values and parsing them into a date would mean choosing one meaning and
    -- hiding the choice. The comparison treats an imprecise value as
    -- incapable of contradicting anybody; see screening/identifiers.py.
    add column dates_of_birth text[] not null default '{}',
    add column nationalities  text[] not null default '{}',
    add column places_of_birth text[] not null default '{}',
    add column citizenships   text[] not null default '{}';

comment on column sanctions_entries.dates_of_birth is
    'Every date of birth the publisher lists, verbatim and in order. Replaces '
    'date_of_birth, which kept only the first and is retained so decisions '
    'taken under ruleset 2026-10-1 stay reproducible.';

comment on column sanctions_entries.nationalities is
    'Nationality as published. NOT the address country, which is where '
    'somebody lives rather than where they hold a passport.';
