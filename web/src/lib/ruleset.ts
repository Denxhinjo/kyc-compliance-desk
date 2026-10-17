/**
 * The ruleset version currently in force.
 *
 * WHY THIS IS DUPLICATED, AND WHAT STOPS IT DRIFTING
 *
 * The authority is the worker: `scoring.RULESET_VERSION` is what actually gets
 * stamped onto a decision. The web cannot import Python, so the value is
 * written twice — and two places agreeing to hold the same string is the
 * arrangement that holds right up until someone changes one of them.
 *
 * So it is asserted instead: tests/test_ruleset_version_agrees.py reads this
 * file and fails if the two differ. A test is cheap; a desk telling a reviewer
 * that a current case was scored under an older vocabulary is not.
 *
 * It is used to decide whether a case needs the "scored under an older
 * ruleset" note, and to keep the landing page's queue preview to cases whose
 * stored reason text uses today's vocabulary.
 */
export const CURRENT_RULESET = "2026-10-2";
