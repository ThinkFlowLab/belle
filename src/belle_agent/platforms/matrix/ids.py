"""Matrix ids, and the check of every tool argument that holds one.

A room id starts with ``!``, an event id with ``$``, a user id with ``@`` and has a server
name after its first ``:``. A room or an event id of a recent room version has no server
name. A room is a ``chat`` and an event is a ``message``, in the words of the tools. A
``chat`` argument also takes a user id, which names that person's direct room.
"""

import re

from belle_agent.tools.host import refusal
from belle_agent.tools.inputs import IdForm, checks_ids, id_error

ROOM_RE = re.compile(r"\A![^\s:]+(:\S+)?\Z")
EVENT_RE = re.compile(r"\A\$[^\s:]+(:\S+)?\Z")
USER_RE = re.compile(r"\A@[^\s:]+:\S+\Z")
_ROOM = "! and an opaque id, such as !AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefg or !abc:example.org"
_USER = "@, a name, : and a server name, such as @alice:example.org"
ID_FORMS: dict[str, IdForm] = {
    "space": (ROOM_RE, _ROOM),
    "chat": (re.compile(f"{ROOM_RE.pattern}|{USER_RE.pattern}"), f"a room id ({_ROOM}) or a person's user id ({_USER})"),
    "message": (EVENT_RE, "$ and an opaque id, such as $AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefg"),
    "user": (USER_RE, _USER),
}
#: The kind of Matrix id each tool argument holds. An argument whose name ends in ``s`` holds a list of them.
ID_ARGUMENTS = {
    "chat_id": "chat",
    "space_id": "space",
    "message_id": "message",
    "event_id": "message",
    "reply_to_message_id": "message",
    "before_message_id": "message",
    "file_id": "message",
    "attachment_ref": "message",
    "user_id": "user",
}


def matrix_id_error(arguments: dict[str, object]) -> tuple[str, str] | None:
    """Why an argument that holds a Matrix id does not hold one, or ``None`` when each does."""
    return id_error(arguments, ID_ARGUMENTS, ID_FORMS, "Matrix")


#: Refuse a call whose Matrix id argument is malformed, before the tool checks anything else.
checks_matrix_ids = checks_ids(ID_ARGUMENTS, ID_FORMS, "Matrix", refusal)
