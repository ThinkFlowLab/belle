"""Emoji characters map to shortcodes, with their skin tones apart."""

from belle_agent.emoji import glyph_shortcode


def test_a_character_gives_its_shortcode_and_each_skin_tone_on_it():
    assert glyph_shortcode("\N{THUMBS UP SIGN}\U0001f3fc") == ("+1", (3,))
    # A presentation selector names no other emoji.
    assert glyph_shortcode("\N{HEAVY BLACK HEART}️") == ("heart", ())


def test_an_unknown_character_has_no_shortcode():
    assert glyph_shortcode("\N{SNOWMAN}") is None
