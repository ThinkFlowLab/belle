"""What Discord can do that no other member of its connector states."""

from belle_agent.platforms.base import PlatformAffordances

#: The most characters one message holds, as Discord documents it ("up to 2000 characters"). Whether a
#: character is a code point or a UTF-16 unit is not stated; Belle counts code points.
MESSAGE_LIMIT = 2000

#: The most options one question offers: the options of one string select, as Discord limits it.
QUESTION_OPTIONS_MAX = 25

#: The model's Markdown is sent as Discord Markdown, in messages of at most ``MESSAGE_LIMIT`` characters.
#: Questions are components on any message the bot sends, answered over the gateway with no public address.
#: A turn that said nothing, or was stopped, deletes its card, which leaves no trace. The later messages of
#: a long reply with no thread go beside the first, as the parts of a long reply do.
DISCORD_AFFORDANCES = PlatformAffordances(
    questions="present", silent_card="delete", reply_parts_in_thread=False,
)
