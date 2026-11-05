"""What a platform's command surface asks of the host. Every backend and every platform must pass.

A command reads what runs and what waits, removes a waiting message, changes
a conversation's model, reads the backend's catalog, and starts a private turn
with what it requires. The checks ask the host what a command surface asks it.
How Slack turns a slash command into these is checked in
``tests/test_slack_connector_commands.py``.
"""

from dataclasses import replace

from conformance_kit import WORKSPACE, ModelTurn

from belle_agent import capability_catalog
from belle_agent.backend import CATALOG_MODELS, CATALOG_SKILLS, CATALOG_SUBAGENTS
from belle_agent.host.commands import SCOPE_FIELD_SKILLS_REQUIRED, SCOPE_FIELD_SUBAGENT_REQUIRED
from belle_agent.platforms.base import InboundDrop

ANSWER_EVERYTHING = """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all]}
"""

A_LEAD_REMOVES = """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all]}
    clicks: {queue_drop: {user: [U-LEAD]}, stop: {user: [U-LEAD]}}
"""


def _drop(world, message, *, sender: str | None = None) -> InboundDrop:
    """A person's request to remove a waiting message, its sender's unless another is named."""
    return InboundDrop(
        platform=world.platform_kit.name,
        workspace=WORKSPACE,
        conversation=message.conversation,
        message_id=message.message_id,
        sender=message.sender if sender is None else sender,
    )


async def _hand(world, record) -> str:
    """Hand the host a record as a command surface builds it, and wait for every turn it leads to.

    A platform kit delivers a record as its platform receives a message, which carries nothing a command adds.
    """
    outcome = await world.host.receive(record)
    await world.host.idle()
    return outcome


def _ids(turns) -> list[str]:
    return [turn.inbound.message_id for turn in turns]


async def _one_runs_and_two_wait(world):
    """A held turn in one thread of C1 with two messages behind it, each of another person."""
    world.kit.play(ModelTurn(final="first", hold=True))
    running = world.message("one", sender="U1", thread="t")
    second, third = world.message("two", sender="U2", thread="t"), world.message("three", sender="U3", thread="t")
    await world.platform.inject(running)
    await world.kit.holding()
    for waiting in (second, third):
        assert (await world.platform.inject(waiting)).startswith("queued")
    return running, second, third


async def test_what_runs_and_what_waits_is_read_per_conversation(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    running, second, third = await _one_runs_and_two_wait(world)

    assert _ids(world.host.running(WORKSPACE, "C1")) == [running.message_id]
    assert _ids(world.host.waiting(WORKSPACE, "C1")) == [second.message_id, third.message_id]
    # Another conversation, and the same one in another workspace, hold nothing.
    assert world.host.running(WORKSPACE, "C2") == world.host.waiting(WORKSPACE, "C2") == []
    assert world.host.running("W-OTHER", "C1") == world.host.waiting("W-OTHER", "C1") == []

    world.kit.release()
    await world.host.idle()
    assert world.host.running(WORKSPACE, "C1") == world.host.waiting(WORKSPACE, "C1") == []
    assert len(world.requests) == 3


async def test_a_waiting_message_removed_by_its_sender_never_runs(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    _, second, third = await _one_runs_and_two_wait(world)

    assert (await world.host.receive(_drop(world, second))).startswith("removed:")
    assert _ids(world.host.waiting(WORKSPACE, "C1")) == [third.message_id]
    # It is gone, so there is nothing left to remove. A message that runs is not one that waits.
    assert await world.host.receive(_drop(world, second)) == "ignored:no-message-waiting"

    world.kit.release()
    await world.host.idle()
    # Control: the message that was left waiting ran. The removed one did not.
    assert world.texts() == ["one", "three"]


async def test_another_persons_waiting_message_is_removed_only_by_whoever_a_rule_names(build_world):
    world = await build_world(A_LEAD_REMOVES)
    _, second, third = await _one_runs_and_two_wait(world)

    assert (await world.host.receive(_drop(world, second, sender="U3"))).startswith("refused:clicks.queue_drop")
    assert _ids(world.host.waiting(WORKSPACE, "C1")) == [second.message_id, third.message_id]
    assert (await world.host.receive(_drop(world, second, sender="U-LEAD"))).startswith("removed:")

    world.kit.release()
    await world.host.idle()
    assert world.texts() == ["one", "three"]


async def test_with_no_rule_only_its_sender_removes_a_waiting_message(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    _, second, _ = await _one_runs_and_two_wait(world)
    turn = world.host.waiting(WORKSPACE, "C1")[0]

    assert world.host.may_drop(turn, "U3")[0] is False and world.host.may_drop(turn, "U2")[0] is True
    assert await world.host.receive(_drop(world, second, sender="U3")) == "refused:clicks.queue_drop"

    world.kit.release()
    await world.host.idle()
    # Control: it was not removed, so it ran.
    assert world.texts() == ["one", "two", "three"]


async def test_a_command_surface_reads_the_rule_a_stop_is_held_to(build_world):
    world = await build_world(A_LEAD_REMOVES)
    world.kit.play(ModelTurn(final="never said", hold=True))
    message = world.message("one", sender="U1", thread="t")
    await world.platform.inject(message)
    await world.kit.holding()
    (turn,) = world.host.running(WORKSPACE, "C1")

    # What the host answers a picker is what it does with the stop itself.
    assert world.host.may_stop(turn, "U2")[0] is False
    assert (await world.platform.inject(world.stop(message, sender="U2"))).startswith("refused:clicks.stop")
    assert world.host.may_stop(turn, "U-LEAD")[0] is True and world.host.may_stop(turn, "U1")[0] is True
    assert (await world.platform.inject(world.stop(message))).startswith("stopped")
    world.kit.release()
    await world.host.idle()


async def test_the_catalog_lists_what_the_backend_declares(world, kit):
    listed = world.host.backend_capabilities.catalog
    catalog = await world.host.catalog()
    await world.send(world.direct("which model serves me"))

    assert catalog is not None and listed
    if CATALOG_MODELS in listed:
        # The list a models command shows is the models the backend is configured with and no other. The
        # first is the one that serves a turn no scope and no person chose a model for.
        assert catalog.models.names == (kit.models()[0], kit.other_model)
    if CATALOG_SKILLS in listed:
        # In the shape the core filters: every entry is usable where no scope restricts.
        assert len(capability_catalog.usable_skills(catalog.skills, None)) == len(catalog.skills) > 0
    if CATALOG_SUBAGENTS in listed:
        assert catalog.subagents and all(isinstance(name, str) and name for name in catalog.subagents)


async def test_the_model_chosen_for_a_conversation_serves_its_turns_until_the_choice_is_removed(world, kit):
    first = world.direct("one")
    chosen = (WORKSPACE, first.conversation)
    await world.send(first)
    world.host.model_choices[chosen] = kit.other_model
    await world.send(world.direct("two"))
    # Another conversation keeps the default.
    await world.send(world.direct("elsewhere", sender="U2"))
    del world.host.model_choices[chosen]
    await world.send(world.direct("three"))

    default, served, elsewhere, after = kit.models()
    assert served == kit.other_model
    assert elsewhere == after == default != kit.other_model


async def test_a_scopes_model_comes_before_the_one_chosen_for_the_conversation(build_world, kit):
    world = await build_world(
        f"""
scopes:
  - match: {{channel: loopback, chat: C1}}
    delivery: {{mode: [all]}}
    agent: {{model_name: {kit.other_model}}}
"""
    )
    await world.send(world.direct("which model is the default"))
    default = kit.models()[0]
    world.host.model_choices[(WORKSPACE, "C1")] = default
    await world.send(world.message("in the room"))

    assert kit.models()[1] == kit.other_model != default
    assert world.host.scoped_model(world.message("x")) == kit.other_model
    assert world.host.settled(world.direct("x")).model == ""


async def test_a_skill_required_of_one_turn_is_required_of_that_turn_only(world):
    assert SCOPE_FIELD_SKILLS_REQUIRED in world.host.backend_capabilities.scope_fields
    skill = (await world.host.catalog()).skills[0]["name"]

    assert (await _hand(world, replace(world.direct("use it"), skills_required=(skill,)))).startswith("dispatched")
    await world.send(world.direct("and now without"))

    assert [request.scope.skills_required for request in world.requests] == [(skill,), ()]
    assert world.outcomes() == ["posted-final", "posted-final"]


async def test_what_a_turn_cannot_be_required_to_have_is_refused_and_starts_nothing(build_world):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback, chat_type: dm}
    agent: {skills: {available: [only-this-one]}, subagents: [only-this-one]}
"""
    )
    catalog = await world.host.catalog()
    applied = world.host.backend_capabilities.scope_fields
    skill, subagent = catalog.skills[0]["name"], (catalog.subagents or ("any",))[0]

    # The scope of the place the turn would run in does not allow the skill.
    outcome = await _hand(world, replace(world.direct("use it"), skills_required=(skill,)))
    assert outcome == "refused:skill-not-available-here"
    # A subagent the backend cannot be told to use, or one the scope does not allow.
    outcome = await _hand(world, replace(world.direct("delegate it"), subagent_required=(subagent, "the task")))
    if SCOPE_FIELD_SUBAGENT_REQUIRED in applied:
        assert outcome == "refused:subagent-not-available-here"
    else:
        assert outcome == f"refused:backend-does-not-apply-{SCOPE_FIELD_SUBAGENT_REQUIRED}"
    assert world.requests == [] and world.platform.log == []

    # Control: the same person's plain message runs.
    assert (await world.send(world.direct("plain"))).startswith("dispatched")
    assert len(world.requests) == 1


async def test_a_side_conversation_shares_no_session_with_the_one_it_sits_in(world):
    await world.send(world.direct("the usual"))
    await _hand(world, replace(world.direct("asked from elsewhere"), side="q1"))
    await _hand(world, replace(world.direct("a follow-up"), side="q1"))
    await _hand(world, replace(world.direct("another question"), side="q2"))
    await world.send(world.direct("the usual again"))

    usual, side, follow_up, other, again = (request.conversation for request in world.requests)
    assert usual == again and side == follow_up
    assert len({usual, side, other}) == 3
    # Each answer goes to the person's own conversation, whichever session ran it.
    assert {post.conversation for post in world.posts()} == {"D-U1"}


async def test_a_rule_does_not_let_somebody_outside_the_allow_list_remove_or_stop(build_world):
    world = await build_world("platforms: {loopback: {allow_from: [U1, U2, U3]}}\n" + A_LEAD_REMOVES)
    running, second, _ = await _one_runs_and_two_wait(world)

    # The rule names U-LEAD, whom the allow list does not admit. A click rule only narrows.
    assert await world.host.receive(_drop(world, second, sender="U-LEAD")) == "refused:allow_from"
    assert await world.platform.inject(world.stop(running, sender="U-LEAD")) == "refused:allow_from"
    assert len(world.host.waiting(WORKSPACE, "C1")) == 2 and world.outcomes() == []

    world.kit.release()
    await world.host.idle()
    assert world.texts() == ["one", "two", "three"]
