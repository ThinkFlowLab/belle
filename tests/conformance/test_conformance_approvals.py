"""Approvals and operator questions through the platform. Every backend must pass.

A tool a scope gates with ``ask`` waits for a person; the decision is Belle's
and the wait is the backend's own server state, never a held tool call. A
question Belle invents pauses the turn until it is answered. Who may answer
follows the clicks rule.
"""

from conformance_kit import ModelTurn

from belle_agent.host import OUTCOME_POSTED_FINAL

ASK_TO_REACT = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
    permissions: {tools: {react_to_message: ask}}
"""

ANSWER_EVERYTHING = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
"""

LEAD_APPROVES = (
    ASK_TO_REACT
    + """  - match: {channel: loopback, chat: C1}
    clicks: {approve: {user: [U-LEAD]}}
"""
)

REACT = (("react_to_message", {"emoji": "eyes"}),)


def _reactions(world) -> list:
    return [action for action in world.platform.log if action.kind == "reaction"]


async def test_a_rejected_tool_leaves_no_side_effect(build_world):
    """A reject never runs the tool. Whether the turn then continues is the backend's own: the
    scripted backend skips the call and goes on; OpenCode's permission reject interrupts the turn.
    Both share the invariant checked here -- the side effect is absent -- and approve runs it."""
    world = await build_world(ASK_TO_REACT)
    world.kit.play(ModelTurn(final="done", calls=REACT))
    message = world.message("react please", conversation="C1", thread="root")
    await world.platform.inject(message)

    (shown,) = await world.wait_question(conversation="C1")
    assert (shown.conversation, shown.thread, shown.question.kind) == ("C1", "root", "approval")
    # The tool is named as each backend spells it (a built-in name, or ``belle_`` for an MCP tool).
    assert shown.question.tool.endswith("react_to_message")
    assert world.posts() == []  # nothing ran while the person had not answered

    await world.send(world.answer_to(shown, approved=False))

    assert _reactions(world) == []  # a reject is an absent side effect

    # Control: the same call, approved, runs. (A second question joins the first in the log.)
    world.kit.play(ModelTurn(final="done", calls=REACT))
    await world.platform.inject(world.message("react again", conversation="C1", thread="root"))
    approve = (await world.wait_question(conversation="C1", count=2))[-1]
    await world.send(world.answer_to(approve, approved=True))
    assert [action.name for action in _reactions(world)] == ["eyes"]


async def test_the_approval_prompt_appears_once_in_the_requesters_conversation(build_world):
    world = await build_world(ASK_TO_REACT)
    world.kit.play(ModelTurn(final="done", calls=REACT))
    await world.platform.inject(world.message("react", conversation="C1"))

    shown = await world.wait_question(conversation="C1")

    assert len(shown) == 1
    assert world.questions("C2") == []
    await world.send(world.answer_to(shown[0], approved=True))


async def test_only_an_allowed_person_releases_the_call(build_world):
    world = await build_world(LEAD_APPROVES)
    world.kit.play(ModelTurn(final="done", calls=REACT))
    await world.platform.inject(world.message("react", conversation="C1", sender="U1"))
    (shown,) = await world.wait_question(conversation="C1")

    refused = await world.platform.inject(world.answer_to(shown, sender="U1", approved=True))

    assert refused.startswith("refused:clicks.approve")
    assert _reactions(world) == []

    # Control: the person the rule names releases it.
    assert (await world.send(world.answer_to(shown, sender="U-LEAD", approved=True))).startswith("answered")
    assert [action.name for action in _reactions(world)] == ["eyes"]


async def test_an_invented_question_pauses_the_turn_and_its_answer_resumes_it(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    world.kit.play(ModelTurn(final="resumed", ask=("Proceed?", ("yes", "no"))))
    await world.platform.inject(world.message("do it", conversation="C1", thread="root"))

    (shown,) = await world.wait_question(conversation="C1")

    assert (shown.conversation, shown.question.kind) == ("C1", "question")
    assert world.posts() == []  # the turn made no progress to a final while the question was open

    await world.send(world.answer_to(shown, values=("yes",)))

    assert [post.text for post in world.posts("C1")] == ["resumed"]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]


async def test_turn_stop_withdraws_a_backend_question(build_world, monkeypatch):
    world = await build_world(ANSWER_EVERYTHING)
    withdrawn = []

    async def withdraw_question(question_id, *, workspace):
        withdrawn.append(question_id)

    platform = world.host._hosted[world.platform_kit.name].platform
    monkeypatch.setattr(platform, "withdraw_question", withdraw_question, raising=False)
    world.kit.play(ModelTurn(final="unused", ask=("Proceed?", ("yes", "no"))))
    message = world.message("ask", conversation="C1", thread="root")
    await world.platform.inject(message)
    (shown,) = await world.wait_question(conversation="C1")

    stopped = await world.platform.inject(world.stop(message))
    await world.host.idle()

    assert stopped.startswith("stopped")
    assert withdrawn == [shown.question.id]


async def test_a_person_the_platform_does_not_admit_answers_no_question(build_world):
    world = await build_world("platforms: {loopback: {allow_from: [U1]}}\n" + ASK_TO_REACT)
    world.kit.play(ModelTurn(final="done", calls=REACT))
    await world.platform.inject(world.message("react", conversation="C1", sender="U1"))
    (shown,) = await world.wait_question(conversation="C1")

    # Seeing a question does not grant the right to answer it.
    assert await world.platform.inject(world.answer_to(shown, sender="U9", approved=True)) == "refused:user-not-in-allow_from"
    assert _reactions(world) == []

    # Control: a person the platform admits releases the call.
    assert (await world.send(world.answer_to(shown, sender="U1", approved=True))).startswith("answered")
    assert [action.name for action in _reactions(world)] == ["eyes"]


async def test_with_no_approve_rule_anyone_in_the_conversation_may_answer(build_world):
    # What the host does today, held as it is. Whether only the requester
    # should answer where no clicks rule says who may is undecided.
    world = await build_world(ASK_TO_REACT)
    world.kit.play(ModelTurn(final="done", calls=REACT))
    await world.platform.inject(world.message("react", conversation="C1", sender="U1"))
    (shown,) = await world.wait_question(conversation="C1")

    assert (await world.send(world.answer_to(shown, sender="U-SOMEBODY-ELSE", approved=True))).startswith("answered")
    assert [action.name for action in _reactions(world)] == ["eyes"]

