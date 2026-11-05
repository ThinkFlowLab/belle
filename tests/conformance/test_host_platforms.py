"""One host serves several platforms, and every action of a record goes back to the platform it came from.

Two loopback platforms under two names stand in for any two. ``test_conformance_two_platforms.py``
runs the same checks on two different platforms built from a configuration.
"""

import asyncio

import pytest
from conformance_kit import ModelTurn, RecordedBackend
from scripted_backend import ScriptedBackend

from belle_agent.admission.dedup import EventDedupStore
from belle_agent.host import ConversationHost, HostedPlatform
from belle_agent.platforms.base import MARK_DONE, MARK_RECEIVED, TRIGGER_DM, InboundAnswer, InboundMessage, InboundStop
from belle_agent.platforms.loopback import LoopbackPlatform
from belle_agent.tools.content import ContentToolkit


def _platform(name: str) -> LoopbackPlatform:
    platform = LoopbackPlatform()
    platform.name = name
    return platform


@pytest.fixture
async def two(tmp_path):
    kit = ScriptedBackend()
    platforms = {name: _platform(name) for name in ("north", "south")}
    backend = RecordedBackend(kit.backend)
    hosted = [HostedPlatform(platform, EventDedupStore(tmp_path / f"{name}.json")) for name, platform in platforms.items()]
    host = ConversationHost(hosted, backend)
    await host.start()
    yield host, platforms, kit, backend
    await host.stop()


def _direct(platform: str, text: str, number: int) -> InboundMessage:
    return InboundMessage(
        platform=platform, workspace="W1", conversation="D-U1", message_id=f"m{number}", event_id=f"e{number}",
        sender="U1", text=text, chat_type="dm", trigger=TRIGGER_DM,
    )


async def test_each_platform_gets_the_answer_and_the_marks_of_its_own_message(two):
    host, platforms, kit, backend = two
    kit.play(ModelTurn(final="to north"), ModelTurn(final="to south"))

    await platforms["north"].inject(_direct("north", "hello", 1))
    await host.idle()
    await platforms["south"].inject(_direct("south", "hello", 2))
    await host.idle()

    assert [(post.text, post.conversation) for post in platforms["north"].log] == [("to north", "D-U1")]
    assert [(post.text, post.conversation) for post in platforms["south"].log] == [("to south", "D-U1")]
    assert platforms["north"].marks == [("m1", MARK_RECEIVED), ("m1", MARK_DONE)]
    assert platforms["south"].marks == [("m2", MARK_RECEIVED), ("m2", MARK_DONE)]
    # The same conversation id on two platforms is two sessions.
    north, south = (request.conversation for request in backend.requests)
    assert north != south and north.startswith("north") and south.startswith("south")


async def test_a_turns_tools_post_on_its_own_platform(two):
    host, platforms, kit, _ = two
    kit.play(ModelTurn(calls=(("post_message", {"text": "by tool"}),), final="final"))

    await platforms["south"].inject(_direct("south", "post it", 1))
    await host.idle()

    # The posting tool of the turn's platform delivered the answer, so the final is not posted as well.
    assert platforms["north"].log == []
    assert [post.text for post in platforms["south"].log] == ["by tool"]


async def test_a_stop_or_an_answer_from_another_platform_reaches_no_turn(two):
    host, platforms, kit, _ = two
    kit.play(ModelTurn(final="done", ask=("Which?", ("a", "b"))))
    message = _direct("north", "ask me", 1)

    await platforms["north"].inject(message)
    async with asyncio.timeout(5):
        while not any(entry.kind == "question" for entry in platforms["north"].log):
            await asyncio.sleep(0.01)
    question = next(entry for entry in platforms["north"].log if entry.kind == "question").question
    elsewhere = {"workspace": "W1", "conversation": "D-U1", "sender": "U1"}

    stopped = await platforms["south"].inject(InboundStop(platform="south", message_id="m1", **elsewhere))
    answered = await platforms["south"].inject(InboundAnswer(platform="south", question_id=question.id, **elsewhere))
    assert (stopped, answered) == ("ignored:no-turn-running", "ignored:question-not-waiting")
    assert platforms["south"].log == []
    assert (await platforms["north"].inject(InboundAnswer(platform="north", question_id=question.id, **elsewhere))).startswith("answered")
    await host.idle()


async def test_a_record_of_a_platform_the_host_does_not_serve_is_dropped(two):
    host, platforms, _, _ = two
    assert await platforms["north"].inject(_direct("west", "hello", 1)) == "dropped:platform-not-served"
    with pytest.raises(ValueError, match="north,south"):
        _ = host.platform


async def test_two_platforms_may_put_a_question_of_the_same_id_and_each_answer_reaches_its_own(two):
    host, platforms, kit, _ = two
    kit.play(*(ModelTurn(final=f"done {name}", ask=("Which?", ("a", "b"))) for name in platforms))
    # The same delivery id on both platforms: one request id, so one question id, in two conversations.
    for name in platforms:
        await platforms[name].inject(_direct(name, "ask me", 1))
    async with asyncio.timeout(5):
        while not all(any(entry.kind == "question" for entry in platform.log) for platform in platforms.values()):
            await asyncio.sleep(0.01)
    asked = {name: next(entry for entry in platform.log if entry.kind == "question").question for name, platform in platforms.items()}
    assert asked["north"].id == asked["south"].id
    where = {"workspace": "W1", "conversation": "D-U1", "sender": "U1"}

    for name in ("south", "north"):
        answered = await platforms[name].inject(InboundAnswer(platform=name, question_id=asked[name].id, **where))
        assert answered.startswith("answered")
    await host.idle()

    assert [post.text for post in platforms["north"].log if post.kind == "post"] == ["done north"]
    assert [post.text for post in platforms["south"].log if post.kind == "post"] == ["done south"]
    assert len({conversation for conversation, _, _ in kit.answers}) == 2


async def test_a_turns_content_tool_reads_an_attachment_on_its_own_platform(tmp_path):
    kit = ScriptedBackend()
    platforms = {name: _platform(name) for name in ("north", "south")}
    payloads = []

    async def request(**call):
        payloads.append(call["payload"])
        return {"ok": True}

    def resolver(name):
        async def resolve(ref, metadata):
            return {"document": f"{name} {ref}"}

        return resolve

    hosted = [
        HostedPlatform(
            platform,
            EventDedupStore(tmp_path / f"{name}.json"),
            content_tools=ContentToolkit({"summarize": {}}, request=request, resolve_attachment=resolver(name)),
        )
        for name, platform in platforms.items()
    ]
    host = ConversationHost(hosted, RecordedBackend(kit.backend))
    await host.start()
    try:
        kit.play(ModelTurn(calls=(("summarize", {"attachment_ref": "F1"}),), final="done"))
        await platforms["south"].inject(_direct("south", "summarize it", 1))
        await host.idle()
    finally:
        await host.stop()

    assert payloads == [{"document": "south F1"}]



async def test_local_status_lifecycle_and_count_privacy(tmp_path, monkeypatch):
    from belle_agent.control.client import get_snapshot
    from belle_agent.control.service import ControlSettings

    runtime = tmp_path / 'runtime'
    monkeypatch.setenv('BELLE_AGENT_RUNTIME_DIR', str(runtime))
    kit = ScriptedBackend()
    platform = _platform('north')
    host = ConversationHost([HostedPlatform(platform, EventDedupStore(tmp_path / 'seen.json'))],
                            RecordedBackend(kit.backend), control=ControlSettings(enabled=True))
    await host.start()
    try:
        snapshot = await get_snapshot(runtime / 'control.sock')
        assert snapshot['work']['metrics']['queued_messages']['value'] == 0
        assert snapshot['work']['metrics']['active_turns'] == {'value': 0, 'reason': None}
        assert snapshot['platforms'][0]['id'] == 'north'
        assert snapshot['platforms'][0]['state'] == 'unsupported'
        assert snapshot['scheduling']['metrics']['due_runs']['value'] is None
        assert host._running
    finally:
        await host.stop()
    assert not (runtime / 'control.sock').exists()
