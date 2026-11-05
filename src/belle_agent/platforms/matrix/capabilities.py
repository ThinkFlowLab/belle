"""What scopes may match and set on Matrix, as far as the platform is built."""

from belle_agent.scopes import AXIS_CHAT_TYPE, PER_CONVERSATION, ChannelCapabilities

NAME = "matrix"

#: A room in the account's ``m.direct``, a room whose join rule is public, and any other room.
CHAT_TYPE_DIRECT = "direct"
CHAT_TYPE_PRIVATE = "private"
CHAT_TYPE_PUBLIC = "public"
CHAT_TYPES = frozenset({CHAT_TYPE_DIRECT, CHAT_TYPE_PRIVATE, CHAT_TYPE_PUBLIC})
PUBLIC_CHAT_TYPES = frozenset({CHAT_TYPE_PUBLIC})

MATRIX_CAPABILITIES = ChannelCapabilities(
    channel=NAME,
    axes=frozenset({"channel", "chat", "chat_type", "user", "workspace"}),
    sections={
        "delivery": frozenset({"mode", "prompt", "mid_turn", "session", "reply", "events"}),
        "agent": frozenset({"model_name", "expert", "subagents", "skills"}),
        "permissions": None,
        # Who may answer a question with a vote on its poll, and stop a turn or remove a waiting message with ⏹️.
        "clicks": frozenset({"approve", "stop", "queue_drop"}),
    },
    # A poll keeps an ask an ask.
    attended=True,
    identity_keys=("user",),
    axis_values={AXIS_CHAT_TYPE: CHAT_TYPES},
    axis_restrictions={
        "delivery.session": PER_CONVERSATION,
        "delivery.reply": PER_CONVERSATION,
        "delivery.events": PER_CONVERSATION,
    },
)
