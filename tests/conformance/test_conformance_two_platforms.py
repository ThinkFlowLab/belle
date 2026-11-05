"""One host on two platforms at once, built from one configuration: loopback, and another platform on its kit.

Each check delivers records on both platforms and reads both logs. What a record leads to reaches its own
platform and never the other one. A later platform adds a parameter to ``other_kit``.
"""

import asyncio
import dataclasses
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml
from conformance_kit import LoopbackKit, ModelTurn, RecordedBackend, World, as_file, scripted_second_brain
from scripted_backend import ScriptedBackend

from belle_agent.config import load_config
from belle_agent.host import host_from_config
from belle_agent.scopes import capabilities as scope_capabilities
from belle_agent.second_brain.backend import SecondBrainBackend
from belle_agent.tools.host import TOOL_OF_ANOTHER_PLATFORM

sys.path.append(str(Path(__file__).resolve().parent))

from discord_kit import DiscordKit
from matrix_kit import MatrixKit
from mattermost_kit import MattermostKit
from slack_kit import SlackKit

#: The settings of the other platform that turn its marks on, so that a mark can be looked for on both platforms.
MARKS_ON = {
    "slack": {"acknowledge_mode": "reaction"},
    "mattermost": {"acknowledgement_emoji": "eyes"},
    "discord": {"acknowledgement_emoji": "eyes"},
    "matrix": {"acknowledgement_emoji": "eyes"},
}
#: The checks that cannot hold with Mattermost beside loopback, each with the part or the difference behind it.
NOT_ON_MATTERMOST = {
    "test_the_same_delivery_id_on_two_platforms_gives_two_turns": (
        "a Mattermost post's delivery id is its post id, so the kit cannot give it the delivery id of a loopback message"
    ),
}
#: The checks that cannot hold with Discord beside loopback, each with the difference behind it.
NOT_ON_DISCORD = {
    "test_a_turn_lists_and_calls_only_the_tools_of_its_own_platform": (
        "the Discord kit holds no file for a tool to read, though download_discord_attachment reads one"
    ),
    "test_public_ingest_selects_what_a_platform_calls_public_and_warns_of_nothing": (
        "Discord calls no chat type public, so public selects nothing there and the host warns of it"
    ),
}
#: The checks that cannot hold with Matrix beside loopback, each with the difference behind it.
NOT_ON_MATRIX = {
    "test_the_same_delivery_id_on_two_platforms_gives_two_turns": (
        "a Matrix message's delivery id is its event id, so the kit cannot give it the delivery id of a loopback message"
    ),
}


@pytest.fixture(params=["slack", "mattermost", "matrix", "discord"])
def other_kit(request: pytest.FixtureRequest, monkeypatch) -> Any:
    """The platform beside loopback, on its kit."""
    if request.param == "matrix":
        if request.node.originalname in NOT_ON_MATRIX:
            pytest.skip(f"a Matrix difference: {NOT_ON_MATRIX[request.node.originalname]}")
        return MatrixKit(monkeypatch)
    if request.param == "mattermost":
        if request.node.originalname in NOT_ON_MATTERMOST:
            pytest.skip(f"a Mattermost difference: {NOT_ON_MATTERMOST[request.node.originalname]}")
        return MattermostKit(monkeypatch)
    if request.param == "discord":
        if request.node.originalname in NOT_ON_DISCORD:
            pytest.skip(f"a Discord difference: {NOT_ON_DISCORD[request.node.originalname]}")
        return DiscordKit(monkeypatch)
    return SlackKit(monkeypatch)


def _answer_everything(names: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{"match": {"channel": name}, "delivery": {"mode": ["all"]}} for name in names]


@pytest_asyncio.fixture
async def build_pair(other_kit, tmp_path):
    """Return a builder of a loopback world and a world of the other platform that share one host and one backend."""
    registry = scope_capabilities.snapshot_registry()
    hosts = []
    name = other_kit.name

    async def build(scopes: Any = None, corpus: SecondBrainBackend | None = None, **more: Any):
        settings = yaml.safe_load(other_kit.configure(getattr(other_kit, "shows", "")))
        settings["platforms"][name].update(MARKS_ON.get(name, {}))
        settings["platforms"]["loopback"] = {"enabled": True}
        scopes = _answer_everything(("loopback", name)) if scopes is None else scopes
        settings.update(scopes=scopes, **more)
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(settings), encoding="utf-8")
        kit = ScriptedBackend()
        backend = RecordedBackend(kit.backend)
        host = host_from_config(load_config(path), backend, state_dir=tmp_path / "state", second_brain=corpus)
        hosts.append(host)
        await host.start()
        loopback = World(host=host, platform=host.platforms["loopback"], kit=kit, backend=backend, platform_kit=LoopbackKit())
        # The two worlds number their messages from one count, so that each message id names one message.
        other = World(
            host=host, platform=other_kit.attach(host), kit=kit, backend=backend, platform_kit=other_kit, _ids=loopback._ids
        )
        return loopback, other

    try:
        yield build
    finally:
        for host in hosts:
            await host.stop()
        scope_capabilities.restore_registry(registry)


@pytest_asyncio.fixture
async def pair(build_pair):
    """A loopback world and a world of the other platform that share one host and one backend."""
    return await build_pair()


def _texts(world: World) -> list[str]:
    return [post.text for post in world.posts()]


async def test_one_host_serves_both_platforms(pair) -> None:
    loopback, other = pair
    assert set(loopback.host.platforms) == {"loopback", other.platform_kit.name}


async def test_a_message_on_each_platform_is_answered_on_its_own(pair) -> None:
    loopback, other = pair
    loopback.kit.play(ModelTurn(final="loopback answer"), ModelTurn(final="other answer"))

    await loopback.send(loopback.direct("hello"))
    await other.send(other.direct("hello"))

    assert _texts(loopback) == ["loopback answer"]
    assert _texts(other) == ["other answer"]
    # The same conversation id on two platforms is two sessions.
    first, second = (request.conversation for request in loopback.requests)
    assert first.startswith("loopback") and second.startswith(other.platform_kit.name)


async def test_a_mark_reaches_only_the_platform_of_its_message(pair) -> None:
    loopback, other = pair

    await other.send(other.message("in a channel", conversation="C1"))

    assert [entry.kind for entry in other.platform.log if entry.kind == "reaction"]
    assert loopback.platform.marks == [] and loopback.platform.log == []


async def test_a_turn_lists_and_calls_only_the_tools_of_its_own_platform(pair) -> None:
    loopback, other = pair
    file_tool, arguments = other.platform_kit.holds(other.platform, "notes.txt", "text/plain", b"notes")
    loopback.kit.play(
        ModelTurn(final="held", hold=True),
        ModelTurn(final="held", hold=True),
        ModelTurn(final="done", calls=((file_tool, arguments), ("post_message", {"text": "said on loopback"}))),
    )
    listed = {}
    for world in (loopback, other):
        await world.platform.inject(world.message("which tools?", conversation="C1"))
        await world.kit.holding()
        listed[world.platform_kit.name] = {d["name"] for d in world.host.tools.definitions(world.requests[-1].conversation)}
        world.kit.release()
        await world.host.idle()

    theirs = listed[other.platform_kit.name]
    assert "post_message" in listed["loopback"] & theirs
    assert file_tool in theirs and file_tool not in listed["loopback"]
    assert "read_file" in listed["loopback"] and "read_file" not in theirs

    await loopback.send(loopback.message("call them", conversation="C1"))
    refused, posted = loopback.kit.results[-2:]
    assert TOOL_OF_ANOTHER_PLATFORM in refused and '"ok": true' in posted.replace('"ok":true', '"ok": true')
    assert "said on loopback" in _texts(loopback)
    assert "said on loopback" not in _texts(other)


async def test_the_backend_is_told_to_offer_each_turn_only_the_tools_of_its_platform(pair) -> None:
    loopback, other = pair
    loopback.kit.play(ModelTurn(final="held", hold=True), ModelTurn(final="held", hold=True))
    every = {definition["name"] for definition in loopback.host.tools.definitions("")}
    own, denied = {}, {}
    for world in (loopback, other):
        await world.platform.inject(world.direct("which tools?"))
        await world.kit.holding()
        request = world.requests[-1]
        own[world.platform_kit.name] = {d["name"] for d in world.host.tools.definitions(request.conversation)}
        denied[world.platform_kit.name] = {name for name, level in request.scope.tools.items() if level == "deny"}
        world.kit.release()
        await world.host.idle()

    # Each turn's own tools, a name both platforms serve among them, stay on. Every other tool is off.
    for name, tools in own.items():
        assert denied[name] == every - tools
    assert "read_file" in denied[other.platform_kit.name] and "read_file" in own["loopback"]


async def test_a_question_a_card_and_a_stop_reach_only_the_platform_of_their_turn(pair) -> None:
    loopback, other = pair
    loopback.kit.play(ModelTurn(final="other answer", ask=("Which one?", ("a", "b"))))
    message = other.direct("ask me")

    await other.platform.inject(message)
    shown = (await other.wait_question())[0]
    # A stop and an answer on the other platform name the same ids, and reach nothing.
    assert await loopback.platform.inject(loopback.stop(message)) == "ignored:no-turn-running"
    assert await loopback.platform.inject(loopback.answer_to(shown, values=("a",))) == "ignored:question-not-waiting"
    await other.send(other.answer_to(shown, values=("a",)))

    assert loopback.questions() == [] and loopback.platform.log == []
    assert loopback.platform.progressed == [] and loopback.platform.concluded == []
    assert {message_id for message_id, _ in other.platform.progressed} == {message.message_id}
    assert [message_id for message_id, *_ in other.platform.concluded] == [message.message_id]


async def test_a_stop_on_the_platform_of_the_turn_stops_it(pair) -> None:
    loopback, other = pair
    loopback.kit.play(ModelTurn(final="never", hold=True))
    message = loopback.direct("long work")

    await loopback.platform.inject(message)
    await loopback.kit.holding()
    assert (await other.platform.inject(other.stop(message))).startswith("ignored")
    assert (await loopback.platform.inject(loopback.stop(message))).startswith("stopped")
    loopback.kit.release()
    await asyncio.wait_for(loopback.host.idle(), 5)
    assert _texts(loopback) == [] and _texts(other) == []


async def test_the_same_delivery_id_on_two_platforms_gives_two_turns(pair) -> None:
    loopback, other = pair
    loopback.kit.play(ModelTurn(final="loopback answer"), ModelTurn(final="other answer"))
    first = loopback.direct("hello")
    second = dataclasses.replace(other.direct("hello"), event_id=first.event_id)

    await loopback.send(first)
    await other.send(second)

    assert (_texts(loopback), _texts(other)) == (["loopback answer"], ["other answer"])


async def test_public_ingest_selects_what_a_platform_calls_public_and_warns_of_nothing(build_pair, tmp_path, caplog) -> None:
    brain = {"enabled": True, "corpus_dir": str(tmp_path / "corpus"), "ingest": {"channels": ["public"]}}
    with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
        loopback, other = await build_pair(scopes=[], corpus=scripted_second_brain(), second_brain=brain)
    assert "selects nothing" not in caplog.text

    # The same chat type and conversation id: public on a platform that names its public conversations, and not on loopback.
    public = min(other.host.platforms[other.platform_kit.name].public_chat_types)
    shared = {"conversation": "C1", "chat_type": public, "files": (as_file("kite-bridle.md"),)}
    assert await other.send(other.message("see this", **shared)) == "ingested:second-brain"
    assert await loopback.send(loopback.message("see this", **shared)) == "ignored:no-trigger-matched"


async def test_three_platforms_on_one_host_answer_and_mark_each_message_on_its_own(monkeypatch, tmp_path) -> None:
    """Loopback, Mattermost and Matrix on one host: no reply and no mark reaches another platform."""
    registry = scope_capabilities.snapshot_registry()
    kits = (MattermostKit(monkeypatch), MatrixKit(monkeypatch))
    settings: dict[str, Any] = {"platforms": {"loopback": {"enabled": True}}}
    for kit in kits:
        settings["platforms"].update(yaml.safe_load(kit.configure(""))["platforms"])
        settings["platforms"][kit.name].update(MARKS_ON[kit.name])
    settings["scopes"] = _answer_everything(("loopback", *(kit.name for kit in kits)))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    scripted = ScriptedBackend()
    backend = RecordedBackend(scripted.backend)
    host = host_from_config(load_config(path), backend, state_dir=tmp_path / "state")
    await host.start()
    try:
        loopback = World(host=host, platform=host.platforms["loopback"], kit=scripted, backend=backend, platform_kit=LoopbackKit())
        worlds = [loopback] + [
            World(host=host, platform=kit.attach(host), kit=scripted, backend=backend, platform_kit=kit, _ids=loopback._ids)
            for kit in kits
        ]
        assert set(host.platforms) == {"loopback", "mattermost", "matrix"}
        for world in worlds:
            scripted.play(ModelTurn(final=f"{world.platform_kit.name} answer"))
            await world.send(world.direct("hello"))
        for world in worlds[1:]:
            scripted.play(ModelTurn(final=f"{world.platform_kit.name} room"))
            await world.send(world.message("in a room", conversation="C1"))
        for world in worlds:
            name = world.platform_kit.name
            assert _texts(world) == [f"{name} answer"] + ([f"{name} room"] if world is not loopback else [])
        names = [world.platform_kit.name for world in worlds]
        assert all(request.conversation.startswith(name) for request, name in zip(loopback.requests, names, strict=False))
        # Each channel message is marked on its own platform; loopback, which had none, shows no reaction.
        for world in worlds[1:]:
            assert [entry for entry in world.platform.log if entry.kind == "reaction"]
        assert not [entry for entry in loopback.platform.log if entry.kind == "reaction"]
    finally:
        await host.stop()
        scope_capabilities.restore_registry(registry)


async def test_four_chat_platforms_on_one_host_answer_and_mark_each_message_on_its_own(monkeypatch, tmp_path) -> None:
    """Slack, Discord, Mattermost and Matrix on one host, with no loopback: no reply and no mark reaches another platform."""
    registry = scope_capabilities.snapshot_registry()
    kits = (SlackKit(monkeypatch), DiscordKit(monkeypatch), MattermostKit(monkeypatch), MatrixKit(monkeypatch))
    settings: dict[str, Any] = {"platforms": {}}
    for kit in kits:
        settings["platforms"].update(yaml.safe_load(kit.configure(""))["platforms"])
        settings["platforms"][kit.name].update(MARKS_ON[kit.name])
    settings["scopes"] = _answer_everything(tuple(kit.name for kit in kits))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    scripted = ScriptedBackend()
    backend = RecordedBackend(scripted.backend)
    host = host_from_config(load_config(path), backend, state_dir=tmp_path / "state")
    await host.start()
    try:
        first = World(host=host, platform=kits[0].attach(host), kit=scripted, backend=backend, platform_kit=kits[0])
        worlds = [first] + [
            World(host=host, platform=kit.attach(host), kit=scripted, backend=backend, platform_kit=kit, _ids=first._ids)
            for kit in kits[1:]
        ]
        assert set(host.platforms) == {"slack", "discord", "mattermost", "matrix"}
        for world in worlds:
            scripted.play(ModelTurn(final=f"{world.platform_kit.name} answer"))
            await world.send(world.direct("hello"))
        for world in worlds:
            scripted.play(ModelTurn(final=f"{world.platform_kit.name} room"))
            await world.send(world.message("in a room", conversation="C1"))
        await host.idle()
        for world in worlds:
            name = world.platform_kit.name
            assert _texts(world) == [f"{name} answer", f"{name} room"]
            assert [entry for entry in world.platform.log if entry.kind == "reaction"]
        # The same conversation id on four platforms is four conversations.
        rooms = [request.conversation for request in first.requests if "C1" in request.conversation]
        assert len(set(rooms)) == 4
        assert sorted(room.split("_")[0] for room in rooms) == sorted(kit.name for kit in kits)
    finally:
        await host.stop()
        scope_capabilities.restore_registry(registry)
