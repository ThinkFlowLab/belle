"""Emoji characters and the shortcodes chat platforms name them by."""

# Presentation selectors do not change an emoji name.
VARIATION_SELECTORS = frozenset({"\ufe0f", "\ufe0e"})

#: Each skin tone modifier and its Fitzpatrick number, which platforms write into a name in their own form.
SKIN_TONES: dict[str, int] = {
    "\U0001f3fb": 2,
    "\U0001f3fc": 3,
    "\U0001f3fd": 4,
    "\U0001f3fe": 5,
    "\U0001f3ff": 6,
}

#: Supported Unicode glyphs and their shortcodes, which Slack and Mattermost both accept.
GLYPH_SHORTCODES: dict[str, str] = {
    # The connector's own lifecycle marks.
    "\N{EYES}": "eyes",
    "\N{NO ENTRY SIGN}": "no_entry_sign",
    "\N{HOURGLASS WITH FLOWING SAND}": "hourglass_flowing_sand",
    "\N{HOURGLASS}": "hourglass",
    "\N{HEAVY CHECK MARK}": "heavy_check_mark",
    "\N{CROSS MARK}": "x",
    "\N{BLACK SQUARE FOR STOP}": "black_square_for_stop",
    "\N{HIGH VOLTAGE SIGN}": "zap",
    "\N{WHITE HEAVY CHECK MARK}": "white_check_mark",
    "\N{WHITE QUESTION MARK ORNAMENT}": "grey_question",
    # Agreement and its opposite.
    "\N{THUMBS UP SIGN}": "+1",
    "\N{THUMBS DOWN SIGN}": "-1",
    "\N{OK HAND SIGN}": "ok_hand",
    "\N{RAISED HAND}": "raised_hand",
    "\N{WAVING HAND SIGN}": "wave",
    "\N{CLAPPING HANDS SIGN}": "clap",
    "\N{PERSON RAISING BOTH HANDS IN CELEBRATION}": "raised_hands",
    "\N{HANDSHAKE}": "handshake",
    "\N{FLEXED BICEPS}": "muscle",
    "\N{PERSON WITH FOLDED HANDS}": "pray",
    # Done, and marking done.
    "\N{BALLOT BOX WITH CHECK}": "ballot_box_with_check",
    "\N{PARTY POPPER}": "tada",
    "\N{HUNDRED POINTS SYMBOL}": "100",
    "\N{FIRE}": "fire",
    "\N{ROCKET}": "rocket",
    "\N{DIRECT HIT}": "dart",
    "\N{WHITE MEDIUM STAR}": "star",
    "\N{GLOWING STAR}": "star2",
    # Attention, trouble and questions.
    "\N{WARNING SIGN}": "warning",
    "\N{BLACK QUESTION MARK ORNAMENT}": "question",
    "\N{HEAVY EXCLAMATION MARK SYMBOL}": "exclamation",
    "\N{OCTAGONAL SIGN}": "octagonal_sign",
    "\N{BUG}": "bug",
    "\N{ELECTRIC LIGHT BULB}": "bulb",
    "\N{LEFT-POINTING MAGNIFYING GLASS}": "mag",
    "\N{ALARM CLOCK}": "alarm_clock",
    "\N{LARGE RED CIRCLE}": "red_circle",
    "\N{LARGE GREEN CIRCLE}": "large_green_circle",
    "\N{LARGE YELLOW CIRCLE}": "large_yellow_circle",
    # Repetition and reference.
    "\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}": ("arrows_counterclockwise"),
    "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}": "repeat",
    "\N{PUSHPIN}": "pushpin",
    "\N{PAPERCLIP}": "paperclip",
    "\N{LINK SYMBOL}": "link",
    "\N{MEMO}": "memo",
    "\N{SPEECH BALLOON}": "speech_balloon",
    # The social tail.
    "\N{HEAVY BLACK HEART}": "heart",
    "\N{GRINNING FACE}": "grinning",
    "\N{SMILING FACE WITH OPEN MOUTH AND SMILING EYES}": "smile",
    "\N{SMILING FACE WITH OPEN MOUTH AND COLD SWEAT}": "sweat_smile",
    "\N{FACE WITH TEARS OF JOY}": "joy",
    "\N{SLIGHTLY SMILING FACE}": "slightly_smiling_face",
    "\N{SMILING FACE WITH SMILING EYES}": "blush",
    "\N{SMILING FACE WITH SUNGLASSES}": "sunglasses",
    "\N{CRYING FACE}": "cry",
    "\N{THINKING FACE}": "thinking_face",
    "\N{SHRUG}": "shrug",
    "\N{FACE WITH PARTY HORN AND PARTY HAT}": "partying_face",
    "\N{ROBOT FACE}": "robot_face",
    "\N{BRAIN}": "brain",
    "\N{OPEN BOOK}": "open_book",
}



def glyph_shortcode(text: str) -> tuple[str, tuple[int, ...]] | None:
    """The shortcode of an emoji character and the Fitzpatrick number of each skin tone on it, or ``None`` when unknown."""
    tones: list[int] = []
    base_chars: list[str] = []
    for char in text:
        if char in VARIATION_SELECTORS:
            continue
        tone = SKIN_TONES.get(char)
        if tone is not None:
            tones.append(tone)
            continue
        base_chars.append(char)
    name = GLYPH_SHORTCODES.get("".join(base_chars))
    return (name, tuple(tones)) if name else None
