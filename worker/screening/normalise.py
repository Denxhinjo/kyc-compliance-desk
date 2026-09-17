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

#: Honorifics and suffixes that carry no identifying information but do carry
#: characters, which is enough to move a score by several points.
_NOISE_TOKENS = frozenset(
    {
        "mr", "mrs", "ms", "miss", "dr", "prof", "sir", "dame", "lord", "lady",
        "hon", "rev", "sheikh", "sheikha", "hajji",
        "jr", "sr", "ii", "iii", "iv",
    }
)

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
