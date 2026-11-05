"""Delete detached and attached sessions on the harness. The only callers of ``Backend.forget``."""

from belle_agent.backend import OP_FORGET, Backend, BackendCapabilities
from belle_agent.host.attached_sessions import AttachedSession
from belle_agent.scheduling.expiry import Blocked
from belle_agent.scheduling.records import DetachedSession


async def delete_owned_conversation(backend: Backend, capabilities: BackendCapabilities, session: DetachedSession) -> None:
    """Delete the conversation that ``session`` owns, on the harness that holds it.

    Raise :class:`Blocked` for a detached session of another backend, or when the backend cannot delete a conversation.
    """
    if session.backend != capabilities.backend:
        raise Blocked(f"the detached session's conversation is on backend {session.backend}, and Belle runs on {capabilities.backend}")
    if OP_FORGET not in capabilities.operations:
        raise Blocked(f"backend {capabilities.backend} cannot delete a conversation")
    await backend.forget(session.owned_conversation)


async def delete_attached_session(backend: Backend, capabilities: BackendCapabilities, record: AttachedSession) -> None:
    """Delete an idle attached session on the harness, under ``attached_sessions.expire_after_idle_hours``.

    Raise :class:`Blocked` for an attached session of another backend.
    """
    if record.backend != capabilities.backend:
        raise Blocked(f"the attached session is on backend {record.backend}, and Belle runs on {capabilities.backend}")
    await backend.forget(record.key)
