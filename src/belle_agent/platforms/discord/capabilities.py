"""What scopes may match and set on Discord."""

from belle_agent.scopes import AXIS_CHAT_TYPE, PER_CONVERSATION, ChannelCapabilities

NAME = "discord"

#: The words for the kinds of conversation, from the channel types of the API. A message in a thread
#: takes the thread's word: a private thread has an audience of its own.
CHAT_TYPE_TEXT = "text"
CHAT_TYPE_ANNOUNCEMENT = "announcement"
CHAT_TYPE_FORUM = "forum"
CHAT_TYPE_VOICE = "voice"
CHAT_TYPE_STAGE = "stage"
CHAT_TYPE_THREAD = "thread"
CHAT_TYPE_PRIVATE_THREAD = "private_thread"
CHAT_TYPE_DM = "dm"
#: Each channel type of the API, by its number, as the word scopes match. A group DM (3) is absent:
#: a bot joins one only through a person's OAuth grant, which Belle does not use.
CHANNEL_TYPES = {
    0: CHAT_TYPE_TEXT,
    1: CHAT_TYPE_DM,
    2: CHAT_TYPE_VOICE,
    5: CHAT_TYPE_ANNOUNCEMENT,
    10: CHAT_TYPE_THREAD,
    11: CHAT_TYPE_THREAD,
    12: CHAT_TYPE_PRIVATE_THREAD,
    13: CHAT_TYPE_STAGE,
    15: CHAT_TYPE_FORUM,
    16: CHAT_TYPE_FORUM,
}
CHAT_TYPES = frozenset(CHANNEL_TYPES.values())

DISCORD_CAPABILITIES = ChannelCapabilities(
    channel=NAME,
    # Named bots qualify the guild or ``direct`` marker with their installation name.
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
