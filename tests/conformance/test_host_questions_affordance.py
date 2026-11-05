from dataclasses import replace

import pytest
from conformance_kit import ModelTurn

from belle_agent.platforms.base import PlatformAffordances

ANSWER_EVERYTHING = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
"""

ELSEWHERE = (("post_message", {"text": "for other eyes", "chat_id": "C2"}),)


def _declare(platform, **fields):
    members = getattr(platform, "members", None)
    target = next(iter(members.values())) if members else platform
    target.affordances = replace(getattr(target, "affordances", None) or PlatformAffordances(), **fields)


async def _held(build_world, **fields):
    world = await build_world(ANSWER_EVERYTHING)
    try:
        world.platform_kit.widens(world.platform, "C1", "C2")
    except NotImplementedError as exc:
        pytest.skip(str(exc))
    asked = []
    ask = world.host.platform.ask

    async def recording(conversation, thread, question, **where):
        asked.append(question)
        return await ask(conversation, thread, question, **where)

    world.host.platform.ask = recording
    _declare(world.host.platform, **fields)
    world.kit.play(ModelTurn(final="I tried.", calls=ELSEWHERE))
    await world.send(world.message("post it over there", conversation="C1", thread="root"))
    return world, asked


async def test_a_platform_that_shows_no_question_is_not_asked_and_the_call_is_refused(build_world):
    world, asked = await _held(build_world, questions="absent")

    assert asked == [] and world.questions() == []
    assert world.posts("C2") == [] and [post.text for post in world.posts("C1")] == ["I tried."]


async def test_a_platform_that_may_show_one_is_asked(build_world):
    world, asked = await _held(build_world, questions="conditional")

    assert [question.tool for question in asked] == ["post_message"]
    assert world.posts("C2") == []
