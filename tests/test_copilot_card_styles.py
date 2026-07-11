"""
tests/test_copilot_card_styles.py
----------------------------------
Regression guard for the co-pilot risk card colours.

The risk card was the only card type with a hard-coded light background
(#fff5f4) while its body text, dismiss button, and source line kept the shared
dark-theme *light* colours (var(--text-dark)/var(--text-muted)). That put light
text on a near-white box — the risk message was effectively unreadable. The fix
makes the risk card use the dark surface like every other card type.
"""
import os

CSS_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "css", "style.css")


def _css():
    with open(CSS_PATH, encoding="utf-8") as f:
        return f.read()


def _rule(css, selector, span=400):
    """Return the chunk of CSS starting at `selector` (for coarse assertions)."""
    i = css.index(selector)
    return css[i:i + span]


def test_risk_card_uses_dark_surface_not_light_pink():
    """The risk card must render on the dark surface, not a light-pink box that
    made the shared light body text unreadable."""
    block = _rule(_css(), ".tc-card-risk,")
    assert "var(--surface)" in block
    assert "#fff5f4" not in block


def test_risk_card_label_is_readable_on_dark():
    """The 'Risk' label must not use the very dark red (#b71c1c) that had almost no
    contrast on the dark surface — it should be a lighter red."""
    block = _rule(_css(), ".tc-card-risk .tc-card-label")
    assert "#b71c1c" not in block
    assert "#ef5350" in block


def test_reply_card_fills_opaque_surface_not_translucent_tint():
    """The co-pilot reply card must fill an OPAQUE dark surface. The old
    translucent-only tint (rgba(0, 150, 136, 0.10)) composited over a light part
    of the panel, so the near-white answer text was invisible until selected."""
    block = _rule(_css(), ".tc-card-reply {")
    assert "var(--surface)" in block                       # opaque dark base
    assert "background: rgba(0, 150, 136, 0.10);" not in block  # not translucent-only
