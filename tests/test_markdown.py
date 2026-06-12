"""Tests for Markdown escaping of user-derived text."""
import bot


def test_escapes_all_specials():
    assert bot.escape_md("a*b_c[d`e") == "a\\*b\\_c\\[d\\`e"


def test_escapes_backslash_first():
    assert bot.escape_md("a\\b") == "a\\\\b"


def test_plain_text_unchanged():
    assert bot.escape_md("Central Library") == "Central Library"


def test_apostrophe_not_escaped():
    # Stop captions like "Prince George's Park" must pass through untouched.
    assert bot.escape_md("Prince George's Park") == "Prince George's Park"


def test_empty_and_none_safe():
    assert bot.escape_md("") == ""
    assert bot.escape_md(None) is None
