import logging
from types import SimpleNamespace

import pytest

from belle_agent.platforms.base import PlatformAffordances, affordances
from belle_agent.platforms.installations import Installations


def _member(**fields):
    return SimpleNamespace(
        name="fake", posting_tool="post", capabilities=None, public_chat_types=frozenset(),
        direct_chat_types=frozenset(), event_kinds={},
        ephemeral_posts=True, ask=lambda *args, **kwargs: None, **fields,
    )


def _port(*members):
    return Installations(
        {f"i{index}": member for index, member in enumerate(members)}, noun="server", where="servers",
        installation_of=lambda workspace: "", metadata_key="server", unknown_tool=lambda name, server: None,
        logger=logging.getLogger("test"),
    )


def test_the_port_adds_what_its_first_installation_declares_to_what_its_own_members_state():
    table = PlatformAffordances(private_aside="conditional", questions="present", silent_card="delete")
    port = _port(_member(affordances=table, cards_wait_for_a_post=True), _member())

    assert port.affordances is table
    assert affordances(port) == PlatformAffordances(
        private_aside="conditional", questions="present", history="absent", cards_wait_for_a_post=True,
        silent_card="delete",
    )


def test_a_member_of_the_port_changed_later_is_what_the_port_reports():
    port = _port(_member(affordances=PlatformAffordances(private_aside="conditional")), _member())

    port.ephemeral_posts = False
    port.cards_wait_for_a_post = True

    found = affordances(port)
    assert (found.private_aside, found.cards_wait_for_a_post) == ("absent", True)


def test_a_first_installation_that_declares_nothing_leaves_the_port_with_its_own_members():
    port = _port(_member(surfaces=SimpleNamespace(silent_card="keep", reply_parts_in_thread=False)), _member())

    assert port.affordances is None
    assert affordances(port) == PlatformAffordances(
        private_aside="present", history="absent", cards_wait_for_a_post=False,
    )


async def test_history_uses_the_addressed_installation():
    async def recent_messages(*args):
        return [{"text": "earlier"}]

    port = _port(_member(recent_messages=recent_messages), _member())
    port._serving = port._members
    port._installation_of = lambda workspace: workspace

    assert affordances(port).history == "present"
    assert await port.recent_messages("room", 2, "latest", workspace="i0") == [{"text": "earlier"}]
    assert affordances(port, workspace="i0").history == "present"
    assert affordances(port, workspace="i1").history == "absent"
    with pytest.raises(NotImplementedError, match="has no decision history"):
        await port.recent_messages("room", 2, "latest", workspace="i1")


def test_a_port_without_history_does_not_claim_it():
    assert affordances(_port(_member(), _member())).history == "absent"


def test_question_support_belongs_to_the_addressed_installation():
    port = _port(
        _member(affordances=PlatformAffordances(questions="present")),
        _member(affordances=PlatformAffordances(questions="absent")),
    )
    port._serving = port._members
    port._installation_of = lambda workspace: workspace

    assert affordances(port, workspace="i0").questions == "present"
    assert affordances(port, workspace="i1").questions == "absent"


def test_startup_diagnostics_name_the_installation_with_the_bad_declaration():
    from belle_agent.platforms.base import affordance_contradictions

    port = _port(_member(), _member(affordances=PlatformAffordances(history="present")))

    assert affordance_contradictions(port) == ["i1: history: present, but recent_messages is absent"]


def test_private_posts_and_card_policy_are_read_from_the_addressed_installation():
    first, second = _member(cards_wait_for_a_post=True), _member(cards_wait_for_a_post=False)
    second.ephemeral_posts = False
    port = _port(first, second)
    port._serving = port._members
    port._installation_of = lambda workspace: workspace

    assert affordances(port, workspace="i0").private_aside == "present"
    assert affordances(port, workspace="i1").private_aside == "absent"
    assert affordances(port, workspace="i0").cards_wait_for_a_post is True
    assert affordances(port, workspace="i1").cards_wait_for_a_post is False


def test_installations_forward_additional_posting_tools():
    assert _port(_member(posting_tools=("post", "upload"))).posting_tools == ("post", "upload")
    assert _port(_member()).posting_tools == ()
