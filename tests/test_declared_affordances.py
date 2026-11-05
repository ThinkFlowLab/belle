from types import SimpleNamespace

from belle_agent.platforms.base import PlatformAffordances, affordance_contradictions, affordances
from belle_agent.platforms.loopback.platform import LoopbackPlatform
from belle_agent.platforms.matrix.connector import MatrixPlatform
from belle_agent.platforms.matrix.surfaces import TurnSurfaces as MatrixSurfaces
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.surfaces import TurnSurfaces as MattermostSurfaces
from belle_agent.platforms.slack.connector import SlackPlatform
from belle_agent.platforms.slack.text import MAX_SLACK_TEXT_LENGTH, split_text


def _members(platform_class, surfaces_class=None):
    return SimpleNamespace(
        ephemeral_posts=platform_class.ephemeral_posts,
        ask=platform_class.ask,
        cards_wait_for_a_post=getattr(platform_class, "cards_wait_for_a_post", False),
        recent_messages=getattr(platform_class, "recent_messages", None),
        affordances=platform_class.affordances,
        **({"surfaces": surfaces_class} if surfaces_class else {}),
    )


def _declares_only_what_no_member_states(table):
    return (table.private_aside in (None, "conditional"), table.history, table.cards_wait_for_a_post) == (True, None, None)


def test_matrix():
    platform = _members(MatrixPlatform, MatrixSurfaces)

    assert _declares_only_what_no_member_states(MatrixPlatform.affordances)
    assert affordance_contradictions(platform) == []
    assert affordances(platform) == PlatformAffordances(
        private_aside="absent", questions="present", history="present",
        cards_wait_for_a_post=True, silent_card="keep", reply_parts_in_thread=False,
    )


def test_mattermost():
    platform = _members(MattermostPlatform, MattermostSurfaces)

    assert _declares_only_what_no_member_states(MattermostPlatform.affordances)
    assert affordance_contradictions(platform) == []
    assert affordances(platform) == PlatformAffordances(
        private_aside="conditional", questions="conditional", history="present", cards_wait_for_a_post=False,
        silent_card="delete", reply_parts_in_thread=True,
    )


def test_loopback():
    platform = _members(LoopbackPlatform)

    assert _declares_only_what_no_member_states(LoopbackPlatform.affordances)
    assert affordance_contradictions(platform) == []
    assert affordances(platform) == PlatformAffordances(
        private_aside="present", questions="present", history="absent", cards_wait_for_a_post=False,
    )


def test_slack():
    platform = _members(SlackPlatform)

    assert _declares_only_what_no_member_states(SlackPlatform.affordances)
    assert affordance_contradictions(platform) == []
    assert affordances(platform) == PlatformAffordances(
        private_aside="present", questions="present", history="present", cards_wait_for_a_post=False,
    )


def test_slack_splits_a_message_at_its_text_budget():
    budget = MAX_SLACK_TEXT_LENGTH
    text = "word " * budget

    found = split_text(text)

    assert len(found) > 1 and all(len(part) <= budget for part in found) and len(found[0]) > budget - len("word ")


def test_discord():
    from belle_agent.platforms.discord.connector import DiscordPlatform

    platform = _members(DiscordPlatform)

    assert _declares_only_what_no_member_states(DiscordPlatform.affordances)
    assert affordance_contradictions(platform) == []
    assert affordances(platform) == PlatformAffordances(
        private_aside="absent", questions="present", history="absent",
        cards_wait_for_a_post=False, silent_card="delete", reply_parts_in_thread=False,
    )
