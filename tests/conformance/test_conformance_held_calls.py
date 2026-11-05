"""A tool call a person must agree to first. Every backend and every platform must pass.

A platform's toolkit says, call by call, what a person must be asked before
the call runs: on Slack, that a post would reach people who cannot read the
conversation it comes from. The call is not kept open while a person
decides. It answers at once that it waits, the turn goes on and ends, and
the host makes the call once when somebody who may answer agrees.
"""

import json

import pytest
from conformance_kit import ModelTurn

from belle_agent.host import CONFIRMATION_TIMEOUT_SECONDS, OUTCOME_INTERRUPTED, OUTCOME_POSTED_FINAL

ANSWER_EVERYTHING = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
"""

LEAD_APPROVES = (
    ANSWER_EVERYTHING
    + """  - match: {channel: loopback, chat: C1}
    clicks: {approve: {user: [U-LEAD]}}
"""
)

ASK_TO_REACT = (
    ANSWER_EVERYTHING
    + """  - match: {channel: loopback}
    permissions: {tools: {react_to_message: ask}}
"""
)

ELSEWHERE = (("post_message", {"text": "for other eyes", "chat_id": "C2"}),)


async def _asked(build_world, settings: str = ANSWER_EVERYTHING, calls=ELSEWHERE, withdraw_question=None):
    """A world whose first turn tried to post from C1 into C2, and has ended with its question open."""
    world = await build_world(settings)
    if withdraw_question is not None:
        world.host._hosted[world.platform_kit.name].platform.withdraw_question = withdraw_question
    world.platform_kit.widens(world.platform, "C1", "C2")
    world.kit.play(ModelTurn(final="I asked first.", calls=calls))
    await world.send(world.message("post it over there", conversation="C1", thread="root"))
    return world


def _later(world, seconds: float) -> None:
    """Move the host's clock on. The wait for a person is the host's own, so its clock is what ages it."""
    now = world.host.clock
    world.host.clock = lambda: now() + seconds


async def test_a_call_a_person_must_agree_to_is_asked_about_and_made_once_when_they_agree(build_world):
    withdrawn = []

    async def withdraw_question(question_id, *, workspace):
        withdrawn.append(question_id)

    world = await _asked(build_world, withdraw_question=withdraw_question)

    # The turn is over, with nothing sent to the other conversation: no tool call is kept open for the answer.
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]
    assert world.posts("C2") == [] and [post.text for post in world.posts("C1")] == ["I asked first."]
    (shown,) = world.questions()
    assert (shown.conversation, shown.thread, shown.question.kind) == ("C1", "root", "approval")
    assert shown.question.tool == "post_message" and "C2" in shown.question.text
    assert withdrawn == []

    # Half an hour later, which no tool call of any backend is known to survive.
    _later(world, 1800)
    assert (await world.send(world.answer_to(shown, approved=True))).startswith("answered")
    assert [post.text for post in world.posts("C2")] == ["for other eyes"]

    # A second click finds nothing waiting, and nothing is sent twice.
    assert await world.send(world.answer_to(shown, approved=True)) == "ignored:question-not-waiting"
    assert [post.text for post in world.posts("C2")] == ["for other eyes"]

    # The conversation's next turn is told what was decided.
    await world.send(world.message("and?", conversation="C1", thread="root"))
    told = "\n".join(world.requests[-1].scope.system_additions)
    assert "post_message" in told and "U1 agreed" in told


async def test_a_held_partial_post_reports_the_parts_already_sent(build_world, monkeypatch):
    world = await _asked(build_world)
    (shown,) = world.questions()

    async def partial_post(_call, _turn):
        return json.dumps({
            "ok": False,
            "error": "forbidden",
            "chat_id": "C2",
            "message_id": "m1",
            "part_message_ids": ["m1", "m2"],
            "parts": 2,
            "thread_id": "root",
            "detail": "the next part was refused",
        })

    monkeypatch.setattr(world.host._tools, "run_confirmed", partial_post)

    assert (await world.send(world.answer_to(shown, approved=True))).startswith("answered")

    messages = [post.text for post in world.posts("C1")]
    assert messages[0] == "I asked first."
    assert "2 part(s) were sent to C2" in messages[-1]
    assert "message IDs: m1, m2" in messages[-1]
    assert "duplicate" in messages[-1]
    assert "Nothing was sent" not in messages[-1]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]
    assert world.posts("C2") == []

    await world.send(world.message("and?", conversation="C1", thread="root"))
    told = "\n".join(world.requests[-1].scope.system_additions)
    assert '"part_message_ids": ["m1", "m2"]' in told


@pytest.mark.parametrize("platform_kit", ["loopback"], indirect=True)
async def test_a_held_partial_post_warns_when_the_next_part_may_have_been_sent(build_world, monkeypatch):
    world = await _asked(build_world)
    (shown,) = world.questions()

    async def uncertain_partial_post(_call, _turn):
        return json.dumps({
            "ok": False,
            "error": "delivery_uncertain_do_not_retry",
            "chat_id": "C2",
            "message_id": "m1",
            "part_message_ids": ["m1"],
            "parts": 1,
            "part_placements": [{"chat_id": "C2", "message_id": "m1", "thread_id": "root"}],
        })

    monkeypatch.setattr(world.host._tools, "run_confirmed", uncertain_partial_post)

    assert (await world.send(world.answer_to(shown, approved=True))).startswith("answered")

    notice = world.posts("C1")[-1].text
    assert "1 part(s) were sent to C2" in notice
    assert "Sent message IDs: m1" in notice
    assert "may also have accepted the next part" in notice
    assert "Do not retry the whole call" in notice
    assert "Nothing was sent" not in notice


async def test_a_refusal_sends_nothing_and_the_next_turn_is_told(build_world):
    world = await _asked(build_world)
    (shown,) = world.questions()

    assert (await world.send(world.answer_to(shown, approved=False))).startswith("answered")

    assert world.posts("C2") == []
    await world.send(world.message("and?", conversation="C1", thread="root"))
    told = "\n".join(world.requests[-1].scope.system_additions)
    assert "U1 refused" in told and "nothing was sent" in told
    # Told once: the turn after that one carries no word of it.
    await world.send(world.message("again?", conversation="C1", thread="root"))
    assert "refused" not in "\n".join(world.requests[-1].scope.system_additions)


async def test_what_was_decided_is_told_to_the_next_turn_after_a_restart(build_world):
    world = await _asked(build_world)
    (shown,) = world.questions()
    assert (await world.send(world.answer_to(shown, approved=False))).startswith("answered")
    await world.host.stop()
    world.kit.restart()

    restarted = await build_world(ANSWER_EVERYTHING)
    # The handled message ids outlive the restart too, so the new messages take new ones.
    restarted._ids = world._ids
    await restarted.send(restarted.message("and?", conversation="C1", thread="root"))
    await restarted.send(restarted.message("again?", conversation="C1", thread="root"))

    assert ["U1 refused" in "\n".join(request.scope.system_additions) for request in restarted.requests] == [True, False]


async def test_an_answer_that_comes_too_late_sends_nothing(build_world):
    world = await _asked(build_world)
    (shown,) = world.questions()

    _later(world, CONFIRMATION_TIMEOUT_SECONDS + 1)

    assert await world.send(world.answer_to(shown, approved=True)) == "ignored:question-not-waiting"
    assert world.posts("C2") == []


async def test_who_may_agree_follows_the_clicks_rule(build_world):
    world = await _asked(build_world, LEAD_APPROVES)
    (shown,) = world.questions()

    refused = await world.send(world.answer_to(shown, sender="U1", approved=True))

    assert refused.startswith("refused:clicks.approve") and world.posts("C2") == []
    # Control: the person the rule names releases the same call.
    assert (await world.send(world.answer_to(shown, sender="U-LEAD", approved=True))).startswith("answered")
    assert [post.text for post in world.posts("C2")] == ["for other eyes"]


async def test_with_no_approve_rule_anyone_in_the_conversation_may_agree(build_world):
    # Held as it is, like the approval of a scoped tool.
    world = await _asked(build_world)
    (shown,) = world.questions()

    assert (await world.send(world.answer_to(shown, sender="U-SOMEBODY-ELSE", approved=True))).startswith("answered")
    assert [post.text for post in world.posts("C2")] == ["for other eyes"]


async def test_the_same_call_made_twice_is_asked_about_once(build_world):
    world = await _asked(build_world, calls=ELSEWHERE * 2)

    (shown,) = world.questions()
    await world.send(world.answer_to(shown, approved=True))
    assert [post.text for post in world.posts("C2")] == ["for other eyes"]


async def test_a_call_nobody_can_be_asked_about_is_refused(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    world.platform_kit.widens(world.platform, "C1", "C2")

    async def cannot_ask(conversation, thread, question, **where):
        raise NotImplementedError("this platform shows no question")

    world.host.platform.ask = cannot_ask
    world.kit.play(ModelTurn(final="It was refused.", calls=ELSEWHERE), ModelTurn(final="done", calls=(("post_message", {"text": "here"}),)))

    await world.send(world.message("post it over there", conversation="C1", thread="root"))

    assert world.posts("C2") == [] and world.questions() == []
    assert [post.text for post in world.posts("C1")] == ["It was refused."]
    # Control: the same tool posts where its turn came from, with nobody asked.
    await world.send(world.message("post it here", conversation="C1", thread="root"))
    assert [post.text for post in world.posts("C1")] == ["It was refused.", "here"]


async def test_a_stopped_turn_takes_its_held_call_with_it(build_world):
    world = await build_world(ASK_TO_REACT)
    world.platform_kit.widens(world.platform, "C1", "C2")
    # The second call waits on the approval of a scoped tool, which keeps the turn running after the first is held.
    world.kit.play(ModelTurn(final="done", calls=(*ELSEWHERE, ("react_to_message", {"emoji": "eyes"}))))
    message = world.message("post it over there", conversation="C1", thread="root")
    await world.platform.inject(message)
    held, _approval = await world.wait_question(count=2, conversation="C1")

    assert (await world.send(world.stop(message))).startswith("stopped")

    assert world.outcomes() == [OUTCOME_INTERRUPTED]
    assert await world.send(world.answer_to(held, approved=True)) == "ignored:question-not-waiting"
    assert world.posts("C2") == []


@pytest.mark.parametrize("kit", ["scripted"], indirect=True)
@pytest.mark.parametrize("platform_kit", ["loopback", "slack"], indirect=True)
@pytest.mark.parametrize("error", ["forbidden", "delivery_uncertain_do_not_retry"])
async def test_held_call_failure_notice_distinguishes_uncertain_delivery(build_world, monkeypatch, error):
    world = await _asked(build_world)
    (shown,) = world.questions()
    calls = []

    async def failed_call(call, turn):
        calls.append(call)
        return json.dumps({"ok": False, "error": error})

    monkeypatch.setattr(world.host._tools, "run_confirmed", failed_call)

    assert (await world.send(world.answer_to(shown, approved=True))).startswith("answered")
    notice = world.posts("C1")[-1].text
    assert error in notice
    if error == "delivery_uncertain_do_not_retry":
        assert "Delivery is uncertain" in notice and "Do not retry" in notice
        assert "Nothing was sent" not in notice
    else:
        assert "Nothing was sent" in notice
    assert await world.send(world.answer_to(shown, approved=True)) == "ignored:question-not-waiting"
    assert len(calls) == 1
