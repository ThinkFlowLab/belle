"""Name the OpenCode session and message of a conversation and a request.

OpenCode accepts a session id that starts with ``ses`` and a message id that
starts with ``msg_``, and a second create with a known session id answers
with that session. Both ids are therefore derived, and no map is stored: the
same conversation finds its session again after a restart of either process.
"""

import hashlib


def _digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def session_id(namespace: str, conversation: str) -> str:
    """Return the session one conversation runs in, for one deployment."""
    return f"ses_belle_{_digest(namespace, conversation)}"


def message_id(namespace: str, conversation: str, request_id: str) -> str:
    """Return the message id of one request. OpenCode runs one prompt for one id."""
    return f"msg_belle_{_digest(namespace, conversation, request_id)}"
