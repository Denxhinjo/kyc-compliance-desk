"""Name normalisation.

The least glamorous file in the project and the one that moves the numbers
most. Measured on the same name pairs used in the matching tests, normalisation
lifted "Jose Munoz" against "José Muñoz" from 80 to 100 — from sitting inside
the ambiguous band, where it would have needed a human, to an unambiguous
match. No change of scorer or threshold bought anything comparable.

The reason is that similarity scorers compare characters. Every difference that
is an artefact of *writing* rather than of *identity* — an accent, a hyphen, an
apostrophe, a capital letter — costs real similarity and pushes a true match
toward a false negative. Removing those differences before scoring is not a
tweak, it is most of the work.
"""

from __future__ import annotations

import re
import unicodedata

#: Honorifics. No identifying information, but real characters, which is
#: enough to move a score by several points.
_HONORIFICS = frozenset(
    {
        "mr", "mrs", "ms", "miss", "dr", "prof", "sir", "dame", "lord", "lady",
        "hon", "rev", "sheikh", "sheikha", "hajji",
    }
)

#: Generational suffixes, which used to be discarded with the honorifics.
#:
#: THEY ARE NOT NOISE. "Jr" and "Sr" are the one piece of a written name whose
#: entire job is to distinguish two people who share every other part of it —
#: usually a father and a son living at the same address. Folding them away
#: made "Sergey Ivanov" and "Sergey IVANOV JR." normalise to the same string
#: and score as an exact match, which is the opposite of what the suffix is
#: for.
#:
#: They are still removed from the string used for NAME similarity, because a
#: name is the same name with or without one. They come back as a separate
#: identifier, compared like a date of birth: agree, conflict, or not
#: available. See compare_suffix().
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})

_NOISE_TOKENS = _HONORIFICS | _SUFFIXES

_NON_NAME = re.compile(r"[^a-z0-9\s]")
_WHITESPACE = re.compile(r"\s+")


def strip_accents(value: str) -> str:
    """Reduce accented characters to their base letters.

    NFKD splits a character into its base plus a combining mark — "ñ" becomes
    "n" followed by a combining tilde — and the marks are then dropped. NFKD
    rather than NFD so that compatibility forms are folded too: ligatures, and
    the full-width characters that turn up in data copied out of East Asian
    systems.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise_name(value: str) -> str:
    """Reduce a name to the form used for comparison.

    Deliberately lossy. The output is for matching only and is never shown to
    anyone or stored in place of the original: an officer must always see the
    name as it was actually written, both in the application and on the list.
    """
    folded = strip_accents(value).lower()

    # Punctuation becomes a space rather than nothing, so "Al-Sayed" and
    # "Al Sayed" converge on the same two tokens instead of "alsayed" and
    # "al sayed" — which would look LESS alike than the originals.
    # "O'Brien" -> "o brien" for the same reason.
    folded = _NON_NAME.sub(" ", folded)

    tokens = [t for t in _WHITESPACE.split(folded) if t and t not in _NOISE_TOKENS]
    return " ".join(tokens)


def name_tokens(value: str) -> frozenset[str]:
    """The distinct word-parts of a normalised name.

    Used to require at least one shared token before a pair is scored at all.
    Similarity scorers will happily return 60-something for two names with
    nothing in common, and on a list of twenty thousand entries that noise
    dominates everything else.
    """
    return frozenset(normalise_name(value).split())


def suffix_of(value: str) -> str | None:
    """The generational suffix written on a name, or None.

    Returns the normalised form ("jr", "iii"), never the spelling, so that
    "Jr.", "JR" and "jr" are one value. The caller shows the original.
    """
    folded = _NON_NAME.sub(" ", strip_accents(value).lower())
    found = [t for t in _WHITESPACE.split(folded) if t in _SUFFIXES]
    # Last one wins: "John Smith Jr" has one, and a name carrying two is
    # malformed rather than meaningfully both.
    return found[-1] if found else None
