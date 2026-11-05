import dataclasses
from types import SimpleNamespace

import pytest

from belle_agent.platforms.base import PlatformAffordances, affordance_contradictions, affordances


class Bare:
    ephemeral_posts = False

    async def ask(self, *args, **kwargs):
        return ""


class Rich(Bare):
    ephemeral_posts = True
    cards_wait_for_a_post = True

    surfaces = SimpleNamespace(silent_card="keep", reply_parts_in_thread=False)

    async def recent_messages(self, conversation, limit, before, *, workspace=""):
        return []


def test_a_platform_that_declares_nothing_gets_what_its_members_say():
    assert affordances(Bare()) == PlatformAffordances(
        private_aside="absent", questions="conditional", history="absent", cards_wait_for_a_post=False,
        silent_card=None, reply_parts_in_thread=None,
    )
    assert affordances(Rich()) == PlatformAffordances(
        private_aside="present", questions="conditional", history="present", cards_wait_for_a_post=True,
        silent_card="keep", reply_parts_in_thread=False,
    )


def test_a_declared_table_gives_the_facts_no_member_states():
    class Declares(Rich):
        affordances = PlatformAffordances(questions="absent", silent_card="keep",
                                          reply_parts_in_thread=False)

    assert affordances(Declares()) == PlatformAffordances(
        private_aside="present", questions="absent", history="present",
        cards_wait_for_a_post=True, silent_card="keep", reply_parts_in_thread=False,
    )
    assert affordance_contradictions(Declares()) == []


def test_a_declaration_refines_a_private_aside_the_platform_has_to_conditional():
    table = PlatformAffordances(private_aside="conditional")

    assert affordances(SimpleNamespace(ephemeral_posts=True, affordances=table)).private_aside == "conditional"
    assert affordances(SimpleNamespace(ephemeral_posts=False, affordances=table)).private_aside == "absent"


def test_a_member_is_read_each_time_and_wins_over_a_declaration():
    platform = Rich()
    platform.affordances = PlatformAffordances(
        private_aside="present", history="present", cards_wait_for_a_post=True, silent_card="keep",
        reply_parts_in_thread=False,
    )
    platform.ephemeral_posts = False
    platform.cards_wait_for_a_post = False

    found = affordances(platform)

    assert (found.private_aside, found.cards_wait_for_a_post) == ("absent", False)
    assert affordance_contradictions(platform) == [
        "private_aside: present, but ephemeral_posts is False",
        "cards_wait_for_a_post: True, but the attribute is False",
    ]


def test_each_declared_fact_a_member_states_otherwise_is_named():
    class Says(Bare):
        affordances = PlatformAffordances(history="present", silent_card="delete", reply_parts_in_thread=True)
        surfaces = SimpleNamespace(silent_card="keep", reply_parts_in_thread=True)

    assert affordance_contradictions(Says()) == [
        "history: present, but recent_messages is absent",
        "silent_card: delete, but surfaces.silent_card is keep",
    ]
    assert affordance_contradictions(Bare()) == []


def test_live_surface_values_win_over_stale_declarations():
    class Says(Rich):
        affordances = PlatformAffordances(silent_card="delete", reply_parts_in_thread=True)

    found = affordances(Says())
    assert (found.silent_card, found.reply_parts_in_thread) == ("keep", False)


def test_the_table_cannot_be_changed():
    with pytest.raises(dataclasses.FrozenInstanceError):
        PlatformAffordances().history = "present"  # type: ignore[misc]


def test_a_missing_question_method_overrides_a_claim_and_names_the_contradiction():
    platform = SimpleNamespace(ephemeral_posts=False, affordances=PlatformAffordances(questions="present"))

    assert affordances(platform).questions == "absent"
    assert affordance_contradictions(platform) == ["questions: present, but ask is absent"]


def test_live_history_requires_a_method_even_when_a_flag_claims_support():
    platform = SimpleNamespace(ephemeral_posts=False, history_available=True)

    assert affordances(platform).history == "absent"
    assert affordances(platform).questions == "absent"
