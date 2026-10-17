"""WCAG AA contrast, asserted against the tokens that actually ship.

WHY THIS IS A TEST AND NOT A SPREADSHEET

The palette was checked by hand when it was designed, against a list of hex
values pasted into a scratch script. That check passes forever, including
after someone edits tokens.css, because it was never reading tokens.css. This
project has a recurring defect -- a thing that looks like a check and is not
one -- and a contrast check on a palette nobody is using is exactly it.

So this parses web/src/styles/tokens.css, resolves var() indirection, and
asserts the pairs that actually appear on screen. Changing a colour in
tokens.css either keeps these ratios or fails here.

WHAT THE THRESHOLDS MEAN

  4.5:1  WCAG 2.1 AA, 1.4.3, for body text.
  3.0:1  WCAG 2.1 AA, 1.4.11, for the visual information needed to identify a
         UI component or its state.

Backgrounds are checked at the DARKEST surface the text lands on, not the
lightest: --text-dim cleared 4.5:1 on the page ground and on a card while
sitting at 4.40:1 inside an inset well, which is where the timeline payloads
and most metadata actually are.
"""

import pathlib
import re

import pytest

TOKENS = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "web" / "src" / "styles" / "tokens.css"
)


def _load() -> dict[str, str]:
    """Every token whose value resolves to a literal colour."""
    raw = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", TOKENS.read_text(encoding="utf-8")))

    def resolve(value: str, depth: int = 0) -> str:
        value = value.split("/*")[0].strip()
        reference = re.fullmatch(r"var\((--[\w-]+)\)", value)
        if reference and depth < 10:
            return resolve(raw[reference.group(1)], depth + 1)
        return value

    return {name: resolve(v) for name, v in raw.items() if resolve(v).startswith("#")}


COLOURS = _load()


def _linear(channel: float) -> float:
    channel /= 255
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(colour: str) -> float:
    colour = colour.lstrip("#")
    if len(colour) == 3:
        colour = "".join(c * 2 for c in colour)
    r, g, b = (int(colour[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def ratio(foreground: str, background: str) -> float:
    high = max(_luminance(COLOURS[foreground]), _luminance(COLOURS[background]))
    low = min(_luminance(COLOURS[foreground]), _luminance(COLOURS[background]))
    return (high + 0.05) / (low + 0.05)


# (foreground, background, minimum, what a reader is looking at)
TEXT_PAIRS = [
    ("--text", "--bg", 4.5, "body text on the page ground"),
    ("--text", "--surface", 4.5, "body text on a card"),
    ("--text", "--accent-bg", 4.5, "text in a selected queue row"),
    ("--text-muted", "--bg", 4.5, "secondary text on the ground"),
    ("--text-muted", "--surface", 4.5, "secondary text on a card"),
    ("--text-dim", "--bg", 4.5, "metadata on the ground"),
    ("--text-dim", "--surface", 4.5, "metadata on a card"),
    ("--text-dim", "--surface-raised", 4.5, "metadata inside an inset well"),
    ("--accent", "--surface", 4.5, "link text on a card"),
    ("--accent", "--bg", 4.5, "link text on the ground"),
    ("--accent", "--accent-bg", 4.5, "link inside an info note"),
    ("--accent-text-on", "--accent", 4.5, "label on a primary button"),
    ("--accent-text-on", "--accent-hover", 4.5, "label on a hovered button"),
    ("--ok-text", "--ok-bg", 4.5, "approved badge"),
    ("--bad-text", "--bad-bg", 4.5, "rejected badge"),
    ("--warn-text", "--warn-bg", 4.5, "warning notice"),
    ("--neutral-text", "--neutral-bg", 4.5, "neutral chip"),
    ("--risk-low-text", "--risk-low-bg", 4.5, "low risk score"),
    ("--risk-med-text", "--risk-med-bg", 4.5, "medium risk score"),
    ("--risk-high-text", "--risk-high-bg", 4.5, "high risk score"),
]

COMPONENT_PAIRS = [
    ("--border-control", "--surface", 3.0, "input border on a card"),
    ("--border-control", "--bg", 3.0, "control edge on the ground"),
    ("--accent", "--surface", 3.0, "focus ring"),
    # The approve and reject buttons are tinted, not filled: the fill is
    # 1.14:1 against the card, so the border is the only thing drawing the
    # button. These are the two irreversible actions in the interface.
    ("--ok-border-strong", "--surface", 3.0, "approve button edge"),
    ("--ok-border-strong", "--ok-bg", 3.0, "approve edge against its own fill"),
    ("--bad-border-strong", "--surface", 3.0, "reject button edge"),
    ("--bad-border-strong", "--bad-bg", 3.0, "reject edge against its own fill"),
]


@pytest.mark.parametrize(
    "foreground,background,minimum,description",
    TEXT_PAIRS,
    ids=[p[3] for p in TEXT_PAIRS],
)
def test_text_meets_aa(foreground, background, minimum, description):
    found = ratio(foreground, background)
    assert found >= minimum, (
        f"{description}: {foreground} on {background} is {found:.2f}:1, "
        f"below the {minimum}:1 WCAG AA minimum for text"
    )


@pytest.mark.parametrize(
    "foreground,background,minimum,description",
    COMPONENT_PAIRS,
    ids=[p[3] for p in COMPONENT_PAIRS],
)
def test_components_meet_aa(foreground, background, minimum, description):
    found = ratio(foreground, background)
    assert found >= minimum, (
        f"{description}: {foreground} on {background} is {found:.2f}:1, "
        f"below the {minimum}:1 required by WCAG 1.4.11"
    )


def test_invalid_input_is_not_less_visible_than_a_valid_one():
    """An error state must not make a field's outline harder to see.

    The soft --bad-border reached 1.97:1 on white against --border-control's
    3.28:1, so marking a field invalid actually weakened its boundary. A rule
    that fires correctly and makes things worse passes every test that only
    asks whether it fired.
    """
    invalid = ratio("--bad-border-strong", "--surface")
    normal = ratio("--border-control", "--surface")
    assert invalid >= normal, (
        f"the invalid-input border is {invalid:.2f}:1 but a normal one is "
        f"{normal:.2f}:1 -- marking a field invalid makes it less visible"
    )


def test_decorative_edges_are_exempt_deliberately():
    """The soft edges are decoration, and this records that as a judgement.

    --ok-border and friends sit at roughly 1.5:1 on their own tint. They are
    the left bar on a callout and the rule under the demo banner; what
    identifies either is the heading beside it, which is asserted above at
    6.3-8.2:1. The exemption is written down here so it stays a decision
    somebody made rather than a pair that fell off the list.
    """
    for edge, fill, heading in [
        ("--ok-border", "--ok-bg", "--ok-text"),
        ("--bad-border", "--bad-bg", "--bad-text"),
        ("--warn-border", "--warn-bg", "--warn-text"),
    ]:
        assert ratio(heading, fill) >= 4.5, (
            f"{heading} carries the meaning that {edge} is exempt from "
            f"carrying, so it must itself meet AA"
        )


def test_every_token_pair_names_a_token_that_exists():
    """Guards the check itself: a typo'd token name would skip a pair."""
    for foreground, background, _, _ in TEXT_PAIRS + COMPONENT_PAIRS:
        assert foreground in COLOURS, f"{foreground} is not a colour in tokens.css"
        assert background in COLOURS, f"{background} is not a colour in tokens.css"
