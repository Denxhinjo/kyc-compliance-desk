"""The web and the worker must name the same ruleset version.

The worker is the authority: `scoring.RULESET_VERSION` is what gets stamped
onto a decision. The web needs the same string to decide whether a case
carries an older vocabulary and needs the explanatory note, and it cannot
import Python, so the value is written twice.

Two places agreeing to hold one string is an arrangement that holds until
somebody changes one of them. The failure would be quiet and wrong in the
worst direction: every current case marked as "scored under an older ruleset",
or -- worse -- every genuinely old case silently presented as current, with
"confirmed" in its frozen reason text and nothing saying why.
"""

from __future__ import annotations

import pathlib
import re

from scoring import NAME_SIMILARITY, RULESET_VERSION

WEB = pathlib.Path(__file__).resolve().parent.parent.parent / "web" / "src"
RULESET_TS = WEB / "lib" / "ruleset.ts"


def test_the_web_names_the_same_version_as_the_worker():
    assert RULESET_TS.exists(), f"{RULESET_TS} is missing"
    source = RULESET_TS.read_text(encoding="utf-8")
    found = re.search(r'CURRENT_RULESET\s*=\s*"([^"]+)"', source)
    assert found, "CURRENT_RULESET is not a plain string literal any more"
    assert found.group(1) == RULESET_VERSION, (
        f"web says {found.group(1)!r}, worker stamps {RULESET_VERSION!r}"
    )


def test_the_landing_page_only_shows_cases_scored_under_the_current_version():
    """The front page must not advertise an older vocabulary.

    Reason text is frozen evidence and is never rewritten, so a case scored
    under 2026-10-1 still reads "...(confirmed)". True of that decision, and
    exactly the claim this project should not be putting on its front door.
    """
    hero = WEB / "app" / "hero-queue.tsx"
    source = hero.read_text(encoding="utf-8")
    assert "CURRENT_RULESET" in source, (
        "the hero no longer filters by ruleset version, so it can show a case "
        "whose stored wording uses the pre-2026-10-2 vocabulary"
    )
    assert "a.risk_ruleset_version = $1" in source


def test_the_case_page_explains_an_older_vocabulary_rather_than_hiding_it():
    case = WEB / "app" / "desk" / "[id]" / "page.tsx"
    source = case.read_text(encoding="utf-8")
    assert "CURRENT_RULESET" in source
    # It must link somewhere a reviewer can see what changed.
    assert "/desk/rulesets" in source


def test_the_similarity_definition_is_named_in_the_migration():
    """The recorded ruleset row must name the scorer, or the switch is a lie.

    Without this key the row would mean v1 by the absent-key rule, and the
    deployed worker would be scoring with v2 under a version that says
    otherwise.
    """
    migration = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "db" / "migrations" / "030_ruleset_2026_10_2.sql"
    )
    source = migration.read_text(encoding="utf-8")
    assert "'name_similarity', '" + NAME_SIMILARITY + "'" in source
    assert RULESET_VERSION in source
