"""Belle's Mattermost channel in JiuwenSwarm: its extension, its factory, and a turn from a post to its answer."""

import asyncio
import sys
import time
from pathlib import Path

import pytest
from jiuwenswarm.extensions.channel_contributions import contributed_spec_for, unregister_channel_spec
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.sdk.host_types import EventType, Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

sys.path.append(str(Path(__file__).resolve().parents[1] / "mattermost"))

from transport import BOT_USER, TOKEN, URL, EventFeeder, FakeRest, hello, posted

from belle_agent.admission.dedup import EventDedupStore
from belle_agent.config import BelleConfigError
from belle_agent.host import ConversationHost, HostedPlatform
from belle_agent.integrations.jiuwenswarm import mattermost_channel
from belle_agent.integrations.jiuwenswarm.mattermost_channel import (
    MattermostChannel,
    create_mattermost_channels,
    mattermost_config,
)
from belle_agent.integrations.jiuwenswarm.mattermost_tools import mount_mattermost_tools
from belle_agent.platforms.mattermost.capabilities import MATTERMOST_CAPABILITIES
from belle_agent.platforms.mattermost.client import MattermostClient
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.settings import TOKEN_ENV, MattermostSettings
from belle_agent.scopes import channel_capabilities

EXTENSION = Path(__file__).resolve().parents[2] / "src/belle_agent/integrations/jiuwenswarm/extensions/mattermost"
DM = "d" * 26
ALICE = "a" * 26


def belle_file(tmp_path: Path, monkeypatch, text: str) -> None:
    path = tmp_path / "belle.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("BELLE_AGENT_CONFIG_FILE", str(path))
    monkeypatch.setenv("BELLE_AGENT_STATE_DIR", str(tmp_path / "state"))


async def test_the_extension_contributes_a_channel_of_its_own_and_the_tools_of_the_agent() -> None:
    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
    try:
        await ExtensionLoader(registry).load_extension(EXTENSION)
        spec = contributed_spec_for("mattermost")
        assert (spec.replaces, spec.source, spec.factory) == ("", "belle-agent", create_mattermost_channels)
        assert mount_mattermost_tools in registry.get_agent_plugins()
        # The AgentServer settles the scoped tool levels of this channel's requests.
        assert channel_capabilities("mattermost") is MATTERMOST_CAPABILITIES
    finally:
        unregister_channel_spec("mattermost")


def test_the_host_serves_mattermost_alone_with_no_second_brain_of_its_own() -> None:
    config = {"platforms": {"slack": {"enforce_audience": True}, "loopback": {}, "mattermost": {"url": URL}},
              "scopes": [], "second_brain": {"enabled": True}}
    assert mattermost_config(config) == {
        "platforms": {"mattermost": {"url": URL}}, "scopes": [], "second_brain": {"enabled": False},
    }


def test_the_factory_builds_one_channel_from_belles_section(tmp_path, monkeypatch) -> None:
    belle_file(tmp_path, monkeypatch, f"platforms:\n  slack:\n    enforce_audience: true\n  mattermost:\n    url: {URL}\n")
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    (channel,) = create_mattermost_channels({"enabled": True}, RobotMessageRouter())
    assert channel.channel_id == "mattermost"
    assert list(channel.host.platforms) == ["mattermost"]


def test_the_factory_refuses_a_section_that_is_off_and_names_what_is_missing(tmp_path, monkeypatch) -> None:
    belle_file(tmp_path, monkeypatch, f"platforms:\n  mattermost:\n    url: {URL}\n")
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(BelleConfigError, match=TOKEN_ENV):
        create_mattermost_channels({"enabled": True}, RobotMessageRouter())


def event(request: Message, kind: EventType, payload: dict) -> Message:
    """An event of ``request``: the Gateway sends it with that request's id and session."""
    return Message(id=request.id, type="event", channel_id="mattermost", session_id=request.session_id, params={},
                   timestamp=time.time(), ok=True, payload=payload, event_type=kind)


@pytest.fixture
async def served(tmp_path, monkeypatch):
    """A channel whose host serves the fake server, started, and what it hands the Gateway."""
    rest = FakeRest()
    feeder = EventFeeder(hello())
    client = MattermostClient(URL, TOKEN, transport=rest.transport(), open_events=feeder.open)
    settings = MattermostSettings(URL, TOKEN, actions_url="http://belle.invalid:18400", actions_port=0)
    connector = MattermostPlatform(settings, client=client)
    monkeypatch.setattr(
        mattermost_channel, "host_from_config",
        lambda config, backend, state_dir: ConversationHost(
            [HostedPlatform(connector, EventDedupStore(tmp_path / "seen.json"))], backend
        ),
    )
    channel = MattermostChannel({}, RobotMessageRouter())
    routed: list[Message] = []
    channel.on_message(routed.append)
    started = asyncio.create_task(channel.start())
    while connector.receiver is None:
        await asyncio.sleep(0.01)
    yield channel, connector, rest, feeder, routed
    await channel.stop()
    await started


async def until(condition) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


def bot_posts(rest: FakeRest) -> list[dict]:
    return [post for post in rest.posts.values() if post.get("user_id") == BOT_USER]


async def test_a_direct_message_is_a_gateway_request_and_its_final_is_posted_back(served) -> None:
    channel, _connector, rest, feeder, routed = served
    feeder.feed(posted("p" * 26, channel=DM, user=ALICE, channel_type="D", message="hello Belle"))
    await until(lambda: routed)
    (msg,) = routed
    assert (msg.channel_id, msg.req_method, msg.chat_id, msg.user_id, msg.id) == (
        "mattermost", ReqMethod.CHAT_SEND, DM, ALICE, "p" * 26
    )
    assert msg.session_id == f"mattermost_direct_{DM}_{ALICE}"
    assert msg.params["query"].startswith("hello Belle")
    assert (msg.metadata["mattermost_channel_id"], msg.metadata["mattermost_user_id"]) == (DM, ALICE)
    await channel.send(event(msg, EventType.CHAT_FINAL, {"content": "Hello alice"}))
    await until(lambda: any(post.get("message") == "Hello alice" for post in bot_posts(rest)))
    (answer,) = [post for post in bot_posts(rest) if post.get("message") == "Hello alice"]
    assert answer["channel_id"] == DM


async def test_an_approval_is_put_as_buttons_and_a_click_resumes_the_request(served) -> None:
    channel, connector, rest, feeder, routed = served
    feeder.feed(posted("p" * 26, channel=DM, user=ALICE, channel_type="D", message="react please"))
    await until(lambda: routed)
    await channel.send(event(routed[0], EventType.CHAT_ASK_USER_QUESTION, {
        "request_id": "q1",
        "source": "permission_interrupt",
        "questions": [{"question": "Run react_to_message?", "options": [
            {"label": "Allow once", "value": "allow_once"}, {"label": "Reject", "value": "reject"},
        ]}],
    }))
    await until(lambda: any(post.get("props") for post in bot_posts(rest)))
    (question,) = [post for post in bot_posts(rest) if post.get("props")]
    buttons = question["props"]["attachments"][0]["actions"]
    assert [button["name"] for button in buttons] == ["Allow once", "Reject"]
    clicked = await connector.questions.on_click({
        "user_id": ALICE, "user_name": "alice", "post_id": question["id"], "channel_id": DM,
        "context": buttons[0]["integration"]["context"],
    })
    assert "update" in clicked
    resume = routed[-1]
    assert (resume.req_method, resume.params["answers"][0]["selected_options"]) == (ReqMethod.CHAT_SEND, ["allow_once"])
    await channel.send(event(resume, EventType.CHAT_FINAL, {"content": "Reacted"}))
    await until(lambda: any(post.get("message") == "Reacted" for post in bot_posts(rest)))


async def test_an_event_of_no_running_turn_is_dropped(served) -> None:
    channel, _connector, rest, _feeder, _routed = served
    nobody = Message(id="nobody", type="req", channel_id="mattermost", session_id=f"mattermost_direct_{DM}_{ALICE}",
                     params={}, timestamp=time.time(), ok=True)
    await channel.send(event(nobody, EventType.CHAT_FINAL, {"content": "late"}))
    assert not bot_posts(rest)
