"""What scopes may match and set on Mattermost."""

from belle_agent.scopes import AXIS_CHAT_TYPE, PER_CONVERSATION, ChannelCapabilities

NAME = "mattermost"

#: Mattermost's channel types: open, private, direct and group.
CHAT_TYPE_OPEN = "O"
CHAT_TYPE_PRIVATE = "P"
CHAT_TYPE_DIRECT = "D"
CHAT_TYPE_GROUP = "G"
CHAT_TYPES = frozenset({CHAT_TYPE_OPEN, CHAT_TYPE_PRIVATE, CHAT_TYPE_DIRECT, CHAT_TYPE_GROUP})
#: The channels every member of a team may join.
PUBLIC_CHAT_TYPES = frozenset({CHAT_TYPE_OPEN})

MATTERMOST_CAPABILITIES = ChannelCapabilities(
    channel=NAME,
    # The workspace is the channel's team, or the word ``direct`` for a direct or group channel.
    axes=frozenset({"channel", "chat", "chat_type", "user", "workspace"}),
    sections={
        "delivery": frozenset({"mode", "prompt", "mid_turn", "session", "reply", "events"}),
        "agent": frozenset({"model_name", "expert", "subagents", "skills"}),
        "permissions": None,
        "clicks": frozenset({"approve", "stop", "queue_drop"}),
    },
    # A button keeps an ask an ask.
    attended=True,
    identity_keys=("user",),
    axis_values={AXIS_CHAT_TYPE: CHAT_TYPES},
    axis_restrictions={
        "delivery.session": PER_CONVERSATION,
        "delivery.reply": PER_CONVERSATION,
        "delivery.events": PER_CONVERSATION,
    },
)
