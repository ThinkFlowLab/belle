"""Discord ids, and the check of every tool argument that holds one.

Every Discord id is a snowflake: a 64-bit number written in decimal. A channel id, a thread id, a user id,
a message id and a guild id look alike, so the check names the argument and its kind, and Discord says
whether the id exists. A channel or a thread is a ``chat`` and a message is a ``message``, in the words of
the tools.
"""

import re
from collections.abc import Awaitable, Callable

from belle_agent.tools.host import refusal
from belle_agent.tools.inputs import IdForm, checks_ids, id_error

#: Seventeen digits or more: the oldest snowflakes, from 2015, have 17; a 64-bit number has at most 20.
SNOWFLAKE_RE = re.compile(r"^[0-9]{17,20}$")
SNOWFLAKE_FORM = "17 to 20 digits, such as 1000000000000000116"
ID_FORMS: dict[str, IdForm] = {
    kind: (SNOWFLAKE_RE, SNOWFLAKE_FORM) for kind in ("chat", "user", "message", "file", "guild", "event", "sound")
}
#: The kind of Discord id each tool argument holds. An argument whose name ends in ``s`` holds a list of them.
ID_ARGUMENTS = {
    "chat_id": "chat",
    "chat_ids": "chat",
    "parent_channel_id": "chat",
    "thread_id": "chat",
    "message_id": "message",
    "source_message_id": "message",
    "reply_to_message_id": "message",
    "before_message_id": "message",
    "after_message_id": "message",
    "around_message_id": "message",
    "attachment_id": "file",
    "user_id": "user",
    "recipient_user_id": "user",
    "user_ids": "user",
    "file_id": "file",
    "guild_id": "guild",
    "event_id": "event",
    "sound_id": "sound",
}


def discord_id_error(arguments: dict[str, object]) -> tuple[str, str] | None:
    """Why an argument that holds a Discord id does not hold one, or ``None`` when each does."""
    return id_error(arguments, ID_ARGUMENTS, ID_FORMS, "Discord")


#: Refuse a call whose Discord id argument is malformed, before the tool checks anything else.
checks_discord_ids: Callable[[Callable[..., Awaitable[str]]], Callable[..., Awaitable[str]]] = checks_ids(ID_ARGUMENTS, ID_FORMS, "Discord", refusal)


def message_url(scope: str, chat_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{scope}/{chat_id}/{message_id}"
