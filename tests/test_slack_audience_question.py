"""The question before a Slack post that widens the audience, on the fake transport.

The rule and its sentences are ``SlackPostToolkit.widening_question``, which
the JiuwenSwarm channel asks through an interrupt rail. Here the host asks
it: the post is held, the person is asked in the conversation the turn came
from, and the post is sent once when they agree. What is generic of that is
in ``tests/conformance/test_conformance_held_calls.py``.
"""

import json

from conformance_kit import ModelTurn
from scripted_backend import ScriptedTurn
from slack_transport import BOT_USER, button, click_body
from test_slack_connector import ANSWER_EVERYTHING, ASKER, BYSTANDER, OTHER_ROOM, ROOM, TS, _message, build_world, slack  # noqa: F401
from test_slack_turn_surfaces import clock  # noqa: F401

from belle_agent.click_notices import WITHDRAWN_QUESTION_NOTE
from belle_agent.host import OUTCOME_POSTED_FINAL
from belle_agent.tools.host import APPROVE, AWAITING_CONFIRMATION, REJECT

ELSEWHERE = (("post_message", {"text": "for other eyes", "chat_id": OTHER_ROOM}),)


async def _asked(build_world, calls=ELSEWHERE):  # noqa: F811
    """A turn in ROOM tried to post into OTHER_ROOM, which BYSTANDER reads and ROOM's readers do not all read."""
    world = await build_world(ANSWER_EVERYTHING)
    web = world.platform.web
    web.members.update({ROOM: [ASKER, BOT_USER], OTHER_ROOM: [ASKER, BYSTANDER, BOT_USER]})
    world.kit.play(ModelTurn(final="I asked before posting there.", calls=calls))
    await world.platform.app.deliver(_message("post it over there"))
    await world.host.idle()
    question = next(payload for payload in web.payloads("chat_postMessage") if payload.get("blocks"))
    ts = next(sent_ts for _, payload, sent_ts in web.sent if payload is question)
    return world, web, question, ts


async def _click(world, question, ts, value: str, *, user: str = ASKER) -> str:
    body = click_body(user=user, channel=ROOM, message_ts=ts, payload=question)
    return await world.platform.app.click(body, button(question["blocks"], value=value))


def _posts(web, channel: str) -> list[str]:
    """The plain messages Slack accepted for one conversation."""
    return [payload["text"] for method, payload, _ in web.sent if method == "chat_postMessage" and payload["channel"] == channel and not payload.get("blocks")]


async def test_a_widening_post_is_put_to_the_conversation_it_comes_from_and_sent_once_on_approval(build_world):  # noqa: F811
    world, web, question, ts = await _asked(build_world)

    # The model was told at once that the post waits, and the turn ended with nothing sent elsewhere.
    waiting = json.loads(world.kit.results[0])
    assert waiting["error"] == AWAITING_CONFIRMATION and BYSTANDER in waiting["detail"]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL] and _posts(web, OTHER_ROOM) == []
    # The question is the toolkit's own sentence, asked where the turn came from, with two buttons.
    assert (question["channel"], question["thread_ts"]) == (ROOM, TS)
    assert BYSTANDER in question["text"] and OTHER_ROOM in question["text"]
    assert [json.loads(button(question["blocks"], index=place)["value"])["value"] for place in (0, 1)] == [APPROVE, REJECT]

    assert (await _click(world, question, ts, APPROVE)).startswith("answered")

    assert _posts(web, OTHER_ROOM) == ["for other eyes"]
    # The buttons come off the question, with who answered.
    closed = web.payload("chat_update")
    assert (closed["channel"], closed["ts"]) == (ROOM, ts) and f"<@{ASKER}>" in json.dumps(closed["blocks"])

    # A click on a stale copy of the message sends nothing more.
    assert await _click(world, question, ts, APPROVE) == "ignored:question-not-waiting"
    assert _posts(web, OTHER_ROOM) == ["for other eyes"]
    assert WITHDRAWN_QUESTION_NOTE in json.dumps(web.payloads("chat_update")[-1]["blocks"])


async def test_a_refused_widening_post_is_not_sent(build_world):  # noqa: F811
    world, web, question, ts = await _asked(build_world)

    assert (await _click(world, question, ts, REJECT, user=BYSTANDER)).startswith("answered")

    assert _posts(web, OTHER_ROOM) == []
    assert [payload["channel"] for payload in web.payloads("chat_postMessage")] == [ROOM, ROOM]


async def test_a_post_that_fails_after_approval_is_said_where_it_was_asked(build_world):  # noqa: F811
    world, web, question, ts = await _asked(build_world)
    web.fail = lambda method, payload: "channel_not_found" if method == "chat_postMessage" and payload.get("channel") == OTHER_ROOM else None

    assert (await _click(world, question, ts, APPROVE)).startswith("answered")

    assert _posts(web, OTHER_ROOM) == []
    assert "post_message was agreed to and then failed" in _posts(web, ROOM)[-1]


async def test_a_turn_with_a_held_call_ends_what_it_showed_as_a_finished_turn(build_world, clock):  # noqa: F811
    """The card and the streamed reply end when the turn ends. The held call is no part of either."""
    world = await build_world("platforms: {slack: {enable_streaming: true, activity_card: true}}\n" + ANSWER_EVERYTHING)
    web = world.platform.web
    web.members.update({ROOM: [ASKER, BOT_USER], OTHER_ROOM: [ASKER, BYSTANDER, BOT_USER]})
    world.kit.play(ScriptedTurn(final="I asked before posting there.\n", calls=ELSEWHERE, hold_before_final=True))

    await world.platform.app.deliver(_message("post it over there"))
    await world.kit.holding()
    await clock.advance(5)
    world.kit.release()
    await world.host.idle()

    question = next(payload for payload in web.payloads("chat_postMessage") if "actions" in json.dumps(payload.get("blocks") or []) and BYSTANDER in payload["text"])
    ts = next(sent_ts for _, payload, sent_ts in web.sent if payload is question)
    card = next(message for shown_ts, message in web.screen.items() if shown_ts != ts and message.get("blocks"))
    # The turn is over: its card is settled and has no stop button, and its reply is the final, whole.
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]
    assert card["blocks"][0]["title"] == "Turn finished" and "actions" not in json.dumps(card["blocks"])
    assert "I asked before posting there." in [text.strip() for text in web.shown(ROOM)] and web.streaming == set()
    assert _posts(web, OTHER_ROOM) == []
    settled = json.dumps(card["blocks"])

    # The answer comes after all of that. The post goes out, and the settled card is not touched again.
    assert (await _click(world, question, ts, APPROVE)).startswith("answered")
    assert _posts(web, OTHER_ROOM) == ["for other eyes"]
    assert json.dumps(card["blocks"]) == settled
