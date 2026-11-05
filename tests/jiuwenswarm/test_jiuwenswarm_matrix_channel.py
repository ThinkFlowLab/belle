"""Matrix channel registration, account loading, and Gateway turn routing."""

import asyncio
import time
from pathlib import Path

from jiuwenswarm.extensions.channel_contributions import contributed_spec_for, unregister_channel_spec
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.sdk.host_types import EventType, Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from belle_agent.backend import EVENT_SUCCEEDED, TurnRequest
from belle_agent.integrations.jiuwenswarm.matrix_channel import (
    MatrixChannel,
    create_matrix_channels,
    matrix_config,
    matrix_session_id,
)
from belle_agent.platforms.matrix.capabilities import MATRIX_CAPABILITIES
from belle_agent.platforms.matrix.toolkit import METADATA_ACCOUNT, METADATA_ROOM, METADATA_USER
from belle_agent.scopes import channel_capabilities

EXTENSION = Path(__file__).resolve().parents[2] / "src/belle_agent/integrations/jiuwenswarm/extensions/matrix"


async def test_extension_registers_matrix_without_an_agent_plugin() -> None:
    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
    try:
        await ExtensionLoader(registry).load_extension(EXTENSION)
        spec = contributed_spec_for("matrix")
        assert (spec.replaces, spec.source, spec.factory) == ("", "belle-agent", create_matrix_channels)
        assert not registry.get_agent_plugins()
        assert channel_capabilities("matrix") is MATRIX_CAPABILITIES
    finally:
        unregister_channel_spec("matrix")


def test_only_matrix_runs_and_the_agent_server_owns_second_brain() -> None:
    config = {"platforms": {"slack": {}, "mattermost": {}, "matrix": {"accounts": []}},
              "scopes": [], "second_brain": {"enabled": True}}
    assert matrix_config(config) == {
        "platforms": {"matrix": {"accounts": []}}, "scopes": [], "second_brain": {"enabled": False},
    }


def test_factory_keeps_two_accounts_under_one_gateway_channel(tmp_path, monkeypatch) -> None:
    config = tmp_path / "belle.yaml"
    config.write_text(
        "platforms:\n  matrix:\n    accounts:\n"
        "      - name: work\n        homeserver: https://matrix.example\n"
        "        user_id: '@work:matrix.example'\n        store_passphrase_env: MATRIX_WORK_PASSPHRASE\n"
        "      - name: friends\n        homeserver: https://matrix.example\n"
        "        user_id: '@friends:matrix.example'\n        store_passphrase_env: MATRIX_FRIENDS_PASSPHRASE\n"
    )
    monkeypatch.setenv("BELLE_AGENT_CONFIG_FILE", str(config))
    monkeypatch.setenv("BELLE_AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MATRIX_WORK_PASSPHRASE", "work-secret")
    monkeypatch.setenv("MATRIX_FRIENDS_PASSPHRASE", "friends-secret")
    (channel,) = create_matrix_channels({"enabled": True}, RobotMessageRouter())
    assert channel.channel_id == "matrix"
    accounts = channel.host.platforms["matrix"]
    assert set(accounts._members) == {"work", "friends"}


def test_matrix_sessions_are_stable_and_separate_across_accounts_and_rooms() -> None:
    first = "matrix_work_!room:example_@alice:example"
    other_account = "matrix_friends_!room:example_@alice:example"
    other_room = "matrix_work_!other:example_@alice:example"
    assert matrix_session_id(first) == matrix_session_id(first)
    assert len({matrix_session_id(value) for value in (first, other_account, other_room)}) == 3
    assert matrix_session_id(first).isascii() and matrix_session_id(first).replace("_", "").isalnum()


async def test_matrix_turn_routes_to_gateway_and_receives_its_final(tmp_path, monkeypatch) -> None:
    class Host:
        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setenv("BELLE_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("belle_agent.integrations.jiuwenswarm.matrix_channel.host_from_config", lambda *args, **kwargs: Host())
    channel = MatrixChannel({}, RobotMessageRouter())
    sent: list[Message] = []
    channel.on_message(sent.append)
    request = TurnRequest(
        conversation="matrix_work_!room:example_@alice:example", request_id="$event", text="hello",
        metadata={METADATA_ACCOUNT: "work", METADATA_ROOM: "!room:example", METADATA_USER: "@alice:example"},
    )
    events = channel.turns.run_turn(request)
    pending = asyncio.create_task(anext(events))
    for _ in range(100):
        if sent:
            break
        await asyncio.sleep(0)
    (routed,) = sent
    assert (routed.channel_id, routed.req_method, routed.chat_id, routed.user_id) == (
        "matrix", ReqMethod.CHAT_SEND, "!room:example", "@alice:example"
    )
    assert routed.metadata[METADATA_ACCOUNT] == "work"
    assert routed.session_id == matrix_session_id(request.conversation)
    await channel.send(Message(
        id="$event", type="event", channel_id="matrix", session_id=routed.session_id,
        params={}, timestamp=time.time(), ok=True, payload={"content": "Hi Alice"}, event_type=EventType.CHAT_FINAL,
    ))
    assert (await pending).kind == EVENT_SUCCEEDED
    await events.aclose()
