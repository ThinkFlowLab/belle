from belle_agent.host.decisions import MessageDecisions
from belle_agent.platforms.base import PlatformAffordances, affordances


class Platform:
    ephemeral_posts = False

    def __init__(self):
        self.asked = []

    async def recent_messages(self, conversation, limit, before, *, workspace=""):
        self.asked.append((conversation, limit, before, workspace))
        return []


def _window(platform):
    return MessageDecisions(object(), {}, platform, forward=False)._gate("W1")._recent_messages


async def test_a_platform_with_recent_messages_gives_the_gate_a_window_of_its_workspace():
    platform = Platform()

    window = _window(platform)

    assert affordances(platform).history == "present"
    assert window is not None
    await window("C1", 5, "M9")
    assert platform.asked == [("C1", 5, "M9", "W1")]


def test_a_platform_without_the_method_gives_the_gate_no_window():
    class Bare:
        ephemeral_posts = False

    assert affordances(Bare()).history == "absent"
    assert _window(Bare()) is None


def test_a_declared_table_cannot_hide_the_method():
    platform = Platform()
    platform.affordances = PlatformAffordances(history="absent")

    assert _window(platform) is not None
