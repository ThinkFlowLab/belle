"""Event builders shared by Slack channel tests."""

import time

from jiuwenswarm.common.schema.message import EventType, Message


def dm_event(*, channel_id: str, user: str, ts: str, text: str) -> dict[str, str]:
    return {
        "type": "message", "channel_type": "im", "channel": channel_id,
        "user": user, "text": text, "ts": ts,
    }


def reaction_coordinates(kwargs: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(kwargs.get("channel", "")),
        str(kwargs.get("timestamp", "")),
        str(kwargs.get("name", "")),
    )


def permission_question_event(
    request: Message, *, chat_id: str, question_id: str
) -> Message:
    """Build the permission question emitted during a Slack turn."""
    return Message(
        id=request.id,
        type="event",
        channel_id="slack",
        session_id=request.session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": EventType.CHAT_ASK_USER_QUESTION.value,
            "request_id": question_id,
            "source": "permission_interrupt",
            "questions": [
                {
                    "question": "Allow the subagent to write that file?",
                    "options": [
                        {"label": "Approve", "value": "approve"},
                        {"label": "Reject", "value": "reject"},
                    ],
                    "multi_select": False,
                }
            ],
        },
        event_type=EventType.CHAT_ASK_USER_QUESTION,
        metadata={"slack_channel_id": chat_id},
    )
