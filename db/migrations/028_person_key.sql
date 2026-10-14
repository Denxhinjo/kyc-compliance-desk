-- 028: a key that identifies repeat applications from the same person.
--
-- DISPLAY ONLY. This changes nothing about scoring, routing or decisions. It
-- exists so that a reviewer looking at a case can see "3 applications from this
-- person" with links to the others, instead of meeting three identical-looking
-- rows in the queue and having to guess whether the system double-processed
-- something.
--
-- That guess is the actual problem being solved. On 2026-09-23 the same person
-- submitted the public form five times in 47 minutes. Each submission was
-- handled exactly once -- five application ids, five vendor sessions, five
-- separate screenings, one decision each -- but the queue showed four rows with
-- the same name, same date of birth, same score and the same sanctions match,
-- with nothing to say they were five separate applications rather than one
-- processed five times. A visitor who notices that concludes the queue is
-- showing a duplicate-processing bug, which is precisely the failure this
-- project claims to prevent.
--
-- WHY A GENERATED COLUMN IN THE DATABASE
--
-- Two services read this. A rule held in TypeScript is one the Python worker
-- cannot see, and vice versa, and "same person" is exactly the kind of
-- definition that drifts into two slightly different implementations. Keeping
-- it as a stored generated column means both services get identical answers
-- for free, it is indexable, and nobody can compute it a second way by
-- accident.
--
-- EXACT MATCH ONLY. NO FUZZY MATCHING.
--
-- Normalised full name AND date of birth, both exactly equal. Nothing
-- approximate, no trigram similarity, no token alignment -- deliberately
-- unlike the sanctions matcher, which is allowed to be approximate because a
-- human then judges every hit it produces. Here the output is shown to a
-- reviewer as a statement of fact: "these are the same person". A false
-- positive would assert that two different applicants are one person, in the
-- case file, with no second check in front of it. So the rule errs the other
-- way: two records that genuinely are the same person but spell the name
-- differently are treated as different people and simply do not link. That is
-- the safe direction for a claim nobody reviews.
--
-- WHAT "NORMALISED" COVERS
--
--   case          lower(), Unicode-aware
--   diacritics    translate() over the Latin-1 Supplement, Latin Extended-A
--                 and Latin Extended Additional letters that decompose to a
--                 single ASCII letter, so "Jose" and "José" agree
--   whitespace    runs of whitespace collapsed to one space, ends trimmed
--
-- Punctuation is NOT stripped: "Petrov-Ivanoff" and "Petrov Ivanoff" remain
-- different people. Dropping a hyphen is a judgement about naming, not a
-- normalisation, and this key is not the place to make it.
--
-- Characters outside the mapped ranges normalise to themselves, so two names
-- differing only by an unmapped mark do not link. Fail-closed again: a missing
-- link is a reviewer seeing less than they could, a false link is a reviewer
-- being told something untrue.
--
-- The map below was generated rather than typed, by NFKD-decomposing each
-- letter in those ranges and keeping the ones whose base is a single ASCII
-- letter:
--
--   python -c "import unicodedata as u; print(''.join(c for cp in
--     list(range(0xC0,0x180))+list(range(0x1E00,0x1F00))
--     for c in [chr(cp)] if u.category(c).startswith('L')))"
--
-- plus, by hand, the Latin letters NFKD does NOT decompose. That gap is worth
-- naming because it is not obvious: NFKD splits a letter into a base plus a
-- combining mark, so it catches e-acute and u-umlaut, and does nothing at all
-- to a letter that IS its own base character. The stroked and barred letters
-- are separate letters in Unicode rather than decorated ones, so the generated
-- set missed every one of them and "Soren"/"Søren" did not link.
--
-- Added 1:1: o-stroke, l-stroke, d-stroke, eth, h-bar, t-bar, dotless i, eng,
-- b-bar, l-bar, z-stroke, e-stroke.
--
-- NOT handled, because translate() maps one character to one character and
-- these expand to two: ae-ligature -> "ae", oe-ligature -> "oe", eszett ->
-- "ss", thorn -> "th", ij-ligature -> "ij". A name differing only by one of
-- those does not link. That is the safe direction -- a missing link shows a
-- reviewer less than it could, a false one tells them something untrue -- and
-- closing it would mean a regexp_replace chain per character, which is a lot
-- of machinery for a case that has not arisen in synthetic data.
--
-- IMMUTABLE is required for a generated column, and is honest here: the
-- function consults no table, no locale-dependent formatting and no clock.
-- The date is formatted with extract() and lpad() rather than to_char() or a
-- ::text cast precisely because both of those depend on the DateStyle setting
-- and are therefore only STABLE -- using one would make this function a lie
-- and the generated column non-deterministic across sessions.

create function person_key(full_name text, date_of_birth date)
returns text
language sql
immutable
strict
parallel safe
as $$
    select btrim(
               regexp_replace(
                   translate(lower(full_name), 'àáâãäåçèéêëìíîïñòóôõöùúûüýÿāăąćĉċčďēĕėęěĝğġģĥĩīĭįĵķĺļľńņňōŏőŕŗřśŝşšţťũūŭůűųŵŷźżžſḁḃḅḇḉḋḍḏḑḓḕḗḙḛḝḟḡḣḥḧḩḫḭḯḱḳḵḷḹḻḽḿṁṃṅṇṉṋṍṏṑṓṕṗṙṛṝṟṡṣṥṧṩṫṭṯṱṳṵṷṹṻṽṿẁẃẅẇẉẋẍẏẑẓẕẖẗẘẙẛạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹøłđðħŧıŋƀƚƶɇ', 'aaaaaaceeeeiiiinooooouuuuyyaaaccccdeeeeegggghiiiijklllnnnooorrrssssttuuuuuuwyzzzsabbbcdddddeeeeefghhhhhiikkkllllmmmnnnnoooopprrrrsssssttttuuuuuvvwwwwwxxyzzzhtwysaaaaaaaaaaaaeeeeeeeeiioooooooooooouuuuuuuyyyyolddhtinblze'),
                   '\s+', ' ', 'g'
               )
           )
           || '|'
           || lpad(extract(year  from date_of_birth)::int::text, 4, '0') || '-'
           || lpad(extract(month from date_of_birth)::int::text, 2, '0') || '-'
           || lpad(extract(day   from date_of_birth)::int::text, 2, '0')
$$;

comment on function person_key(text, date) is
    'Normalised name plus date of birth, for linking repeat applications by '
    'the same person. Exact match only, display purposes only. See migration 028.';

-- STORED, not VIRTUAL: it is read on every queue render and every case page,
-- and it is indexed. Computing it per row per read would be wasted work.
alter table applications
    add column person_key text
    generated always as (person_key(full_name, date_of_birth)) stored;

comment on column applications.person_key is
    'Generated. Equal for two applications from the same person (exact '
    'normalised name and date of birth). Display only -- never an input to '
    'scoring, routing or decisions.';

-- The lookup is always "every other application with this key", so a plain
-- btree on the key is the whole access pattern. created_at is included so the
-- "and their timestamps" half of the display is answered from the index.
create index applications_person_key_idx
    on applications (person_key, created_at);

-- WHAT THIS DELIBERATELY DOES NOT DO
--
-- It does not make a repeat application a risk factor. Scoring does not read
-- this column and must not: "has applied before" changing a score is a policy
-- change that belongs in a new rules version, with the points written into the
-- rulesets table and every decision stamped with the version that applied it.
-- Recorded as a proposal in docs/decisions.md instead.
--
-- It does not merge, supersede or auto-decide anything. Five applications from
-- one person remain five applications, each with its own evidence and its own
-- decision, because that is what actually happened.
--
-- It does not deduplicate at submission time. Refusing a second application
-- from the same person is a product decision with real consequences -- a
-- legitimate second attempt after a failed verification is the common case --
-- and it is not one to take as a side effect of adding a display feature.
