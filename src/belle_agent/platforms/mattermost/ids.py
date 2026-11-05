"""Mattermost ids, and the check of every tool argument that holds one.

Every Mattermost id has one form: 26 letters and digits. A channel id, a user
id, a post id, a file id and a team id therefore look alike, so the check
names the argument and its kind, and the server says whether the id exists.
A channel is a ``chat`` and a post is a ``message``, in the words of the tools.
"""

import re

from belle_agent.tools.host import refusal
from belle_agent.tools.inputs import IdForm, checks_ids, id_error

ID_RE = re.compile(r"\A[A-Za-z0-9]{26}\Z")
_FORM = "26 letters and digits, such as 4xp9fdt77pncbef59f4k1qe83o"
ID_FORMS: dict[str, IdForm] = {kind: (ID_RE, _FORM) for kind in ("chat", "user", "message", "file", "team", "bookmark")}
#: The kind of Mattermost id each tool argument holds. An argument whose name ends in ``s`` holds a list of them.
#: The kinds are the words of the tools, so an argument that several platforms take has one refusal code.
ID_ARGUMENTS = {
    "chat_id": "chat",
    "chat_ids": "chat",
    "message_id": "message",
    "reply_to_message_id": "message",
    "before_message_id": "message",
    "visible_only_to_user_id": "user",
    "user_id": "user",
    "user_ids": "user",
    "file_id": "file",
    "bookmark_id": "bookmark",
    "file_ids": "file",
    "attachment_ref": "file",
    "team_id": "team",
}


def mattermost_id_error(arguments: dict[str, object]) -> tuple[str, str] | None:
    """Why an argument that holds a Mattermost id does not hold one, or ``None`` when each does."""
    return id_error(arguments, ID_ARGUMENTS, ID_FORMS, "Mattermost")


#: Refuse a call whose Mattermost id argument is malformed, before the tool checks anything else.
checks_mattermost_ids = checks_ids(ID_ARGUMENTS, ID_FORMS, "Mattermost", refusal)
