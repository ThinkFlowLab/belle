"""Recognize silence replies and their per-turn metadata."""

import re
from collections.abc import Mapping
from typing import Any

NO_REPLY_SENTINEL = "NO_REPLY"
REPLY_OPTIONAL_KEY = "belle_reply_optional"
SILENCE_UNSTORED_KEY = "belle_silence_unstored"

# Whitespace, NUL, and the zero-width characters seen in model replies.
_SENTINEL_INVISIBLE = "\u200b\ufeff\u2060"
_SENTINEL_CHARS = rf"\s\x00{_SENTINEL_INVISIBLE}"
_SENTINEL_PADDING = rf"[{_SENTINEL_CHARS}]*"
_SENTINEL_SEPARATOR = rf"[{_SENTINEL_CHARS}]+"
_NO_REPLY_RE = re.compile(
    rf"^{_SENTINEL_PADDING}[`\[\(\"']*{_SENTINEL_PADDING}"
    rf"{re.escape(NO_REPLY_SENTINEL)}"
    rf"(?:{_SENTINEL_SEPARATOR}{re.escape(NO_REPLY_SENTINEL)})*"
    rf"{_SENTINEL_PADDING}"
    rf"[`\]\)\"']*[.!]?{_SENTINEL_PADDING}$",
    re.IGNORECASE,
)


def is_no_reply_sentinel(content: str) -> bool:
    """Match only whole replies made of the silence token."""
    return bool(_NO_REPLY_RE.match(content or ""))


def silence_is_unstored(metadata: Mapping[str, Any] | None) -> bool:
    """Check for an explicit unstored-silence request."""
    return (
        isinstance(metadata, Mapping)
        and metadata.get(REPLY_OPTIONAL_KEY) is True
        and metadata.get(SILENCE_UNSTORED_KEY) is True
    )


# Keep the token code-owned so the instruction and matcher cannot disagree.
# This controls the form of silence; operator prompts decide when to use it.
NO_REPLY_INSTRUCTION = (
    f"If you choose silence, output exactly {NO_REPLY_SENTINEL} as your entire"
    " final response. It must contain only those eight ASCII characters,"
    " with no other text or formatting."
)


# The token as a model might spell it when it is not copying: the underscore
# written as a space or a hyphen, any case, anywhere in the reply. Bounded by
# word boundaries so "no_reply_handler" in a code block is not read as the model
# addressing the connector.
_SENTINEL_SPELLINGS_RE = re.compile(r"\bno[ _-]reply\b", re.IGNORECASE)

# The wordings a turn reaches for when it has no token to write. Anchored over
# the whole reply, and length-bounded: what is being detected is a reply that is
# nothing but a refusal to answer, not an answer that uses these words. The
# bracket forms are first because both observed failures were parentheticals.
_DECLINED_REPLY_PHRASES = "no reply|nothing to add|nothing to say|no comment"
_DECLINED_REPLY_SHAPE_RE = re.compile(
    rf"^[\s\[\(]*(?:{_DECLINED_REPLY_PHRASES})[^)\]]{{0,80}}[\s\)\]\.!]*$",
    re.IGNORECASE,
)


def looks_like_a_declined_reply(content: str) -> bool:
    """Detect possible silence replies for logging only."""
    text = (content or "").strip()
    if not text:
        return False
    if _SENTINEL_SPELLINGS_RE.search(text):
        return True
    return bool(_DECLINED_REPLY_SHAPE_RE.match(text))


def post_tool_instruction(posting_tool_name: str) -> str:
    """Tell the agent how a post-tool turn reaches readers."""
    return (
        "Your final response is not sent to anyone. The only way to reach a"
        f" reader is the {posting_tool_name} tool. Decide how many messages"
        " to send and where each one goes."
    )


__all__ = [
    "NO_REPLY_INSTRUCTION",
    "NO_REPLY_SENTINEL",
    "REPLY_OPTIONAL_KEY",
    "SILENCE_UNSTORED_KEY",
    "is_no_reply_sentinel",
    "looks_like_a_declined_reply",
    "post_tool_instruction",
    "silence_is_unstored",
]
