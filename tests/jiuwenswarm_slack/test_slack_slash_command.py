"""Slack slash command parsing, permissions, forms, and turn dispatch."""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from belle_agent.integrations.jiuwenswarm import config as belle_config
import jiuwenswarm

from belle_agent.platforms.slack.admission import SlackMessageOrigin, SlackTurnInitiator
from belle_agent.platforms.slack import config as slack_config

from belle_agent.platforms.slack import command_ui
from belle_agent.platforms.slack.scoped_config import apply_scopes_to_slack_overrides
import yaml
from slack_sdk.web.async_slack_response import AsyncSlackResponse
from belle_agent.platforms.slack import commands as slack_commands

from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from belle_agent.scopes import compile_scopes
from belle_agent.platforms.slack.scope_policy import (
    COMMAND_REQUIREMENTS,
    DEFAULT_BOT_NAME,
    DEFAULT_COMMAND_NAME,
    MAIN_COMMAND,
    command_token,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.integrations.jiuwenswarm import slack_command_handlers as slash_handlers
from belle_agent.integrations.jiuwenswarm import slack_inbound
from belle_agent.platforms.slack import socket_mode as belle_socket_mode
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
)

TEAM = "T0TESTTEAM"
USER = "U0ASKER001"
OUTSIDER = "U0OUTSIDE3"
BOT_USER = "U0THEBOT01"
BOT_ID = "B0THEBOT01"
DM = "D0DIRECT01"
ROOM = "C0CHANNEL1"
OTHER_ROOM = "C0CHANNEL2"
RESPONSE_URL = "https://hooks.slack.com/commands/T0TESTTEAM/1/secret"
# What a workspace that changed nothing types. Read from the policy module
# rather than written out, so a test cannot come to assert a command no
# deployment has.
COMMAND = command_token(DEFAULT_COMMAND_NAME)
# What a workspace that set no bot_name reads. Read from the policy module for
# the same reason COMMAND is, and it is the resolved value rather than the
# template's: an install that already wrote bot_name: "" reaches this too.
BOT_NAME = DEFAULT_BOT_NAME

# What the runtime's command.status answers with, secrets and paths included,
# so the tests can hold that none of them reaches Slack.
STATUS_PAYLOAD = {
    "version": "9.1.0",
    "session_id": "",
    "cwd": "/srv/jiuwen/private-workdir",
    "model": "glm-4.6",
    "provider": "openai",
    "api_base": "https://llm.internal.example/v1",
    "connection_status": "connected",
    "mcp_servers": [{"name": "internal-billing-db", "enabled": True, "transport": "stdio"}],
    "config_path": "/home/ops/.jiuwenswarm/config/config.yaml",
    "settings_sources": ["/home/ops/.jiuwenswarm/config/config.yaml"],
    "memory_warnings": [{"path": "/srv/jiuwen/JIUWENSWARM.md", "kind": "project",
                         "char_count": 1, "threshold": 0, "message": "large"}],
}
_STATUS_SECRETS = (
    "llm.internal.example", "private-workdir", "internal-billing-db",
    "config.yaml", "JIUWENSWARM.md",
)

CATALOG = [
    {"name": "research", "installed": True, "enabled": True,
     "description": "Find and summarise sources."},
    {"name": "ambient", "installed": True, "enabled": True, "description": ""},
    {"name": "switched-off", "installed": True, "enabled": False},
    {"name": "marketplace-only", "installed": False, "enabled": True},
]


pytestmark = pytest.mark.usefixtures("isolated_slack_dedup_store")


class _SlackApiError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(f"The request to the Slack API failed.\n{error}")
        self.response = {"ok": False, "error": error}


class _RecordingSlackClient:
    def __init__(
        self, *, post_error: str = "", info: "dict[str, Any] | None" = None,
        users: "dict[str, dict[str, Any]] | None" = None,
    ) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self.reactions: list[dict[str, Any]] = []
        self.removed_reactions: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []
        self.info_calls: list[str] = []
        self.user_info_calls: list[str] = []
        self.post_error = post_error
        self.info = info
        self.users = users or {}

    async def chat_delete(self, **kwargs: Any) -> dict[str, bool]:
        self.deleted.append((kwargs.get("channel", ""), kwargs.get("ts", "")))
        return {"ok": True}

    async def users_info(self, *, user: str) -> dict[str, Any]:
        self.user_info_calls.append(user)
        if user not in self.users:
            raise _SlackApiError("user_not_found")
        return {"ok": True, "user": self.users[user]}

    async def conversations_info(self, *, channel: str) -> AsyncSlackResponse:
        self.info_calls.append(channel)
        if self.info is None:
            raise _SlackApiError("missing_scope")
        return AsyncSlackResponse(
            client=None, http_verb="GET", api_url="https://slack.com/api/conversations.info",
            req_args={}, data={"ok": True, "channel": self.info},
            headers={}, status_code=200,
        )

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        if self.post_error:
            raise _SlackApiError(self.post_error)
        self.posts.append(kwargs)
        # Slack answers a post addressed to a person from the app's DM with them.
        channel = kwargs["channel"]
        return {
            "ok": True,
            "channel": DM if channel.startswith("U") else channel,
            "ts": f"1780000000.{len(self.posts):06d}",
        }

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.reactions.append(kwargs)
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        self.removed_reactions.append(kwargs)
        return {"ok": True}

    async def api_call(self, method: str, **kwargs: Any) -> dict[str, bool]:
        self.statuses.append({"method": method, **kwargs})
        return {"ok": True}


class _Recorder:
    """Stands in for bolt's ``ack`` and ``respond``, which take the same keywords."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *_args: Any, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    @property
    def text(self) -> str:
        assert len(self.calls) == 1, f"called {len(self.calls)} times"
        return self.calls[0].get("text", "")


def _channel(
    *,
    client: "_RecordingSlackClient | None" = None,
    reply: "Any" = None,
    expert_reply: "Any" = None,
    subagents_reply: "Any" = None,
    fork_ok: bool = True,
    status_reply: "Any" = ...,
    warm_chat_types: bool = True,
    **config: Any,
) -> tuple[SlackChannel, list[Message]]:
    """A running channel, and every request it routes.

    ``reply`` answers a routed ``skills.list`` as ``(ok, payload)``, sent back
    through ``send`` the way the gateway sends it; ``None`` sends nothing. A
    ``session.fork`` is answered with the target it named, or refused when
    ``fork_ok`` is false.
    """
    config.setdefault("acknowledge_mode", "off")
    config.setdefault("thinking_status", "")
    # The subject here is the line assistant.threads.setStatus draws, so the
    # surface is pinned. Under the default word, auto, a config whose wording is
    # the shipped wording probes for agents.sessions.setStatus, which draws four
    # fixed lifecycle words and none of the text below.
    config.setdefault("session_surface", "assistant")
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, default_channel_id=ROOM, **config),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._identity.team_id = TEAM
    channel._identity.bot_user_id = BOT_USER
    channel._identity.bot_id = BOT_ID
    channel._client = client or _RecordingSlackClient()
    if warm_chat_types:
        channel._chat_types[ROOM] = "channel"
    routed: list[Message] = []

    async def route(req: Message) -> None:
        routed.append(req)
        answer = None
        if req.req_method is ReqMethod.SKILLS_LIST and reply is not None:
            answer = reply
        elif req.req_method is ReqMethod.AGENT_TEMPLATES_LIST and expert_reply is not None:
            answer = expert_reply
        elif req.req_method is ReqMethod.SUBAGENTS_LIST and subagents_reply is not None:
            answer = subagents_reply
        elif req.req_method is ReqMethod.COMMAND_STATUS:
            answer = (True, STATUS_PAYLOAD) if status_reply is ... else status_reply
        elif req.req_method is ReqMethod.SESSION_FORK:
            answer = (
                (True, {"session_id": req.params["target_session_id"]})
                if fork_ok
                else (False, {"error": "source session not found"})
            )
        if answer is not None:
            ok, payload = answer
            await channel.send(
                Message(
                    id=req.id,
                    type="res",
                    channel_id="slack",
                    session_id=req.session_id,
                    params={},
                    timestamp=0.0,
                    ok=ok,
                    payload=payload,
                    event_type=EventType.CHAT_FINAL,
                    metadata=req.metadata,
                )
            )

    channel.on_message(route)
    return channel, routed


def _command(text: str = "help", **fields: str) -> dict[str, str]:
    payload = {
        "command": COMMAND,
        "text": text,
        "team_id": TEAM,
        "user_id": USER,
        "channel_id": DM,
        "channel_name": "directmessage",
        "response_url": RESPONSE_URL,
        "trigger_id": "1.2.abc",
        "token": "verification-token",
    }
    payload.update(fields)
    return payload


async def _run(
    channel: SlackChannel, command: "dict[str, Any] | None"
) -> tuple[_Recorder, _Recorder]:
    ack, respond = _Recorder(), _Recorder()
    await channel._handle_slash_command(ack, command, respond)  # type: ignore[arg-type]
    assert len(ack.calls) == 1, "every invocation is acknowledged exactly once"
    return ack, respond


async def _cancel_picker(
    channel: SlackChannel, command: str, **fields: str
) -> None:
    _, picker = await _run(channel, _command(command, **fields))
    cancel = picker.calls[0]["blocks"][-1]["elements"][1]
    assert cancel["action_id"] == command_ui.CANCEL_SELECT_ACTION
    assert cancel["text"]["text"] == "Cancel"
    response = _Recorder()
    await channel._handle_slash_select_cancel(_Recorder(), {}, cancel, response)
    assert response.calls == [{
        "response_type": "ephemeral", "replace_original": True,
        "text": command_ui.CANCELLED_NOTICE, "blocks": [],
    }]


def _task_form_click(
    form_reply: _Recorder, *, name: str = "", task: str = "hello",
    user_id: str = USER, channel_id: str = DM,
) -> tuple[dict[str, Any], dict[str, Any]]:
    blocks = form_reply.calls[0]["blocks"]
    button = blocks[-1]["elements"][0]
    values: dict[str, Any] = {
        "belle_slash_task:task_block": {
            "belle_slash_task:task": {"type": "plain_text_input", "value": task}
        }
    }
    if name:
        values["belle_slash_task:name_block"] = {
            "belle_slash_task:name": {
                "type": "static_select", "selected_option": {"value": name}
            }
        }
    body = {
        "type": "block_actions",
        "team": {"id": TEAM},
        "user": {"id": user_id},
        "channel": {"id": channel_id},
        "state": {"values": values},
    }
    return body, button


def _task_name_element(reply: _Recorder) -> dict[str, Any]:
    return next(
        block["element"] for block in reply.calls[0]["blocks"]
        if block.get("block_id") == slack_connect.slack_task_forms.NAME_BLOCK
    )


def _picker_click(
    response: _Recorder, *, channel_id: str = ROOM, team_id: str = TEAM,
    user_id: str = USER, index: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    element = response.calls[-1]["blocks"][-1]["elements"][0]
    action = {
        "action_id": element["action_id"],
        "selected_option": element["options"][index],
    }
    body = {
        "team": {"id": team_id},
        "user": {"id": user_id},
        "channel": {"id": channel_id},
        "container": {"type": "message", "channel_id": channel_id},
    }
    return body, action


def _scoped_config(
    agent: "dict[str, Any] | None" = None,
    *,
    entries: "list[dict[str, Any]] | None" = None,
    **config: Any,
) -> dict[str, Any]:
    entries = entries or [{"match": {"channel": "slack"}, "agent": agent}]
    scopes = compile_scopes(entries, warn=lambda *a: None)
    platform, resolved = apply_scopes_to_slack_overrides({}, scopes=scopes)
    return {
        "scopes": scopes,
        "platform_override": platform,
        "conversation_overrides": resolved,
        **config,
    }


# ── parsing and text ────────────────────────────────────────────────────────


def _waiting(
    *, user: str = USER, channel: str = ROOM,
    stamp: str = "1790600000.000001", thread: str = "1790500000.000001",
) -> slack_connect._SlackQueuedMessage:
    return slack_connect._SlackQueuedMessage(
        request=None,  # type: ignore[arg-type]
        session_id=f"slack_{TEAM}_{channel}_{thread}",
        user_id=user, is_dm=False, chat_type="channel", team_id=TEAM,
        channel_id=channel, message_ts=stamp,
        reply_thread_ts=thread, root_thread_ts=thread,
    )


@pytest.mark.asyncio
async def test_queue_list_shows_only_waiting_messages_in_this_conversation() -> None:
    channel, _ = _channel()
    channel._identity.host = "acme.slack.com"
    own = _waiting()
    elsewhere = _waiting(channel=OTHER_ROOM, stamp="1790600000.000002")
    channel._queued_messages[own.session_id] = [own]
    channel._queued_messages[elsewhere.session_id] = [elsewhere]

    ack, respond = await _run(channel, _command("queue list", channel_id=ROOM))
    assert ack.calls == [{}]
    assert respond.calls[0]["response_type"] == "ephemeral"
    assert "https://acme.slack.com/archives/C0CHANNEL1/p1790600000000001" in respond.text
    assert "1790600000.000002" not in respond.text
    assert all("blocks" not in call for call in respond.calls)


@pytest.mark.asyncio
async def test_queue_list_resolves_each_sender_once_and_keeps_names_silent() -> None:
    client = _RecordingSlackClient(users={
        USER: {"profile": {"display_name": "  Ada <@U0OTHER> & @everyone  "}},
        OUTSIDER: {"profile": {"display_name": ""}, "real_name": "Grace"},
    })
    channel, _ = _channel(client=client)
    channel._identity.host = "acme.slack.com"
    entries = [
        _waiting(stamp="1790600000.000001"),
        _waiting(stamp="1790600000.000002"),
        _waiting(user=OUTSIDER, stamp="1790600000.000003"),
    ]
    channel._queued_messages[entries[0].session_id] = entries

    _, respond = await _run(channel, _command("queue list", channel_id=ROOM))

    assert client.user_info_calls == [USER, OUTSIDER]
    assert respond.text.count("Ada &lt;@\u200bU0OTHER&gt; &amp; @\u200beveryone") == 2
    assert "from Grace" in respond.text
    assert "<@U0OTHER>" not in respond.text
    assert "@everyone" not in respond.text
    assert "https://acme.slack.com/archives/C0CHANNEL1/p1790600000000001" in respond.text


@pytest.mark.asyncio
async def test_queue_list_falls_back_to_id_when_user_lookup_fails() -> None:
    client = _RecordingSlackClient()
    channel, _ = _channel(client=client)
    entry = _waiting()
    channel._queued_messages[entry.session_id] = [entry]

    _, respond = await _run(channel, _command("queue list", channel_id=ROOM))

    assert client.user_info_calls == [USER]
    assert f"from {USER}" in respond.text


@pytest.mark.asyncio
async def test_queue_list_does_not_look_up_users_when_empty() -> None:
    client = _RecordingSlackClient()
    channel, _ = _channel(client=client)

    _, respond = await _run(channel, _command("queue list", channel_id=ROOM))

    assert client.user_info_calls == []
    assert respond.text == "No messages are waiting in this channel or direct message."


@pytest.mark.asyncio
async def test_queue_list_sends_every_link_when_one_response_is_too_long(monkeypatch) -> None:
    monkeypatch.setattr(command_ui, "MAX_SLACK_TEXT_LENGTH", 120)
    channel, _ = _channel()
    channel._identity.host = "acme.slack.com"
    entries = [_waiting(stamp=f"1790600000.{index:06d}") for index in range(1, 5)]
    channel._queued_messages[entries[0].session_id] = entries

    _, respond = await _run(channel, _command("queue list", channel_id=ROOM))
    assert len(respond.calls) > 1
    output = "\n".join(call["text"] for call in respond.calls)
    for entry in entries:
        assert entry.message_ts.replace(".", "") in output
    assert all(len(call["text"]) <= 120 for call in respond.calls)
    assert all("blocks" not in call for call in respond.calls)


@pytest.mark.asyncio
async def test_queue_drop_removes_own_prompt_and_both_reactions() -> None:
    client = _RecordingSlackClient()
    channel, _ = _channel(client=client, acknowledge_mode="both")
    channel._identity.host = "acme.slack.com"
    entry = _waiting()
    channel._queued_messages[entry.session_id] = [entry]
    link = "https://acme.slack.com/archives/C0CHANNEL1/p1790600000000001"

    _, respond = await _run(channel, _command(f"queue drop {link}", channel_id=ROOM))
    assert "will not start" in respond.text
    assert entry.session_id not in channel._queued_messages
    removed = {call["name"] for call in client.removed_reactions}
    assert removed == {"hourglass_flowing_sand", "eyes"}
    assert client.statuses[-1]["data"]["status"] == ""


@pytest.mark.asyncio
async def test_queue_drop_picker_shows_safe_preview_and_removes_selected_message() -> None:
    channel, _ = _channel()
    channel._identity.host = "acme.slack.com"
    first = _waiting()
    first.request = SimpleNamespace(id="queue-one")  # type: ignore[assignment]
    first.preview_text = "<@U0SOMEONE> Check the backup job before noon"
    second = _waiting(stamp="1790600000.000002")
    second.request = SimpleNamespace(id="queue-two")  # type: ignore[assignment]
    channel._queued_messages[first.session_id] = [first, second]

    _, picker = await _run(channel, _command("queue drop", channel_id=ROOM))
    options = picker.calls[0]["blocks"][-1]["elements"][0]["options"]
    assert "Check the backup job" in options[0]["text"]["text"]
    assert "<@" not in options[0]["text"]["text"]
    assert options[0]["description"]["text"].endswith("UTC")
    assert "confirm" in picker.calls[0]["blocks"][-1]["elements"][0]

    body, action = _picker_click(picker)
    ack, result = _Recorder(), _Recorder()
    await channel._handle_slash_queue_drop_select(ack, body, action, result)

    assert ack.calls == [{}]
    assert channel._queued_messages[first.session_id] == [second]
    assert "https://acme.slack.com/archives/C0CHANNEL1/p1790600000000001" in result.text
    assert result.calls[0]["replace_original"] is True


@pytest.mark.asyncio
async def test_queue_drop_picker_rechecks_stale_and_cross_channel_choice() -> None:
    channel, _ = _channel()
    entry = _waiting()
    entry.request = SimpleNamespace(id="queue-one")  # type: ignore[assignment]
    channel._queued_messages[entry.session_id] = [entry]
    _, picker = await _run(channel, _command("queue drop", channel_id=ROOM))
    body, action = _picker_click(picker)

    foreign = dict(body, channel={"id": OTHER_ROOM})
    ack, response = _Recorder(), _Recorder()
    await channel._handle_slash_queue_drop_select(ack, foreign, action, response)
    assert ack.calls == [{}]
    assert not response.calls
    assert channel._queued_messages[entry.session_id] == [entry]

    wrong_team = dict(body, team={"id": "T0OTHERTEAM"})
    ack, response = _Recorder(), _Recorder()
    await channel._handle_slash_queue_drop_select(ack, wrong_team, action, response)
    assert response.text == slack_commands.foreign_workspace_notice(BOT_NAME)
    assert channel._queued_messages[entry.session_id] == [entry]

    entry.request = SimpleNamespace(id="replacement")  # type: ignore[assignment]
    ack, response = _Recorder(), _Recorder()
    await channel._handle_slash_queue_drop_select(ack, body, action, response)
    assert "not waiting" in response.text
    assert channel._queued_messages[entry.session_id] == [entry]

    channel._queued_messages.clear()
    ack, response = _Recorder(), _Recorder()
    await channel._handle_slash_queue_drop_select(ack, body, action, response)
    assert "not waiting" in response.text


@pytest.mark.asyncio
async def test_queue_drop_picker_lists_only_entries_the_user_can_remove() -> None:
    channel, _ = _channel()
    own = _waiting()
    other = _waiting(user=OUTSIDER, stamp="1790600000.000002")
    channel._queued_messages[own.session_id] = [own, other]

    _, picker = await _run(channel, _command("queue drop", channel_id=ROOM))
    options = picker.calls[0]["blocks"][-1]["elements"][0]["options"]
    assert len(options) == 1


@pytest.mark.asyncio
async def test_queue_drop_picker_rechecks_sender_on_click() -> None:
    channel, _ = _channel()
    entry = _waiting()
    channel._queued_messages[entry.session_id] = [entry]
    _, picker = await _run(channel, _command("queue drop", channel_id=ROOM))
    body, action = _picker_click(picker)

    entry.user_id = OUTSIDER
    result = _Recorder()
    await channel._handle_slash_queue_drop_select(_Recorder(), body, action, result)
    assert "Only the sender" in result.text
    assert channel._queued_messages[entry.session_id] == [entry]


@pytest.mark.asyncio
async def test_queue_drop_picker_names_overflow_and_keeps_typed_links() -> None:
    channel, _ = _channel()
    entries = [
        _waiting(stamp=f"1790600000.{index:06d}") for index in range(1, 102)
    ]
    channel._queued_messages[entries[0].session_id] = entries

    _, picker = await _run(channel, _command("queue drop", channel_id=ROOM))
    assert "More messages exist" in picker.text
    assert len(picker.calls[0]["blocks"][-1]["elements"][0]["options"]) == 100


@pytest.mark.asyncio
async def test_queue_drop_keeps_queued_status_when_another_message_waits() -> None:
    client = _RecordingSlackClient()
    channel, _ = _channel(client=client, acknowledge_mode="both")
    channel._identity.host = "acme.slack.com"
    first = _waiting()
    second = _waiting(stamp="1790600000.000002")
    channel._queued_messages[first.session_id] = [first, second]
    link = "https://acme.slack.com/archives/C0CHANNEL1/p1790600000000001"

    await _run(channel, _command(f"queue drop {link}", channel_id=ROOM))
    assert channel._queued_messages[first.session_id] == [second]
    assert client.statuses[-1]["data"]["status"] == channel.config.queued_status


@pytest.mark.asyncio
async def test_queue_drop_restores_running_status_for_last_waiter() -> None:
    client = _RecordingSlackClient()
    channel, _ = _channel(
        client=client, acknowledge_mode="both", thinking_status="is thinking..."
    )
    channel._identity.host = "acme.slack.com"
    entry = _waiting()
    channel._queued_messages[entry.session_id] = [entry]
    channel._admission.turn_initiators[entry.session_id] = SlackTurnInitiator(
        user_id=USER, request_id="running", is_dm=False, chat_type="channel",
        channel_id=ROOM, reply_thread_ts=entry.reply_thread_ts,
        root_thread_ts=entry.root_thread_ts,
    )
    link = "https://acme.slack.com/archives/C0CHANNEL1/p1790600000000001"

    await _run(channel, _command(f"queue drop {link}", channel_id=ROOM))
    assert entry.session_id in channel._admission.turn_initiators
    assert client.statuses[-1]["data"]["status"] == channel.config.thinking_status


@pytest.mark.asyncio
async def test_queue_drop_does_not_add_status_when_acknowledgements_are_off() -> None:
    client = _RecordingSlackClient()
    channel, _ = _channel(client=client, thinking_status="is thinking...")
    channel._identity.host = "acme.slack.com"
    entry = _waiting()
    channel._queued_messages[entry.session_id] = [entry]
    link = "https://acme.slack.com/archives/C0CHANNEL1/p1790600000000001"

    await _run(channel, _command(f"queue drop {link}", channel_id=ROOM))
    assert not client.statuses


@pytest.mark.asyncio
async def test_queue_drop_denies_other_sender_without_separate_rule() -> None:
    channel, _ = _channel()
    channel._identity.host = "acme.slack.com"
    entry = _waiting(user=OUTSIDER)
    channel._queued_messages[entry.session_id] = [entry]
    link = "https://acme.slack.com/archives/C0CHANNEL1/p1790600000000001"

    _, respond = await _run(channel, _command(f"queue drop {link}", channel_id=ROOM))
    assert "Only the sender" in respond.text
    assert channel._queued_messages[entry.session_id] == [entry]


@pytest.mark.asyncio
async def test_queue_drop_separate_rule_can_allow_other_sender() -> None:
    channel, _ = _channel(**_scoped_config(entries=[
        {"match": {"channel": "slack", "chat": ROOM},
         "clicks": {"queue_drop": {"user": [USER]}}},
    ]))
    channel._identity.host = "acme.slack.com"
    entry = _waiting(user=OUTSIDER)
    channel._queued_messages[entry.session_id] = [entry]
    link = "https://acme.slack.com/archives/C0CHANNEL1/p1790600000000001"

    _, respond = await _run(channel, _command(f"queue drop {link}", channel_id=ROOM))
    assert "will not start" in respond.text
    assert entry.session_id not in channel._queued_messages


@pytest.mark.asyncio
async def test_queue_drop_rejects_a_link_to_another_conversation() -> None:
    channel, _ = _channel()
    channel._identity.host = "acme.slack.com"
    entry = _waiting()
    channel._queued_messages[entry.session_id] = [entry]
    links = (
        "https://other.slack.com/archives/C0CHANNEL1/p1790600000000001",
        "https://acme.slack.com/archives/C0CHANNEL2/p1790600000000001",
    )
    for link in links:
        _, respond = await _run(channel, _command(f"queue drop {link}", channel_id=ROOM))
        assert "link from this conversation" in respond.text
        assert channel._queued_messages[entry.session_id] == [entry]


@pytest.mark.asyncio
async def test_queue_drop_accepts_a_long_slack_timestamp() -> None:
    channel, _ = _channel()
    channel._identity.host = "acme.slack.com"
    entry = _waiting(stamp="11790600000.000001")
    channel._queued_messages[entry.session_id] = [entry]
    link = f"https://acme.slack.com/archives/{ROOM}/p{entry.message_ts.replace('.', '')}"

    _, respond = await _run(channel, _command(f"queue drop {link}", channel_id=ROOM))

    assert "will not start" in respond.text
    assert entry.session_id not in channel._queued_messages


def test_the_manifest_usage_hint_names_every_subcommand() -> None:
    hint = COMMAND_REQUIREMENTS[MAIN_COMMAND].usage_hint
    for sub in slack_commands.SUBCOMMANDS:
        assert " ".join(filter(None, (sub.name, sub.arguments))) in hint


@pytest.fixture
def models(monkeypatch):
    known = belle_config.ConfiguredModels(
        names=("default", "fast"), aliases=("quick",),
        alias_pairs=(("quick", "fast"),),
    )
    monkeypatch.setattr(slash_handlers, "configured_models", lambda: known)
    monkeypatch.setattr(slack_inbound, "configured_models", lambda: known)
    monkeypatch.setattr(belle_config, "configured_models", lambda: known)
    return known


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", [
        "status", "models list", "skills list", "subagents list",
        "experts list", "queue list",
    ],
)
async def test_status_and_list_commands_have_no_controls(command: str, models) -> None:
    channel, _ = _channel(
        reply=(True, {"skills": CATALOG}),
        subagents_reply=(True, {"subagents": ["code_agent"]}),
        expert_reply=(True, {"templates": EXPERT_CATALOG}),
    )
    _, response = await _run(channel, _command(command))

    assert response.calls
    assert all("blocks" not in call for call in response.calls)
    assert not channel._slash_task_forms


@pytest.mark.asyncio
async def test_models_list_current_use_reset_in_dm(models) -> None:
    channel, _ = _channel()
    _, listed = await _run(channel, _command("models list"))
    assert "`fast` (aliases: quick)" in listed.text
    _, current = await _run(channel, _command("models current"))
    assert "`default` (configured default)" in current.text

    _, selected = await _run(channel, _command("models use quick"))
    assert "everyone in this conversation" in selected.text
    assert "after a restart" in selected.text
    assert selected.calls[0]["response_type"] == "ephemeral"
    assert channel._channel_model_name(DM, USER, "im") == "quick"
    _, current = await _run(channel, _command("models current"))
    assert "`quick` (temporary selection)" in current.text

    _, reset = await _run(channel, _command("models reset"))
    assert "`default` (configured default)" in reset.text
    assert channel._channel_model_name(DM, USER, "im") == ""
    assert not channel._model_overrides


@pytest.mark.asyncio
async def test_models_list_is_read_only_and_bare_use_offers_ephemeral_picker(models) -> None:
    channel, _ = _channel()
    ack, listed = await _run(channel, _command("models list"))
    assert ack.calls == [{}]
    assert "`fast`" in listed.text
    assert listed.calls[0]["response_type"] == "ephemeral"
    assert all("blocks" not in call for call in listed.calls)

    ack, response = await _run(channel, _command("models use"))
    assert ack.calls == [{}]
    payload = response.calls[0]
    assert payload["response_type"] == "ephemeral"
    assert payload["text"]
    blocks = payload["blocks"]
    assert blocks[1]["elements"][0]["type"] == "external_select"
    assert "unless a scope" in blocks[1]["elements"][0]["confirm"]["text"]["text"]
    assert not channel._model_overrides


@pytest.mark.asyncio
async def test_model_picker_options_are_searched_and_gated(models) -> None:
    channel, _ = _channel(allow_from=[USER])
    body = {
        "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": DM}, "value": "fast",
    }
    ack = _Recorder()
    await channel._handle_model_select_options(ack, body)
    assert ack.calls[0]["options"] == [
        {"text": {"type": "plain_text", "text": "fast"}, "value": "fast"}
    ]

    denied = _Recorder()
    await channel._handle_model_select_options(
        denied, {**body, "user": {"id": OUTSIDER}},
    )
    assert denied.calls == [{"options": []}]

    foreign = _Recorder()
    await channel._handle_model_select_options(
        foreign, {**body, "team": {"id": "T0FOREIGN"}},
    )
    assert foreign.calls == [{"options": []}]

    scoped, _ = _channel(**_scoped_config(entries=[
        {"match": {"channel": "slack"}, "agent": {"model_name": "default"}},
    ]))
    scoped_ack = _Recorder()
    await scoped._handle_model_select_options(scoped_ack, body)
    assert scoped_ack.calls == [{"options": []}]


@pytest.mark.asyncio
async def test_model_picker_search_reaches_names_past_slacks_page_limit(monkeypatch) -> None:
    names = tuple(f"model-{index:03d}" for index in range(120))
    monkeypatch.setattr(
        slash_handlers, "configured_models",
        lambda: belle_config.ConfiguredModels(names=names),
    )
    channel, _ = _channel()
    body = {
        "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": DM}, "value": "model-119",
    }
    ack = _Recorder()
    await channel._handle_model_select_options(ack, body)
    assert [item["value"] for item in ack.calls[0]["options"]] == ["model-119"]


@pytest.mark.asyncio
async def test_model_picker_action_rechecks_selected_model_and_scope(models, monkeypatch) -> None:
    channel, _ = _channel()
    body = {"team": {"id": TEAM}, "user": {"id": USER},
            "channel": {"id": DM}}
    action = {"selected_option": {"value": "fast"}}
    ack, respond = _Recorder(), _Recorder()
    await channel._handle_model_select_action(ack, body, action, respond)
    assert ack.calls == [{}]
    assert respond.calls[0]["replace_original"] is True
    assert "everyone in this conversation" in respond.text
    assert channel._channel_model_name(DM, USER, "im") == "fast"

    monkeypatch.setattr(
        slash_handlers, "configured_models",
        lambda: belle_config.ConfiguredModels(names=("default",)),
    )
    ack, respond = _Recorder(), _Recorder()
    await channel._handle_model_select_action(ack, body, action, respond)
    assert "Unknown model" in respond.text
    assert not channel._model_overrides

    scoped, _ = _channel(**_scoped_config(entries=[
        {"match": {"channel": "slack"}, "agent": {"model_name": "default"}},
    ]))
    ack, respond = _Recorder(), _Recorder()
    await scoped._handle_model_select_action(ack, body, action, respond)
    assert "scope selects" in respond.text
    assert not scoped._model_overrides


@pytest.mark.asyncio
async def test_model_picker_action_refuses_a_newly_disallowed_user(models) -> None:
    channel, _ = _channel(allow_from=[USER])
    body = {
        "team": {"id": TEAM}, "user": {"id": OUTSIDER},
        "container": {"channel_id": DM},
    }
    ack, respond = _Recorder(), _Recorder()
    await channel._handle_model_select_action(
        ack, body, {"selected_option": {"value": "fast"}}, respond,
    )
    assert ack.calls == [{}]
    assert respond.text == slack_commands.not_permitted_notice(BOT_NAME)
    assert not channel._model_overrides


@pytest.mark.asyncio
async def test_models_reset_sends_default_on_next_turn(models) -> None:
    channel, routed = _channel()
    await _run(channel, _command("models use fast"))
    await channel._route_message_event(
        {"type": "message", "channel_type": "im", "channel": DM,
         "user": USER, "text": "first", "ts": "1780000000.000001"},
        {"team_id": TEAM},
    )
    first = _of(routed, ReqMethod.CHAT_SEND)[-1]
    assert first.params["model_name"] == "fast"

    await _run(channel, _command("models reset"))
    await channel._route_message_event(
        {"type": "message", "channel_type": "im", "channel": DM,
         "user": USER, "text": "next", "ts": "1780000000.000002"},
        {"team_id": TEAM},
    )
    queued = channel._queued_messages[first.session_id][0]
    assert queued.request.params["model_name"] == "default"


@pytest.mark.asyncio
async def test_models_selection_is_shared_in_channel_but_not_other_rooms(models) -> None:
    channel, _ = _channel()
    _, selected = await _run(channel, _command("models use fast", channel_id=ROOM))
    assert "everyone" in selected.text
    assert channel._channel_model_name(ROOM, USER, "channel") == "fast"
    assert channel._channel_model_name(ROOM, OUTSIDER, "channel") == "fast"
    assert channel._channel_model_name(DM, USER, "im") == ""
    assert channel._channel_model_name(OTHER_ROOM, USER, "channel") == ""
    # A new connector has no selection, even with the same config.
    restarted, _ = _channel()
    assert restarted._channel_model_name(ROOM, USER, "channel") == ""


@pytest.mark.asyncio
async def test_models_use_refuses_a_valid_scope_pin(models) -> None:
    channel, _ = _channel(**_scoped_config(entries=[
        {"match": {"channel": "slack", "chat": ROOM},
         "agent": {"model_name": "default"}},
    ]))
    _, refused = await _run(channel, _command("models use fast", channel_id=ROOM))
    assert "scope selects" in refused.text
    assert channel._channel_model_name(ROOM, USER, "channel") == "default"
    assert not channel._model_overrides
    _, current = await _run(channel, _command("models current", channel_id=ROOM))
    assert "`default` (scope)" in current.text


@pytest.mark.asyncio
async def test_models_use_refuses_unknown_and_unreadable_names(models, monkeypatch) -> None:
    channel, _ = _channel()
    _, unknown = await _run(channel, _command("models use missing"))
    assert "Unknown model" in unknown.text
    assert not channel._model_overrides
    monkeypatch.setattr(slash_handlers, "configured_models", lambda: belle_config.ConfiguredModels())
    _, unreadable = await _run(channel, _command("models use fast"))
    assert "unavailable" in unreadable.text
    assert not channel._model_overrides


@pytest.mark.asyncio
async def test_models_use_keeps_queued_and_running_request_models(models) -> None:
    channel, routed = _channel()
    await _run(channel, _command("models use default"))
    await channel._route_message_event(
        {"type": "message", "channel_type": "im", "channel": DM,
         "user": USER, "text": "first", "ts": "1780000000.000001"},
        {"team_id": TEAM},
    )
    first = _of(routed, ReqMethod.CHAT_SEND)[-1]
    assert first.params["model_name"] == "default"
    entry = channel.turn_initiator(first.session_id)
    assert entry is not None and entry.model_name == "default"

    await _run(channel, _command("models use fast"))
    assert entry.model_name == "default"
    assert first.params["model_name"] == "default"
    await channel._route_message_event(
        {"type": "message", "channel_type": "im", "channel": DM,
         "user": USER, "text": "next", "ts": "1780000000.000002"},
        {"team_id": TEAM},
    )
    held = channel._queued_messages[first.session_id][0]
    assert held.request.params["model_name"] == "fast"
    await _run(channel, _command("models use default"))
    channel._admission.turn_initiators.pop(first.session_id)
    await channel._drain_queued_messages(first.session_id)
    assert _of(routed, ReqMethod.CHAT_SEND)[-1].params["model_name"] == "fast"


@pytest.mark.asyncio
async def test_models_use_obeys_slash_permissions(models) -> None:
    channel, _ = _channel(allow_from=[USER])
    ack, _ = await _run(channel, _command("models use fast", user_id=OUTSIDER))
    assert ack.text == slack_commands.not_permitted_notice(BOT_NAME)
    assert not channel._model_overrides


def _start_stop_target(
    channel: SlackChannel,
    *,
    user_id: str = USER,
    channel_id: str = DM,
    message_ts: str = "1790000000.000001",
    request_id: str = "turn-1",
) -> str:
    is_dm = channel_id == DM
    session_id = (
        f"slack_{TEAM}_{channel_id}_{user_id}"
        if is_dm else f"slack_{TEAM}_{channel_id}_{message_ts}"
    )
    channel._remember_turn_initiator(
        session_id, user_id, request_id,
        is_dm=is_dm, chat_type="im" if is_dm else "channel",
        origin=SlackMessageOrigin(
            channel_id=channel_id, message_ts=message_ts,
            # A DM is answered at top level, so it has no reply target. Its
            # status still opens on the message's own root.
            reply_thread_ts="" if is_dm else message_ts,
            root_thread_ts=message_ts,
        ),
    )
    return session_id


@pytest.mark.asyncio
async def test_stop_command_cancels_one_running_turn_without_a_card() -> None:
    channel, routed = _channel()
    session_id = _start_stop_target(channel)

    ack, picker = await _run(channel, _command("stop"))

    assert ack.calls == [{}]
    assert "Select the turn" in picker.text
    assert not routed
    body, action = _picker_click(picker, channel_id=DM)
    click_ack, respond = _Recorder(), _Recorder()
    await channel._handle_slash_stop_select(click_ack, body, action, respond)
    assert click_ack.calls == [{}]
    assert respond.text == slack_commands.STOP_ACCEPTED_NOTICE
    assert [msg.session_id for msg in routed if msg.req_method is ReqMethod.CHAT_CANCEL] == [
        session_id
    ]
    assert channel.turn_initiator(session_id) is None


@pytest.mark.asyncio
async def test_stop_command_that_fails_to_send_can_be_run_again() -> None:
    channel, routed = _channel()
    session_id = _start_stop_target(channel)
    route = channel._route_request

    async def fail_once(_req: Message) -> None:
        channel._route_request = route  # type: ignore[method-assign]
        raise RuntimeError("bus down")

    channel._route_request = fail_once  # type: ignore[method-assign]

    _ack, picker = await _run(channel, _command("stop"))
    body, action = _picker_click(picker, channel_id=DM)
    respond = _Recorder()
    await channel._handle_slash_stop_select(_Recorder(), body, action, respond)
    assert respond.text == slack_commands.FAILED_NOTICE
    assert channel.turn_initiator(session_id) is not None

    _ack, picker = await _run(channel, _command("stop"))
    body, action = _picker_click(picker, channel_id=DM)
    respond = _Recorder()
    await channel._handle_slash_stop_select(_Recorder(), body, action, respond)

    assert respond.text == slack_commands.STOP_ACCEPTED_NOTICE
    assert [msg.session_id for msg in routed if msg.req_method is ReqMethod.CHAT_CANCEL] == [
        session_id
    ]
    assert channel.turn_initiator(session_id) is None


@pytest.mark.asyncio
async def test_stop_command_takes_the_activity_card_down() -> None:
    """The subcommand ends a turn on purpose, so it ends the card the same way.

    The two gestures reach one method. Whatever found the turn -- a button that
    named its request, or a picker that named its message -- the card is settled
    by ``_settle_stopped_card``, and a settled stop has no card. A subcommand
    that rewrote a card the button deletes would be two answers to one question.
    """
    client = _RecordingSlackClient()
    channel, _routed = _channel(client=client, activity_card=True)
    session_id = _start_stop_target(channel)
    record = slack_connect._SlackActivityRecord(
        request_id="turn-1", channel_id=DM, thread_ts="", session_id=session_id,
    )
    record.message_ts = "1790000000.000009"
    channel._activity_records[("turn-1", DM)] = record

    _ack, picker = await _run(channel, _command("stop"))
    body, action = _picker_click(picker, channel_id=DM)
    await channel._handle_slash_stop_select(
        _Recorder(), body, action, _Recorder()
    )

    assert client.deleted == [(DM, "1790000000.000009")]
    assert client.updates == []
    assert record.stopped is True


@pytest.mark.asyncio
async def test_stop_command_keeps_the_card_of_a_turn_that_failed() -> None:
    """The one card a stop cannot take down, whichever gesture asked.

    The card holds the only copy of the harness error: the message that would
    have reported it is suppressed on the strength of the card showing it. So
    ``failed`` vetoes every reason to discard, and this path is no exception to
    a rule the other two obey.
    """
    client = _RecordingSlackClient()
    channel, _routed = _channel(client=client, activity_card=True)
    session_id = _start_stop_target(channel)
    record = slack_connect._SlackActivityRecord(
        request_id="turn-1", channel_id=DM, thread_ts="", session_id=session_id,
    )
    record.message_ts = "1790000000.000009"
    record.failed = True
    record.harness_error = "the model call timed out"
    channel._activity_records[("turn-1", DM)] = record

    _ack, picker = await _run(channel, _command("stop"))
    body, action = _picker_click(picker, channel_id=DM)
    await channel._handle_slash_stop_select(
        _Recorder(), body, action, _Recorder()
    )

    assert client.deleted == []
    assert len(client.updates) == 1
    assert record.stopped is True


@pytest.mark.asyncio
async def test_stop_command_requires_a_link_when_two_turns_run_here() -> None:
    channel, routed = _channel()
    first = _start_stop_target(channel, channel_id=ROOM)
    second = _start_stop_target(
        channel, channel_id=ROOM, message_ts="1790000000.000002", request_id="turn-2"
    )
    channel._identity.host = "example.slack.com"

    _, picker = await _run(channel, _command("stop", channel_id=ROOM))
    assert len(picker.calls[0]["blocks"][-1]["elements"][0]["options"]) == 2
    assert not routed

    link = "https://example.slack.com/archives/C0CHANNEL1/p1790000000000001"
    _, selected = await _run(channel, _command(f"stop {link}", channel_id=ROOM))
    assert selected.text == slack_commands.STOP_ACCEPTED_NOTICE
    assert [msg.session_id for msg in routed if msg.req_method is ReqMethod.CHAT_CANCEL] == [
        first
    ]
    assert channel.turn_initiator(second) is not None


@pytest.mark.asyncio
async def test_stop_command_rejects_a_link_from_another_conversation() -> None:
    channel, routed = _channel()
    _start_stop_target(channel)
    channel._identity.host = "example.slack.com"

    _, respond = await _run(
        channel,
        _command("stop https://example.slack.com/archives/C0CHANNEL1/p1790000000000001"),
    )

    assert respond.text == slack_commands.STOP_LINK_INVALID_NOTICE
    assert not routed


@pytest.mark.asyncio
async def test_stop_command_starter_is_allowed_outside_allow_from() -> None:
    channel, routed = _channel(allow_from=[OUTSIDER])
    _start_stop_target(channel)

    _, picker = await _run(channel, _command("stop"))
    body, action = _picker_click(picker, channel_id=DM)
    respond = _Recorder()
    await channel._handle_slash_stop_select(_Recorder(), body, action, respond)

    assert respond.text == slack_commands.STOP_ACCEPTED_NOTICE
    assert len([msg for msg in routed if msg.req_method is ReqMethod.CHAT_CANCEL]) == 1


@pytest.mark.asyncio
async def test_stop_picker_rejects_replaced_turn_and_foreign_team() -> None:
    channel, routed = _channel()
    session_id = _start_stop_target(channel, channel_id=ROOM)
    _, picker = await _run(channel, _command("stop", channel_id=ROOM))
    body, action = _picker_click(picker)

    foreign = dict(body, team={"id": "T0OTHERTEAM"})
    response = _Recorder()
    await channel._handle_slash_stop_select(_Recorder(), foreign, action, response)
    assert response.text == slack_commands.foreign_workspace_notice(BOT_NAME)
    assert not routed

    _start_stop_target(channel, channel_id=ROOM, request_id="replacement")
    response = _Recorder()
    await channel._handle_slash_stop_select(_Recorder(), body, action, response)
    assert response.text == slack_commands.STOP_NONE_NOTICE
    assert channel.turn_initiator(session_id).request_id == "replacement"
    assert not routed


@pytest.mark.asyncio
async def test_stop_picker_rechecks_starter_permission_on_click() -> None:
    channel, routed = _channel(allow_from=[OUTSIDER])
    session_id = _start_stop_target(channel, channel_id=ROOM)
    _, picker = await _run(channel, _command("stop", channel_id=ROOM))
    body, action = _picker_click(picker)

    channel.turn_initiator(session_id).user_id = OUTSIDER
    response = _Recorder()
    await channel._handle_slash_stop_select(_Recorder(), body, action, response)
    assert response.text == slack_commands.not_permitted_notice(BOT_NAME)
    assert not routed


@pytest.mark.asyncio
async def test_stop_command_does_not_extend_starter_allowance_to_a_stranger() -> None:
    channel, routed = _channel(allow_from=[USER])
    _start_stop_target(channel)

    ack, respond = await _run(channel, _command("stop", user_id=OUTSIDER))

    assert ack.text == slack_commands.not_permitted_notice(BOT_NAME)
    assert not respond.calls
    assert not routed


@pytest.mark.asyncio
async def test_stop_command_starter_picker_filters_others_outside_allow_from() -> None:
    channel, routed = _channel(allow_from=[OUTSIDER])
    _start_stop_target(channel, channel_id=ROOM)
    other_session = _start_stop_target(
        channel, user_id=OUTSIDER, channel_id=ROOM,
        message_ts="1790000000.000002", request_id="turn-2"
    )

    ack, respond = await _run(channel, _command("stop", channel_id=ROOM))

    assert ack.calls == [{}]
    assert len(respond.calls[0]["blocks"][-1]["elements"][0]["options"]) == 1
    body, action = _picker_click(respond)
    await channel._handle_slash_stop_select(_Recorder(), body, action, _Recorder())
    assert len([msg for msg in routed if msg.req_method is ReqMethod.CHAT_CANCEL]) == 1
    assert channel.turn_initiator(other_session) is not None


@pytest.mark.asyncio
async def test_stop_command_starter_does_not_bypass_disabled_channel() -> None:
    channel, routed = _channel(
        allow_from=[OUTSIDER], allowed_channel_ids=[OTHER_ROOM]
    )
    _start_stop_target(channel, channel_id=ROOM)

    ack, respond = await _run(channel, _command("stop", channel_id=ROOM))

    assert ack.text == slack_commands.conversation_not_enabled_notice(BOT_NAME)
    assert not respond.calls
    assert not routed


@pytest.mark.asyncio
async def test_stop_command_obeys_clicks_stop_for_another_user() -> None:
    scopes = compile_scopes(
        [{"match": {"channel": "slack", "chat": ROOM},
          "clicks": {"stop": {"role": "operator"}}}],
        people={"operator": {"slack": "U0OPERATOR"}},
        roles={"operator": ["operator"]},
        warn=lambda *_args: None,
    )
    channel, routed = _channel(scopes=scopes, allow_from=[USER, OUTSIDER])
    _start_stop_target(channel, channel_id=ROOM)

    _, respond = await _run(channel, _command("stop", channel_id=ROOM, user_id=OUTSIDER))

    assert respond.text == slack_commands.STOP_REFUSED_NOTICE
    assert not routed


# ── help ────────────────────────────────────────────────────────────────────


def _jiuwenswarm_help() -> str:
    unavailable = {
        sub: "this host does not provide scheduling"
        for sub in slack_commands.SUBCOMMANDS
        if sub.words[0] in {"schedule", "sessions", "bg"}
    }
    return slack_commands.help_text(COMMAND, bot_name=BOT_NAME, unavailable=unavailable)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["help", "", "HELP"])
async def test_help_is_answered_ephemerally_in_the_acknowledgement(text: str) -> None:
    channel, routed = _channel()
    ack, respond = await _run(channel, _command(text))
    assert ack.text == _jiuwenswarm_help()
    assert ack.calls[0]["response_type"] == "ephemeral"
    assert not respond.calls and not routed


@pytest.mark.asyncio
async def test_help_in_an_allowed_channel() -> None:
    channel, _ = _channel(allowed_channel_ids=[ROOM])
    ack, _ = await _run(channel, _command(channel_id=ROOM, channel_name="general"))
    assert ack.text == _jiuwenswarm_help()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_id", "info", "kind"),
    [
        (ROOM, {"id": ROOM, "is_channel": True}, "channel"),
        (ROOM, {"id": ROOM, "is_channel": True, "is_private": True}, "group"),
        ("G0GROUPDM", {"id": "G0GROUPDM", "is_mpim": True}, "mpim"),
    ],
)
async def test_cold_slash_uses_verified_chat_type_for_skill_scope(
    chat_id: str, info: dict[str, Any], kind: str
) -> None:
    client = _RecordingSlackClient(info=info)
    channel, routed = _channel(
        client=client, warm_chat_types=False,
        reply=(True, {"skills": CATALOG}),
        **_scoped_config(entries=[
            {"match": {"channel": "slack", "chat_type": kind},
             "agent": {"skills": {"available": ["ambient"]}}},
        ]),
    )
    channel._granted_scopes = frozenset({"channels:read", "groups:read", "mpim:read"})

    ack, respond = await _run(channel, _command("skills list", channel_id=chat_id))

    assert ack.calls == [{}]
    assert client.info_calls == [chat_id]
    assert channel._chat_types[chat_id] == kind
    assert "`ambient`" in respond.text
    assert "research" not in respond.text
    assert _of(routed, ReqMethod.SKILLS_LIST)[0].metadata["slack_channel_type"] == kind


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["status", "skills list", "subagents list", "experts list", "models list", "models current", "models use fast", "models reset", "skills use research task", "subagents use code_agent task", "ask task"])
async def test_cold_slash_refuses_scope_sensitive_command_without_read_scope(
    text: str,
) -> None:
    client = _RecordingSlackClient()
    channel, routed = _channel(client=client, warm_chat_types=False)
    channel._granted_scopes = frozenset()

    ack, respond = await _run(channel, _command(text, channel_id=ROOM))

    assert ack.calls == [{}]
    assert respond.text == slack_commands.conversation_kind_unknown_notice(BOT_NAME)
    assert not client.info_calls and not client.posts and not routed


@pytest.mark.asyncio
async def test_cold_slash_refuses_when_info_does_not_verify_channel() -> None:
    client = _RecordingSlackClient(info={"id": OTHER_ROOM, "is_channel": True})
    channel, routed = _channel(client=client, warm_chat_types=False)
    channel._granted_scopes = frozenset({"channels:read"})

    ack, respond = await _run(channel, _command("status", channel_id=ROOM))

    assert ack.calls == [{}]
    assert client.info_calls == [ROOM]
    assert respond.text == slack_commands.conversation_kind_unknown_notice(BOT_NAME)
    assert not routed


@pytest.mark.asyncio
async def test_an_unknown_subcommand_gets_usage_without_its_own_text_echoed() -> None:
    channel, _ = _channel()
    ack, _ = await _run(channel, _command("<!channel> pwned"))
    assert ack.text == slack_commands.unknown_text(COMMAND, bot_name=BOT_NAME)
    assert "<!channel>" not in ack.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "sub"),
    [
        ("help me", slack_commands.HELP),
        ("skills list everything", slack_commands.SKILLS_LIST),
        ("experts list everything", slack_commands.EXPERTS_LIST),
    ],
)
async def test_wrong_arguments_get_the_usage_line(text: str, sub: Any) -> None:
    channel, routed = _channel()
    ack, _ = await _run(channel, _command(text))
    assert ack.text == slack_commands.usage_text(sub, COMMAND)
    assert not routed and not channel._client.posts


# ── refusals ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["help", "skills list", "experts list", "ask hello"])
async def test_another_workspace_is_refused(text: str) -> None:
    channel, routed = _channel()
    ack, _ = await _run(channel, _command(text, team_id="T0SOMEONE9"))
    assert ack.text == slack_commands.foreign_workspace_notice(BOT_NAME)
    assert not routed and not channel._client.posts


@pytest.mark.asyncio
async def test_a_connection_that_has_not_learned_its_workspace_refuses() -> None:
    channel, _ = _channel()
    channel._identity.team_id = ""
    ack, _ = await _run(channel, _command())
    assert ack.text == slack_commands.foreign_workspace_notice(BOT_NAME)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["help", "skills list", "experts list", "ask hello"])
async def test_a_user_outside_allow_from_is_refused(text: str) -> None:
    channel, routed = _channel(allow_from=[USER])
    ack, _ = await _run(channel, _command(text, user_id=OUTSIDER))
    assert ack.text == slack_commands.not_permitted_notice(BOT_NAME)
    assert not routed and not channel._client.posts


@pytest.mark.asyncio
async def test_a_channel_outside_allowed_channel_ids_is_refused() -> None:
    channel, routed = _channel(allowed_channel_ids=[ROOM])
    ack, _ = await _run(
        channel, _command("skills list", channel_id=OTHER_ROOM, channel_name="random")
    )
    assert ack.text == slack_commands.conversation_not_enabled_notice(BOT_NAME)
    assert not routed


@pytest.mark.asyncio
async def test_allowed_channel_ids_does_not_restrict_a_dm() -> None:
    channel, _ = _channel(allowed_channel_ids=[ROOM])
    ack, _ = await _run(channel, _command())
    assert ack.text == _jiuwenswarm_help()


# ── skills list ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skills_list_asks_the_runtime_and_answers_privately() -> None:
    channel, routed = _channel(reply=(True, {"skills": CATALOG}))
    ack, respond = await _run(channel, _command("skills list"))

    assert ack.calls == [{}], "acknowledged empty, answered afterwards"
    (req,) = routed
    assert req.req_method is ReqMethod.SKILLS_LIST
    assert req.is_stream is False
    assert req.metadata["slack_team_id"] == TEAM
    assert RESPONSE_URL not in repr(req)

    assert respond.calls[0]["response_type"] == "ephemeral"
    assert "blocks" not in respond.calls[0]
    text = respond.text
    assert "`research`: Find and summarise sources\n" in text
    assert "`ambient`" in text
    for hidden in ("switched-off", "marketplace-only"):
        assert hidden not in text
    # The reply answered the command; it was not posted as a turn's reply.
    assert not channel._client.posts
    assert not channel._agent_replies


@pytest.mark.asyncio
async def test_skills_list_is_narrowed_by_the_conversation_scope() -> None:
    channel, _ = _channel(
        reply=(True, {"skills": CATALOG}),
        **_scoped_config(
            {"skills": {"available": ["ambient", "marketplace-only"],
                        "required": ["ambient"]}}
        ),
    )
    _, respond = await _run(channel, _command("skills list"))
    assert "`ambient` (always loaded here)" in respond.text
    assert "research" not in respond.text
    assert "marketplace-only" not in respond.text


@pytest.mark.asyncio
async def test_skills_list_with_nothing_available() -> None:
    channel, _ = _channel(
        reply=(True, {"skills": CATALOG}), **_scoped_config({"skills": {"available": []}})
    )
    _, respond = await _run(channel, _command("skills list"))
    assert respond.text == "No skills are available in this conversation."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply", [(False, {"error": "boom"}), (True, {"unexpected": 1}), (True, None)]
)
async def test_skills_list_says_so_when_the_runtime_cannot_answer(reply: Any) -> None:
    channel, _ = _channel(reply=reply)
    _, respond = await _run(channel, _command("skills list"))
    assert respond.text == slack_commands.SKILLS_UNAVAILABLE_NOTICE


@pytest.mark.asyncio
async def test_skills_list_gives_up_when_no_reply_comes(monkeypatch) -> None:
    monkeypatch.setattr(slash_handlers, "_SLASH_AGENT_TIMEOUT_SECONDS", 0.01)
    channel, routed = _channel(reply=None)
    _, respond = await _run(channel, _command("skills list"))
    assert len(routed) == 1
    assert respond.text == slack_commands.SKILLS_UNAVAILABLE_NOTICE
    assert not channel._agent_replies


# ── experts list ────────────────────────────────────────────────────────────


EXPERT_CATALOG = [
    {"id": "cedar", "installed": True, "connection_state": "connected",
     "displayDescription": {"en": "Answers concise questions."}},
    {"id": "market-id", "packageName": "lantern", "installed": True,
     "connection_state": "disconnected", "displayDescription": {"en": "Finds sources."}},
    {"id": "remote-only", "installed": False, "connection_state": "disconnected"},
]


@pytest.mark.asyncio
async def test_experts_list_shows_installed_cards_and_scope_selection() -> None:
    channel, routed = _channel(
        expert_reply=(True, {"templates": EXPERT_CATALOG}),
        **_scoped_config({"expert": "cedar"}),
    )
    ack, respond = await _run(channel, _command("experts list"))

    assert ack.calls == [{}]
    assert respond.calls[0]["response_type"] == "ephemeral"
    assert "blocks" not in respond.calls[0]
    (req,) = routed
    assert req.req_method is ReqMethod.AGENT_TEMPLATES_LIST
    assert req.params == {"filter": "local"}
    assert req.is_stream is False
    assert req.metadata["slack_team_id"] == TEAM
    assert RESPONSE_URL not in repr(req)
    assert "*Selected by scope*: `cedar`" in respond.text
    assert "`cedar`: Answers concise questions" in respond.text
    assert "`lantern` (connectors are not ready): Finds sources" in respond.text
    assert "remote-only" not in respond.text
    assert "market-id" not in respond.text
    assert "does not change" in respond.text
    assert not channel._client.posts and not channel._agent_replies


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expert_reply", [(False, {"error": "boom"}), (True, {"unexpected": 1}),
                     (True, None)]
)
async def test_experts_list_fails_closed_on_invalid_runtime_response(expert_reply: Any) -> None:
    channel, _ = _channel(expert_reply=expert_reply)
    _, respond = await _run(channel, _command("experts list"))
    assert respond.text == slack_commands.experts_unavailable_notice(BOT_NAME)


@pytest.mark.asyncio
async def test_subagents_list_uses_runtime_roster_and_conversation_scope() -> None:
    channel, routed = _channel(
        subagents_reply=(True, {"subagents": ["research_agent", "code_agent"]}),
        **_scoped_config({"subagents": ["code_agent", "not_mounted"]}),
    )
    ack, respond = await _run(channel, _command("subagents list", channel_id=ROOM))

    assert ack.calls == [{}]
    (req,) = routed
    assert req.req_method is ReqMethod.SUBAGENTS_LIST
    assert req.is_stream is False
    assert respond.calls[0]["response_type"] == "ephemeral"
    assert "blocks" not in respond.calls[0]
    assert "`code_agent`" in respond.text
    assert "research_agent" not in respond.text
    assert "not_mounted" not in respond.text
    assert not channel._client.posts


@pytest.mark.asyncio
async def test_subagents_list_with_no_effective_names() -> None:
    channel, _ = _channel(
        subagents_reply=(True, {"subagents": ["research_agent"]}),
        **_scoped_config({"subagents": []}),
    )
    _, respond = await _run(channel, _command("subagents list"))
    assert respond.text == "No subagents are available in this conversation."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply", [(False, {"error": "secret"}), (True, {"agents": []})]
)
async def test_subagents_list_reports_runtime_failure_without_details(reply: Any) -> None:
    channel, _ = _channel(subagents_reply=reply)
    _, respond = await _run(channel, _command("subagents list"))
    assert respond.text == slack_commands.subagents_unavailable_notice(BOT_NAME)
    assert "secret" not in respond.text


@pytest.mark.asyncio
async def test_late_command_error_is_not_posted_to_slack(monkeypatch) -> None:
    monkeypatch.setattr(slash_handlers, "_SLASH_AGENT_TIMEOUT_SECONDS", 0.01)
    channel, routed = _channel(reply=None)
    await _run(channel, _command("skills list", channel_id=ROOM))
    request = _of(routed, ReqMethod.SKILLS_LIST)[0]

    await channel.send(
        Message(
            id=request.id, type="res", channel_id="slack", session_id=None,
            params={}, timestamp=0.0, ok=False,
            payload={"error": "private runtime error"},
            metadata={"slack_channel_id": ROOM},
        )
    )

    assert not channel._client.posts


@pytest.mark.asyncio
async def test_a_message_with_an_unknown_id_is_not_taken_for_a_reply() -> None:
    channel, _ = _channel()
    stray = Message(
        id="someone-else", type="res", channel_id="slack", session_id=None,
        params={}, timestamp=0.0, ok=True, payload={},
    )
    assert channel._settle_agent_reply(stray) is False


# ── ask ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_posts_the_question_and_starts_a_dm_turn_on_it() -> None:
    channel, routed = _channel()
    ack, respond = await _run(channel, _command("ask  What changed\nin v90?"))

    assert ack.calls == [{}]
    assert not respond.calls
    (anchor,) = channel._client.posts
    assert anchor["channel"] == DM
    assert anchor["text"] == slack_commands.anchor_text(
        "What changed\nin v90?", f"{COMMAND} ask"
    )

    (req,) = routed
    assert req.req_method is ReqMethod.CHAT_SEND
    # The DM's own session: the question joins the conversation already there.
    assert req.session_id == f"slack_{TEAM}_{DM}_{USER}"
    assert req.user_id == USER
    assert req.params["query"].startswith("What changed\nin v90?")
    assert req.metadata["slack_message_ts"] == "1780000000.000001"
    assert req.metadata["slack_channel_type"] == "im"
    assert "plugin_names" not in req.params
    assert "mcp" not in req.params
    assert RESPONSE_URL not in repr(req)


@pytest.mark.asyncio
async def test_ask_carries_the_scoped_parameters_a_dm_message_would() -> None:
    channel, routed = _channel(
        **_scoped_config({"subagents": [], "skills": {"available": ["ambient"]}})
    )
    await _run(channel, _command("ask hello"))
    (req,) = routed
    assert req.params["agent_skills_available"] == ["ambient"]
    assert req.params["agent_subagents_available"] == []


@pytest.mark.asyncio
async def test_the_anchor_coming_back_as_an_event_starts_no_second_turn() -> None:
    channel, routed = _channel()
    await _run(channel, _command("ask hello"))
    (anchor,) = channel._client.posts
    echo = {
        "type": "message",
        "channel_type": "im",
        "channel": DM,
        "user": BOT_USER,
        "bot_id": BOT_ID,
        "text": anchor["text"],
        "ts": "1780000000.000001",
    }
    outcome = await channel._route_message_event(echo, {"team_id": TEAM})
    assert outcome.startswith("ignored:")
    assert len(routed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["channel_not_found", "not_in_channel"])
async def test_ask_in_somebody_elses_dm_is_refused(error: str) -> None:
    channel, routed = _channel(client=_RecordingSlackClient(post_error=error))
    ack, respond = await _run(channel, _command("ask hello"))
    assert ack.calls == [{}]
    assert respond.text == slack_commands.ask_needs_dm_text(COMMAND, bot_name=BOT_NAME)
    assert not routed


@pytest.mark.asyncio
async def test_ask_whose_question_cannot_be_posted_starts_nothing() -> None:
    channel, routed = _channel(client=_RecordingSlackClient(post_error="ratelimited"))
    _, respond = await _run(channel, _command("ask hello"))
    assert respond.text == slack_commands.ASK_NOT_POSTED_NOTICE
    assert not routed


# ── ask from a channel: a side conversation answered in the DM ─────────────

THREAD = "1710000001.000100"
THREAD_SESSION = f"slack_{TEAM}_{ROOM}_{THREAD}"


async def _seed_channel_turn(channel: SlackChannel, *, ts: str = THREAD) -> None:
    """A mention in ROOM, which starts a thread session there the usual way."""
    outcome = await channel._handle_slack_event(
        {"type": "app_mention", "channel_type": "channel", "channel": ROOM,
         "user": OUTSIDER, "text": f"<@{BOT_USER}> what is the plan?", "ts": ts},
        {"team_id": TEAM},
        is_dm=False,
        trigger="mention",
    )
    assert outcome.startswith("dispatched:"), outcome


def _of(routed: list[Message], method: ReqMethod) -> list[Message]:
    return [req for req in routed if req.req_method is method]


@pytest.mark.asyncio
async def test_ask_in_a_channel_answers_in_a_dm_thread_forked_from_its_latest_thread() -> None:
    channel, routed = _channel()
    channel._identity.host = "acme.slack.com"
    await _seed_channel_turn(channel)
    ack, respond = await _run(channel, _command("ask what did we decide?", channel_id=ROOM))

    # The channel is told, privately, and sees nothing else.
    assert ack.text == slack_commands.ask_answering_in_dm_notice(BOT_NAME)
    assert ack.calls[0]["response_type"] == "ephemeral"
    assert not respond.calls
    (anchor,) = channel._client.posts
    assert anchor["channel"] == USER
    assert f"<#{ROOM}>" in anchor["text"]
    assert ">what did we decide?" in anchor["text"]
    assert "https://acme.slack.com/archives/C0CHANNEL1/p1710000001000100" in anchor["text"]

    side = f"slack_{TEAM}_{DM}_1780000000.000001"
    (fork,) = _of(routed, ReqMethod.SESSION_FORK)
    assert fork.params == {
        "source_session_id": THREAD_SESSION,
        "target_session_id": side,
        "title": f"{COMMAND} ask from {ROOM}",
        "session_equipment_override": {
            "agent_template_name": "",
            "plugin_names": [],
            "mcp": [],
        },
    }
    seeded, asked = _of(routed, ReqMethod.CHAT_SEND)
    assert seeded.session_id == THREAD_SESSION
    assert asked.session_id == side
    assert asked.user_id == USER
    assert asked.metadata["slack_channel_id"] == DM
    assert asked.metadata["slack_thread_ts"] == "1780000000.000001"
    assert asked.params["plugin_names"] == []
    assert asked.params["mcp"] == []
    assert f"<#{ROOM}>" in asked.params["query"]
    assert "what did we decide?" in asked.params["query"]


@pytest.mark.asyncio
async def test_a_reply_in_the_dm_thread_continues_the_side_conversation() -> None:
    channel, routed = _channel()
    await _seed_channel_turn(channel)
    await _run(channel, _command("ask what did we decide?", channel_id=ROOM))
    side = f"slack_{TEAM}_{DM}_1780000000.000001"

    await channel._route_message_event(
        {"type": "message", "channel_type": "im", "channel": DM, "user": USER,
         "text": "and why?", "ts": "1780000100.000001",
         "thread_ts": "1780000000.000001"},
        {"team_id": TEAM},
    )
    follow_up = _of(routed, ReqMethod.CHAT_SEND)[-1]
    assert follow_up.session_id == side
    assert follow_up.metadata["slack_thread_ts"] == "1780000000.000001"

    # Outside that thread the DM is the person's ordinary conversation.
    await channel._route_message_event(
        {"type": "message", "channel_type": "im", "channel": DM, "user": USER,
         "text": "unrelated", "ts": "1780000200.000001"},
        {"team_id": TEAM},
    )
    assert _of(routed, ReqMethod.CHAT_SEND)[-1].session_id == f"slack_{TEAM}_{DM}_{USER}"


@pytest.mark.asyncio
async def test_the_channels_latest_thread_is_the_one_forked() -> None:
    channel, routed = _channel()
    await _seed_channel_turn(channel, ts="1710000001.000100")
    await _seed_channel_turn(channel, ts="1710000009.000900")
    await _run(channel, _command("ask status?", channel_id=ROOM))
    (fork,) = _of(routed, ReqMethod.SESSION_FORK)
    assert fork.params["source_session_id"] == f"slack_{TEAM}_{ROOM}_1710000009.000900"


@pytest.mark.asyncio
async def test_a_channel_wide_session_is_forked_without_a_prior_turn() -> None:
    channel, routed = _channel(
        **_scoped_config(entries=[{"match": {"channel": "slack", "chat": ROOM},
                                   "delivery": {"session": "channel"}}])
    )
    await _run(channel, _command("ask status?", channel_id=ROOM))
    (fork,) = _of(routed, ReqMethod.SESSION_FORK)
    assert fork.params["source_session_id"] == f"slack_{TEAM}_{ROOM}"
    assert "Answering from the channel's conversation." in channel._client.posts[0]["text"]


@pytest.mark.asyncio
async def test_cold_private_channel_ask_uses_its_channel_session() -> None:
    client = _RecordingSlackClient(
        info={"id": ROOM, "is_channel": True, "is_private": True}
    )
    channel, routed = _channel(
        client=client, warm_chat_types=False,
        **_scoped_config(entries=[
            {"match": {"channel": "slack", "chat_type": "group"},
             "delivery": {"session": "channel"}},
        ]),
    )
    channel._granted_scopes = frozenset({"groups:read"})

    ack, _ = await _run(channel, _command("ask status?", channel_id=ROOM))

    assert ack.calls == [{}]
    assert client.info_calls == [ROOM]
    assert _of(routed, ReqMethod.SESSION_FORK)[0].params["source_session_id"] == (
        f"slack_{TEAM}_{ROOM}"
    )
    assert _of(routed, ReqMethod.CHAT_SEND)[0].metadata["slack_channel_type"] == "im"


@pytest.mark.asyncio
async def test_a_channel_with_no_known_session_is_answered_without_context() -> None:
    channel, routed = _channel()
    await _run(channel, _command("ask status?", channel_id=ROOM))
    assert not _of(routed, ReqMethod.SESSION_FORK)
    (asked,) = _of(routed, ReqMethod.CHAT_SEND)
    assert asked.session_id == f"slack_{TEAM}_{DM}_1780000000.000001"
    assert "No conversation of that channel's was found" in channel._client.posts[0]["text"]


@pytest.mark.asyncio
async def test_a_failed_fork_still_answers_and_corrects_the_dm_message() -> None:
    channel, routed = _channel(fork_ok=False)
    await _seed_channel_turn(channel)
    _, respond = await _run(channel, _command("ask status?", channel_id=ROOM))
    assert not respond.calls
    (update,) = channel._client.updates
    assert update["channel"] == DM and update["ts"] == "1780000000.000001"
    assert "No conversation of that channel's was found" in update["text"]
    assert _of(routed, ReqMethod.CHAT_SEND)[-1].session_id == (
        f"slack_{TEAM}_{DM}_1780000000.000001"
    )


@pytest.mark.asyncio
async def test_a_side_question_that_cannot_open_the_dm_starts_nothing() -> None:
    channel, routed = _channel(client=_RecordingSlackClient(post_error="ratelimited"))
    ack, respond = await _run(channel, _command("ask status?", channel_id=ROOM))
    assert ack.text == slack_commands.ask_answering_in_dm_notice(BOT_NAME)
    assert respond.text == slack_commands.ASK_NOT_POSTED_NOTICE
    assert not routed


@pytest.mark.asyncio
async def test_a_side_question_from_a_channel_outside_the_allow_list_is_refused() -> None:
    channel, routed = _channel(allowed_channel_ids=[OTHER_ROOM])
    ack, _ = await _run(channel, _command("ask status?", channel_id=ROOM))
    assert ack.text == slack_commands.conversation_not_enabled_notice(BOT_NAME)
    assert not routed and not channel._client.posts


def test_side_conversations_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(slash_handlers, "_MAX_SIDE_SESSIONS", 2)
    channel, _ = _channel()
    for ts in ("1.1", "2.2", "3.3"):
        channel._remember_side_session(TEAM, DM, ts, f"side-{ts}")
    assert channel._side_session(TEAM, DM, "1.1") == ""
    assert channel._side_session(TEAM, DM, "3.3") == "side-3.3"


# ── skills use: a turn with one skill required ─────────────────────────────


def _asked(routed: list[Message]) -> Message:
    (req,) = [r for r in routed if r.req_method is ReqMethod.CHAT_SEND]
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["skills use", "skills use research", "skills use research   "])
async def test_skills_use_without_a_task_offers_a_form(text: str) -> None:
    channel, routed = _channel(reply=(True, {"skills": CATALOG}))
    ack, respond = await _run(channel, _command(text))
    assert ack.calls == [{}]
    assert routed[0].req_method is ReqMethod.SKILLS_LIST
    assert respond.calls[0]["blocks"][-1]["elements"][0]["action_id"] == (
        "belle_slash_task:submit"
    )


@pytest.mark.asyncio
async def test_skills_use_in_the_dm_requires_the_skill_for_that_turn() -> None:
    channel, routed = _channel(reply=(True, {"skills": CATALOG}))
    ack, respond = await _run(channel, _command("skills use research summarise the doc"))

    assert ack.calls == [{}] and not respond.calls
    assert [r.req_method for r in routed] == [ReqMethod.SKILLS_LIST, ReqMethod.CHAT_SEND]
    req = _asked(routed)
    assert req.params["agent_skills_required"] == ["research"]
    assert "agent_skills_available" not in req.params
    assert req.params["query"].startswith("summarise the doc")
    assert req.session_id == f"slack_{TEAM}_{DM}_{USER}"
    (anchor,) = channel._client.posts
    assert anchor["text"] == slack_commands.anchor_text(
        "summarise the doc", f"{COMMAND} skills use research"
    )


@pytest.mark.asyncio
async def test_skills_use_matches_a_name_without_regard_to_case() -> None:
    channel, routed = _channel(reply=(True, {"skills": CATALOG}))
    await _run(channel, _command("skills use RESEARCH go"))
    assert _asked(routed).params["agent_skills_required"] == ["research"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["nope", "switched-off", "marketplace-only"])
async def test_skills_use_refuses_a_skill_that_cannot_be_used(name: str) -> None:
    channel, routed = _channel(reply=(True, {"skills": CATALOG}))
    _, respond = await _run(channel, _command(f"skills use {name} do it"))
    assert respond.text == slack_commands.skill_unknown_text(name, COMMAND)
    assert [r.req_method for r in routed] == [ReqMethod.SKILLS_LIST]
    assert not channel._client.posts


@pytest.mark.asyncio
async def test_skills_use_adds_to_what_the_scope_already_requires() -> None:
    channel, routed = _channel(
        reply=(True, {"skills": CATALOG}),
        **_scoped_config({"skills": {"available": ["ambient", "research"],
                                     "required": ["ambient"]}}),
    )
    await _run(channel, _command("skills use research go"))
    req = _asked(routed)
    assert req.params["agent_skills_required"] == ["ambient", "research"]
    assert req.params["agent_skills_available"] == ["ambient", "research"]


@pytest.mark.asyncio
async def test_skills_use_refuses_a_skill_the_scope_keeps_out() -> None:
    channel, routed = _channel(
        reply=(True, {"skills": CATALOG}),
        **_scoped_config({"skills": {"available": ["ambient"]}}),
    )
    _, respond = await _run(channel, _command("skills use research go"))
    assert respond.text == slack_commands.skill_unknown_text("research", COMMAND)
    assert not [r for r in routed if r.req_method is ReqMethod.CHAT_SEND]


@pytest.mark.asyncio
async def test_skills_use_says_so_when_the_skill_list_cannot_be_read() -> None:
    channel, routed = _channel(reply=(False, {"error": "boom"}))
    _, respond = await _run(channel, _command("skills use research go"))
    assert respond.text == slack_commands.SKILLS_UNAVAILABLE_NOTICE
    assert not channel._client.posts


@pytest.mark.asyncio
async def test_skills_use_in_a_channel_is_a_side_question_with_the_skill() -> None:
    channel, routed = _channel(reply=(True, {"skills": CATALOG}))
    await _seed_channel_turn(channel)
    ack, respond = await _run(channel, _command("skills use research recap", channel_id=ROOM))

    assert ack.calls == [{}]
    # Acknowledged before the skill was checked, so the notice comes after.
    assert respond.calls[0]["text"] == slack_commands.ask_answering_in_dm_notice(BOT_NAME)
    assert len(respond.calls) == 1
    assert _of(routed, ReqMethod.SESSION_FORK)
    asked = _of(routed, ReqMethod.CHAT_SEND)[-1]
    assert asked.session_id == f"slack_{TEAM}_{DM}_1780000000.000001"
    assert asked.params["agent_skills_required"] == ["research"]
    assert f"{COMMAND} skills use research" in channel._client.posts[0]["text"]


@pytest.mark.asyncio
async def test_skills_use_from_a_channel_refuses_a_skill_the_dm_keeps_out() -> None:
    channel, routed = _channel(
        reply=(True, {"skills": CATALOG}),
        **_scoped_config(entries=[
            {"match": {"channel": "slack", "chat_type": "im"},
             "agent": {"skills": {"available": ["ambient"]}}},
        ]),
    )
    _, respond = await _run(channel, _command("skills use research recap", channel_id=ROOM))
    expected = slack_commands.skill_not_in_dm_text("research", bot_name=BOT_NAME)
    assert [call["text"] for call in respond.calls] == [
        slack_commands.ask_answering_in_dm_notice(BOT_NAME), expected,
    ]
    (update,) = channel._client.updates
    assert update["text"] == expected
    assert not _of(routed, ReqMethod.CHAT_SEND)
    assert not _of(routed, ReqMethod.SESSION_FORK)


@pytest.mark.asyncio
async def test_a_required_skill_outside_the_turns_catalog_is_never_sent() -> None:
    channel, routed = _channel(**_scoped_config({"skills": {"available": ["ambient"]}}))
    outcome = await channel._handle_slack_event(
        {"type": "message", "channel_type": "im", "channel": DM, "user": USER,
         "text": "go", "ts": "1780000000.000009"},
        {"team_id": TEAM},
        is_dm=True,
        trigger="dm",
        required_skills=("research",),
    )
    assert outcome == "refused:required-skill-outside-the-scoped-catalog"
    assert not routed


# ── subagents use: one named delegation in a private turn ──────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["subagents use", "subagents use code_agent"])
async def test_subagents_use_without_a_task_offers_a_form(text: str) -> None:
    channel, routed = _channel(subagents_reply=(True, {"subagents": ["code_agent"]}))
    ack, respond = await _run(channel, _command(text))
    assert ack.calls == [{}]
    assert routed[0].req_method is ReqMethod.SUBAGENTS_LIST
    assert respond.calls[0]["blocks"][-1]["elements"][0]["action_id"] == (
        "belle_slash_task:submit"
    )


@pytest.mark.asyncio
async def test_subagents_use_in_dm_sends_one_turn_requirement() -> None:
    channel, routed = _channel(
        subagents_reply=(True, {"subagents": ["code_agent", "research_agent"]})
    )
    ack, respond = await _run(
        channel, _command("subagents use CODE_AGENT inspect the code")
    )
    assert ack.calls == [{}] and not respond.calls
    assert [r.req_method for r in routed] == [ReqMethod.SUBAGENTS_LIST, ReqMethod.CHAT_SEND]
    req = _asked(routed)
    assert req.params["agent_subagent_required"] == {
        "name": "code_agent", "task": "inspect the code"
    }
    assert req.session_id == f"slack_{TEAM}_{DM}_{USER}"
    assert req.params["query"].startswith("inspect the code")
    assert channel._client.posts[0]["text"] == slack_commands.anchor_text(
        "inspect the code", f"{COMMAND} subagents use code_agent"
    )


@pytest.mark.asyncio
async def test_subagents_use_refuses_a_name_not_allowed_at_origin() -> None:
    channel, routed = _channel(
        subagents_reply=(True, {"subagents": ["code_agent"]}),
        **_scoped_config({"subagents": ["research_agent"]}),
    )
    _, respond = await _run(
        channel, _command("subagents use code_agent inspect", channel_id=ROOM)
    )
    assert respond.text == slack_commands.subagent_unknown_text(
        "code_agent", COMMAND
    )
    assert not channel._client.posts
    assert not _of(routed, ReqMethod.CHAT_SEND)


@pytest.mark.asyncio
async def test_subagents_use_refuses_a_name_not_allowed_in_dm() -> None:
    channel, routed = _channel(
        subagents_reply=(True, {"subagents": ["code_agent"]}),
        **_scoped_config(entries=[
            {"match": {"channel": "slack", "chat_type": "im"},
             "agent": {"subagents": []}},
        ]),
    )
    _, respond = await _run(
        channel, _command("subagents use code_agent inspect", channel_id=ROOM)
    )
    expected = slack_commands.subagent_not_in_dm_text("code_agent", bot_name=BOT_NAME)
    assert [call["text"] for call in respond.calls] == [
        slack_commands.subagent_starting_in_dm_notice(BOT_NAME), expected,
    ]
    assert channel._client.updates[0]["text"] == expected
    assert not _of(routed, ReqMethod.SESSION_FORK)
    assert not _of(routed, ReqMethod.CHAT_SEND)


@pytest.mark.asyncio
async def test_a_required_subagent_outside_the_turns_catalog_is_never_sent() -> None:
    channel, routed = _channel(**_scoped_config({"subagents": []}))
    outcome = await channel._handle_slack_event(
        {"type": "message", "channel_type": "im", "channel": DM, "user": USER,
         "text": "inspect", "ts": "1780000000.000019"},
        {"team_id": TEAM},
        is_dm=True,
        trigger="dm",
        required_subagent=("code_agent", "inspect"),
    )
    assert outcome == "refused:required-subagent-outside-the-scoped-catalog"
    assert not routed


@pytest.mark.asyncio
async def test_subagents_use_from_channel_is_private_without_channel_history() -> None:
    channel, routed = _channel(
        subagents_reply=(True, {"subagents": ["code_agent"]})
    )
    await _seed_channel_turn(channel)
    _, respond = await _run(
        channel, _command("subagents use code_agent inspect", channel_id=ROOM)
    )
    assert respond.text == slack_commands.subagent_starting_in_dm_notice(BOT_NAME)
    assert not _of(routed, ReqMethod.SESSION_FORK)
    req = _of(routed, ReqMethod.CHAT_SEND)[-1]
    assert req.chat_id == DM
    assert req.params["agent_subagent_required"] == {
        "name": "code_agent", "task": "inspect"
    }
    assert f"{COMMAND} subagents use code_agent" in channel._client.posts[0]["text"]
    assert "receives only the task" in channel._client.posts[0]["text"]


@pytest.mark.asyncio
async def test_subagents_use_queues_instead_of_steering_a_running_turn() -> None:
    channel, routed = _channel(
        subagents_reply=(True, {"subagents": ["code_agent"]}),
        **_scoped_config(entries=[
            {"match": {"channel": "slack", "chat_type": "im"},
             "delivery": {"mid_turn": "steer"}},
        ]),
    )
    await _run(channel, _command("ask first task"))
    assert len(_of(routed, ReqMethod.CHAT_SEND)) == 1
    await _run(channel, _command("subagents use code_agent second task"))
    assert len(_of(routed, ReqMethod.CHAT_SEND)) == 1
    (entry,) = channel._queued_messages[f"slack_{TEAM}_{DM}_{USER}"]
    assert entry.request.params["agent_subagent_required"] == {
        "name": "code_agent", "task": "second task"
    }
    assert "input_mode" not in entry.request.params
    channel._admission.turn_initiators.clear()
    await channel._drain_queued_messages(entry.session_id)
    assert _of(routed, ReqMethod.CHAT_SEND)[-1].params[
        "agent_subagent_required"
    ] == {"name": "code_agent", "task": "second task"}


# ── status ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_in_the_dm_reports_the_agent_and_nothing_private() -> None:
    channel, routed = _channel()
    ack, respond = await _run(channel, _command("status"))

    assert ack.calls == [{}]
    (req,) = routed
    assert req.req_method is ReqMethod.COMMAND_STATUS
    text = respond.text
    assert respond.calls[0]["response_type"] == "ephemeral"
    assert "*Agent*: running, version 9.1.0 (status check took " in text
    assert "*Model*: `glm-4.6` via openai" in text
    assert "*Right now*: nothing running." in text
    assert "here would use" not in text, "ask's source is a channel matter"
    for secret in _STATUS_SECRETS:
        assert secret not in text
    assert not channel._client.posts


@pytest.mark.asyncio
async def test_status_in_a_channel_describes_what_a_message_there_would_meet() -> None:
    channel, _ = _channel()
    channel._identity.host = "acme.slack.com"
    await _seed_channel_turn(channel)
    channel._queued_messages[THREAD_SESSION] = [object(), object()]  # type: ignore[list-item]
    # A different channel whose id merely starts with this one's.
    channel._admission.turn_initiators[f"slack_{TEAM}_{ROOM}0_1.1"] = object()  # type: ignore[assignment]

    _, respond = await _run(channel, _command("status", channel_id=ROOM))
    text = respond.text
    assert (
        "*Right now*: 1 turn running "
        "(<https://acme.slack.com/archives/C0CHANNEL1/p1710000001000100|source message>), "
        "2 messages waiting." in text
    )
    assert "A message sent during a turn waits until the current turn ends." in text
    assert (
        "<https://acme.slack.com/archives/C0CHANNEL1/p1710000001000100|the most recent thread>"
        in text
    )


@pytest.mark.asyncio
async def test_status_links_the_message_that_started_a_running_thread_turn() -> None:
    channel, _ = _channel()
    channel._identity.host = "acme.slack.com"
    channel._remember_turn_initiator(
        THREAD_SESSION, USER, "turn-1", is_dm=False, chat_type="channel",
        origin=SlackMessageOrigin(
            channel_id=ROOM, message_ts="1790600000.000001",
            reply_thread_ts=THREAD, root_thread_ts=THREAD,
        ),
    )

    _, respond = await _run(channel, _command("status", channel_id=ROOM))

    assert (
        "1 turn running "
        "(<https://acme.slack.com/archives/C0CHANNEL1/p1790600000000001|source message>)"
        in respond.text
    )
    assert "blocks" not in respond.calls[0]
    assert "Belle status" in respond.text
    assert "1 turn running" in respond.text
    assert "<https://acme.slack.com/archives/C0CHANNEL1/p1710000001000100|source message>" not in respond.text


@pytest.mark.asyncio
async def test_status_omits_a_running_link_when_two_turns_match() -> None:
    channel, _ = _channel()
    channel._identity.host = "acme.slack.com"
    _start_stop_target(channel, channel_id=ROOM, message_ts="1790600000.000001")
    _start_stop_target(
        channel, channel_id=ROOM, message_ts="1790600000.000002",
        request_id="turn-2",
    )

    _, respond = await _run(channel, _command("status", channel_id=ROOM))

    assert "*Right now*: 2 turns running." in respond.text
    assert "|source message>" not in respond.text
    assert "blocks" not in respond.calls[0]


@pytest.mark.asyncio
async def test_status_omits_a_running_link_without_a_workspace_host() -> None:
    channel, _ = _channel()
    _start_stop_target(channel, channel_id=ROOM)

    _, respond = await _run(channel, _command("status", channel_id=ROOM))

    assert "*Right now*: 1 turn running." in respond.text
    assert "|source message>" not in respond.text


def _scoped_channel(delivery: dict[str, Any], **agent: Any) -> SlackChannel:
    channel, _ = _channel(
        **_scoped_config(entries=[
            {"match": {"channel": "slack", "chat": ROOM},
             "delivery": delivery, "agent": agent},
        ])
    )
    return channel


@pytest.mark.asyncio
async def test_status_reads_the_conversations_scopes(monkeypatch) -> None:
    channel = _scoped_channel({"session": "channel", "mid_turn": "cancel"})
    # The model a message here would name, after the connector's own check
    # that the name is still configured -- which is that method's business.
    monkeypatch.setattr(channel, "_channel_model_name", lambda *_a, **_k: "fast-model")
    _, respond = await _run(channel, _command("status", channel_id=ROOM))
    text = respond.text
    assert "`fast-model`, set for this conversation" in text
    assert "stops the current turn and starts a new one" in text
    assert "the channel's conversation" in text


@pytest.mark.asyncio
async def test_status_reports_what_a_message_would_meet_not_what_was_written() -> None:
    # steer under a channel-wide session is settled to the default, so a steered
    # answer cannot land in a thread its sender never wrote in; status says so.
    channel = _scoped_channel({"session": "channel", "mid_turn": "steer"})
    _, respond = await _run(channel, _command("status", channel_id=ROOM))
    assert "waits until the current turn ends" in respond.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status_reply", [None, (False, {"error": "boom"})])
async def test_status_still_answers_when_the_agent_does_not(
    monkeypatch, status_reply: Any
) -> None:
    monkeypatch.setattr(slash_handlers, "_STATUS_AGENT_TIMEOUT_SECONDS", 0.01)
    channel, _ = _channel(status_reply=status_reply)
    _, respond = await _run(channel, _command("status"))
    assert "*Agent*: not answering." in respond.text
    assert "*Model*: unknown" in respond.text
    assert "*Right now*: nothing running." in respond.text


# ── failure and logging ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_form_submits_once_and_replayed_click_does_not_start_a_turn() -> None:
    channel, routed = _channel()
    _, form_reply = await _run(channel, _command("ask"))
    body, button = _task_form_click(form_reply, task="What is new?")
    first_ack, first_reply = _Recorder(), _Recorder()
    await channel._handle_slash_task_action(first_ack, body, button, first_reply)

    assert first_ack.calls == [{}]
    assert first_reply.calls[0]["replace_original"] is True
    assert first_reply.calls[0]["text"] == "Submitting your request."
    assert _asked(routed).params["query"].startswith("What is new?")

    second_ack, second_reply = _Recorder(), _Recorder()
    await channel._handle_slash_task_action(second_ack, body, button, second_reply)
    assert second_ack.calls == [{}]
    assert "expired" in second_reply.text
    assert len([req for req in routed if req.req_method is ReqMethod.CHAT_SEND]) == 1


@pytest.mark.asyncio
async def test_skill_form_rechecks_catalog_before_starting_a_turn(monkeypatch) -> None:
    channel, routed = _channel(reply=(True, {"skills": CATALOG}))
    _, form_reply = await _run(channel, _command("skills use"))
    body, button = _task_form_click(form_reply, name="research", task="Summarise this")

    async def changed_catalog(method: ReqMethod, *_args: Any, **_kwargs: Any):
        assert method is ReqMethod.SKILLS_LIST
        return SimpleNamespace(ok=True, payload={"skills": []})

    monkeypatch.setattr(channel, "_request_agent", changed_catalog)
    action_reply = _Recorder()
    await channel._handle_slash_task_action(_Recorder(), body, button, action_reply)
    assert action_reply.calls[0]["text"] == "Submitting your request."
    assert "No skill called" in action_reply.calls[-1]["text"]
    assert not [req for req in routed if req.req_method is ReqMethod.CHAT_SEND]


@pytest.mark.asyncio
async def test_subagent_form_rejects_click_from_another_user() -> None:
    channel, routed = _channel(subagents_reply=(True, {"subagents": ["code_agent"]}))
    _, form_reply = await _run(channel, _command("subagents use"))
    assert form_reply.calls[0]["blocks"]
    body, button = _task_form_click(
        form_reply, name="code_agent", task="Inspect this", user_id=OUTSIDER
    )
    action_reply = _Recorder()
    await channel._handle_slash_task_action(_Recorder(), body, button, action_reply)
    assert action_reply.text == slack_commands.not_permitted_notice(BOT_NAME)
    assert not [req for req in routed if req.req_method is ReqMethod.CHAT_SEND]


@pytest.mark.asyncio
async def test_subagent_form_starts_one_private_delegation() -> None:
    channel, routed = _channel(subagents_reply=(True, {"subagents": ["code_agent"]}))
    _, form_reply = await _run(channel, _command("subagents use"))
    body, button = _task_form_click(
        form_reply, name="code_agent", task="Inspect the code"
    )
    await channel._handle_slash_task_action(_Recorder(), body, button, _Recorder())
    req = _asked(routed)
    assert req.params["agent_subagent_required"] == {
        "name": "code_agent", "task": "Inspect the code"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["skill", "subagent"])
async def test_task_form_search_selects_a_name_beyond_first_page(kind: str) -> None:
    names = [f"{kind}-{index:03d}" for index in range(121)]
    catalog = [{"name": name, "installed": True, "enabled": True} for name in names]
    channel, routed = _channel(
        reply=(True, {"skills": catalog}),
        subagents_reply=(True, {"subagents": names}),
    )
    _, form_reply = await _run(channel, _command(f"{kind}s use"))
    name_input = _task_name_element(form_reply)
    assert name_input["type"] == "external_select"
    search = {
        "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": DM}, "action_id": name_input["action_id"],
        "value": names[-1],
    }
    choices = _Recorder()
    await channel._handle_slash_task_name_options(choices, search)
    assert [item["text"]["text"] for item in choices.calls[0]["options"]] == [names[-1]]

    body, button = _task_form_click(form_reply, task="Check this")
    body["state"]["values"][slack_connect.slack_task_forms.NAME_BLOCK] = {
        name_input["action_id"]: {
            "type": "external_select",
            "selected_option": choices.calls[0]["options"][0],
        }
    }
    await channel._handle_slash_task_action(_Recorder(), body, button, _Recorder())
    params = _asked(routed).params
    if kind == "skill":
        assert names[-1] in params["agent_skills_required"]
    else:
        assert params["agent_subagent_required"] == {
            "name": names[-1], "task": "Check this",
        }


@pytest.mark.asyncio
async def test_task_name_options_recheck_owner_and_scope(monkeypatch) -> None:
    names = [f"agent-{index:03d}" for index in range(121)]
    channel, _ = _channel(subagents_reply=(True, {"subagents": names}))
    _, form_reply = await _run(channel, _command("subagents use"))
    element = _task_name_element(form_reply)
    body = {
        "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": DM}, "action_id": element["action_id"],
        "value": "agent-120",
    }
    offered = _Recorder()
    await channel._handle_slash_task_name_options(offered, body)
    assert len(offered.calls[0]["options"]) == 1

    outsider = _Recorder()
    await channel._handle_slash_task_name_options(
        outsider, {**body, "user": {"id": OUTSIDER}},
    )
    assert outsider.calls == [{"options": []}]

    original = slack_connect.slack_agent_request_params

    def restricted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {**original(*args, **kwargs),
                "agent_subagents_available": ["agent-000"]}

    monkeypatch.setattr(slash_handlers, "slack_agent_request_params", restricted)
    scoped = _Recorder()
    await channel._handle_slash_task_name_options(scoped, body)
    assert scoped.calls == [{"options": []}]


@pytest.mark.asyncio
async def test_expired_task_form_refuses_submit_without_dispatch() -> None:
    channel, routed = _channel()
    _, form_reply = await _run(channel, _command("ask"))
    body, button = _task_form_click(form_reply)
    nonce = button["value"]
    form = channel._slash_task_forms[nonce]
    channel._slash_task_forms[nonce] = slack_connect.slack_task_forms.TaskForm(
        kind=form.kind, team_id=form.team_id, user_id=form.user_id,
        channel_id=form.channel_id, created_at=form.created_at - 3601,
    )
    action_reply = _Recorder()
    await channel._handle_slash_task_action(_Recorder(), body, button, action_reply)
    assert "expired" in action_reply.text
    assert not routed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    ("ask", "skills use", "skills use research", "subagents use", "subagents use code_agent"),
)
async def test_cancel_task_form_consumes_nonce_without_dispatch(command: str) -> None:
    channel, routed = _channel(
        reply=(True, {"skills": CATALOG}),
        subagents_reply=(True, {"subagents": ["code_agent"]}),
    )
    _, form_reply = await _run(channel, _command(command))
    body, submit = _task_form_click(form_reply, task="Do this task")
    cancel = form_reply.calls[0]["blocks"][-1]["elements"][1]
    assert cancel == {
        "type": "button",
        "action_id": slack_connect.slack_task_forms.CANCEL_ACTION,
        "text": {"type": "plain_text", "text": "Cancel"},
        "value": submit["value"],
    }
    before = len(routed)
    ack, response = _Recorder(), _Recorder()
    await channel._handle_slash_task_action(ack, body, cancel, response)
    assert ack.calls == [{}]
    assert response.calls == [{
        "response_type": "ephemeral", "replace_original": True,
        "text": command_ui.CANCELLED_NOTICE, "blocks": [],
    }]
    assert submit["value"] not in channel._slash_task_forms
    assert len(routed) == before
    assert not channel._client.posts

    replay = _Recorder()
    await channel._handle_slash_task_action(_Recorder(), body, submit, replay)
    assert "expired" in replay.text
    assert len(routed) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity", ({"user": {"id": OUTSIDER}},
                 {"team": {"id": "T0OTHERTEAM"}},
                 {"channel": {"id": OTHER_ROOM}}),
)
async def test_task_form_cancel_rejects_other_identity(identity: dict[str, Any]) -> None:
    channel, routed = _channel()
    _, form_reply = await _run(channel, _command("ask"))
    body, submit = _task_form_click(form_reply)
    cancel = form_reply.calls[0]["blocks"][-1]["elements"][1]
    refused = _Recorder()
    await channel._handle_slash_task_action(
        _Recorder(), {**body, **identity}, cancel, refused
    )
    assert refused.text == slack_commands.not_permitted_notice(BOT_NAME)
    assert submit["value"] in channel._slash_task_forms
    assert not routed

    accepted = _Recorder()
    await channel._handle_slash_task_action(_Recorder(), body, cancel, accepted)
    assert accepted.text == command_ui.CANCELLED_NOTICE
    assert submit["value"] not in channel._slash_task_forms


@pytest.mark.asyncio
async def test_cancel_model_picker_keeps_current_model(models) -> None:
    channel, routed = _channel()
    await _cancel_picker(channel, "models use")
    assert not channel._model_overrides
    assert not routed


@pytest.mark.asyncio
async def test_cancel_stop_picker_keeps_turn_running() -> None:
    channel, routed = _channel()
    session_id = _start_stop_target(channel)
    await _cancel_picker(channel, "stop")
    assert channel.turn_initiator(session_id) is not None
    assert not _of(routed, ReqMethod.CHAT_CANCEL)


@pytest.mark.asyncio
async def test_cancel_queue_drop_picker_keeps_message_waiting() -> None:
    channel, routed = _channel()
    entry = _waiting()
    channel._queued_messages[entry.session_id] = [entry]
    await _cancel_picker(channel, "queue drop", channel_id=ROOM)
    assert channel._queued_messages[entry.session_id] == [entry]
    assert not routed


@pytest.mark.asyncio
async def test_a_raising_handler_still_acknowledges(monkeypatch) -> None:
    channel, _ = _channel()

    async def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(channel, "_run_slash_command", _boom)
    ack, _ = await _run(channel, _command())
    assert ack.text == slack_commands.FAILED_NOTICE


@pytest.mark.asyncio
async def test_a_raise_after_the_acknowledgement_answers_through_respond(
    monkeypatch,
) -> None:
    channel, _ = _channel()

    async def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(channel, "_request_agent", _boom)
    ack, respond = await _run(channel, _command("skills list"))
    assert ack.calls == [{}]
    assert respond.text == slack_commands.FAILED_NOTICE


@pytest.mark.asyncio
async def test_a_malformed_payload_is_still_acknowledged() -> None:
    channel, _ = _channel()
    ack, _ = await _run(channel, None)
    assert ack.text == slack_commands.FAILED_NOTICE


@pytest.mark.asyncio
async def test_the_log_names_the_outcome_and_none_of_the_secrets(caplog) -> None:
    # Attached to the connector's logger rather than read off the root: the
    # package logger may be configured not to propagate, and then caplog's own
    # handler never sees the line.
    logger = command_ui.logger
    logger.addHandler(caplog.handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        channel, _ = _channel()
        await _run(channel, _command("ask my secret plan"))
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(previous)
    lines = [r.getMessage() for r in caplog.records if COMMAND in r.getMessage()]
    assert any("asked:dispatched:dm" in line for line in lines), lines
    logged = "\n".join(lines)
    assert USER in logged
    for secret in (RESPONSE_URL, "verification-token", "1.2.abc", "secret plan"):
        assert secret not in logged


@pytest.mark.asyncio
async def test_failed_private_response_does_not_log_its_url(caplog) -> None:
    channel, _ = _channel(reply=(True, {"skills": CATALOG}))

    async def fail_response(**_kwargs: Any) -> None:
        raise RuntimeError(f"request failed at {RESPONSE_URL}")

    logger = command_ui.logger
    logger.addHandler(caplog.handler)
    try:
        await channel._handle_slash_command(
            _Recorder(), _command("skills list"), fail_response
        )
    finally:
        logger.removeHandler(caplog.handler)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged
    assert RESPONSE_URL not in logged


# ── the command's name ──────────────────────────────────────────────────────
#
# One name reaches three places: the listener bolt matches an invocation
# against, every answer that quotes the command back, and the manifest an
# operator pastes into Slack. The tests below hold that all three follow
# Belle's slack.command_name, that an absent key leaves the default in place,
# and that a name Slack would refuse stops the workspace instead of registering
# a listener nothing can ever reach.


class _StartupSlackClient:
    """The one call ``start`` makes before the socket opens."""

    async def auth_test(self) -> dict[str, str]:
        return {"user_id": BOT_USER, "bot_id": BOT_ID, "team_id": TEAM}


async def _registered_command(
    monkeypatch: pytest.MonkeyPatch, **config: Any
) -> "str | None":
    """Start a channel against a fake bolt; the command it registered, or ``None``.

    ``None`` is a start that refused, which is a workspace that never opened a
    socket rather than one that registered nothing.
    """
    registered: list[str] = []
    started = asyncio.Event()
    closed = asyncio.Event()

    class FakeApp:
        def __init__(self, token: str, logger: Any = None) -> None:
            self.client = _StartupSlackClient()

        def event(self, _name: Any) -> Any:
            return lambda listener: listener

        def action(self, _constraint: Any) -> Any:
            return lambda listener: listener

        def options(self, _constraint: Any) -> Any:
            return lambda listener: listener

        def command(self, name: str) -> Any:
            registered.append(name)
            return lambda listener: listener

    class FakeHandler:
        def __init__(self, app: Any, app_token: str) -> None:
            pass

        async def start_async(self) -> None:
            started.set()
            await closed.wait()

        async def close_async(self) -> None:
            closed.set()

        monkeypatch.setattr(slack_connect, "AsyncApp", FakeApp)
    monkeypatch.setattr(belle_socket_mode, "AsyncSocketModeHandler", FakeHandler)

    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True, bot_token="xoxb-test", app_token="xapp-test", **config
        ),
        RobotMessageRouter(),
    )
    task = asyncio.create_task(channel.start())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
    except TimeoutError:
        await asyncio.wait_for(task, timeout=1)
        return None
    await channel.stop()
    await asyncio.wait_for(task, timeout=1)
    return registered[0] if registered else ""


@pytest.mark.asyncio
async def test_the_configured_name_is_the_one_bolt_listens_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _registered_command(monkeypatch, command_name="acme-bot") == "/acme-bot"


@pytest.mark.asyncio
async def test_no_configured_name_leaves_the_default_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _registered_command(monkeypatch) == COMMAND


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "written, registered",
    [
        ("/acme", "/acme"),
        ("  acme  ", "/acme"),
        ("ACME", "/acme"),
        ("/ACME_2", "/acme_2"),
    ],
)
async def test_a_slash_spaces_and_capitals_are_removed_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch, written: str, registered: str
) -> None:
    assert await _registered_command(monkeypatch, command_name=written) == registered


@pytest.mark.asyncio
@pytest.mark.parametrize("written", ["", "two words", "Ünicode", "a" * 33, "acme!"])
async def test_a_name_slack_would_refuse_stops_the_workspace(
    monkeypatch: pytest.MonkeyPatch, caplog, written: str
) -> None:
    """Refused at startup, because the alternative failure is a silent one.

    Slack delivers only the command its manifest declares, so a listener under
    any other name waits for an invocation that never arrives: the command is
    offered in the picker and answered by nobody, with nothing in the log.
    """
    logger = slash_handlers.logger
    logger.addHandler(caplog.handler)
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        assert await _registered_command(monkeypatch, command_name=written) is None
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(previous)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "command_name" in logged and "not starting this workspace" in logged


@pytest.mark.parametrize(
    "written, resolved",
    [
        ({}, DEFAULT_COMMAND_NAME),
        ({"command_name": None}, DEFAULT_COMMAND_NAME),
        ({"command_name": "   "}, DEFAULT_COMMAND_NAME),
        ({"command_name": "/Acme_Bot"}, "acme_bot"),
        ({"command_name": "acme!"}, ""),
        # YAML 1.1 turns each of these six words into a boolean before the
        # config reaches any reader, and all six are names Slack would accept.
        # Which word was written is gone, so it is refused rather than guessed.
        ({"command_name": True}, ""),
        ({"command_name": False}, ""),
    ],
)
def test_the_resolver_answers_an_absent_blank_or_unusable_key(
    written: dict[str, Any], resolved: str
) -> None:
    assert slack_config.resolve_command_name(written) == resolved


@pytest.mark.asyncio
async def test_every_answer_quotes_the_command_the_workspace_has() -> None:
    """Help, a usage line and a refusal all name the configured command."""
    channel, _ = _channel(command_name="acme-bot")
    ack, _ = await _run(channel, _command(""))
    assert "`/acme-bot help`" in ack.text
    assert COMMAND not in ack.text

    ack, _ = await _run(channel, _command("nonsense"))
    assert "`/acme-bot`" in ack.text

    ack, _ = await _run(channel, _command("skills list extra"))
    assert ack.text == "Usage: `/acme-bot skills list`"


@pytest.mark.asyncio
async def test_the_dm_anchor_names_the_command_the_workspace_has() -> None:
    """The anchor is written where no payload says what was typed.

    ``ask`` posts it into the person's DM before the turn starts, and a turn
    started from a task-form button gets the same line from a Block Kit payload
    that names no command at all. Config answers for both.
    """
    channel, _ = _channel(command_name="acme-bot")
    await _run(channel, _command("ask what changed?"))
    (anchor,) = channel._client.posts
    assert anchor["text"].startswith("You asked with `/acme-bot ask`:")


# ── the bot's display name ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "written, resolved",
    [
        # The code-side default. An install that already wrote the key with an
        # empty value reaches this, and so does one that deleted the line: the
        # config merge is version-gated and prunes nothing, so a template value
        # alone would never reach either.
        ("", DEFAULT_BOT_NAME),
        ("   ", DEFAULT_BOT_NAME),
        (None, DEFAULT_BOT_NAME),
        # Anything an operator writes is a name they get. Unlike a command
        # name, Slack matches nothing against this, so nothing is refused.
        ("Acme Bot", "Acme Bot"),
        ("  Acme Bot  ", "Acme Bot"),
    ],
)
def test_the_resolver_reads_a_blank_bot_name_as_the_default(
    written: Any, resolved: str
) -> None:
    channel, _ = _channel(bot_name=written) if written is not None else _channel()
    assert channel._bot_display_name() == resolved


def _said(ack: _Recorder, respond: _Recorder) -> str:
    """The text an invocation put in front of the caller, wherever it went.

    A refusal reaches Slack as an acknowledgement or through the response URL
    depending on how far the invocation got. Which one is not what these tests
    are about, so they read whichever spoke.
    """
    said = [
        str(call.get("text", ""))
        for recorder in (respond, ack)
        for call in recorder.calls
        if call.get("text")
    ]
    assert len(said) == 1, f"said {len(said)} things: {said}"
    return said[0]


@pytest.mark.asyncio
async def test_the_connector_hands_every_notice_the_configured_name() -> None:
    """The name reaches the notices through the connector, not only by hand.

    Four routes, each refusing for a different reason. A call site left on a
    constant would still answer, so the test drives the connector rather than
    the functions.
    """
    channel, _ = _channel(bot_name="Acme Bot", allowed_channel_ids=[OTHER_ROOM])

    ack, _ = await _run(channel, _command("help"))
    assert ack.text.startswith("*Acme Bot commands in Slack*")

    ack, respond = await _run(channel, _command("help", team_id="T0OTHERTEAM"))
    assert _said(ack, respond) == "Acme Bot is not set up for this workspace."

    ack, respond = await _run(channel, _command("ask hello", channel_id=ROOM))
    assert _said(ack, respond) == "Acme Bot is not enabled in this conversation."

    channel, _ = _channel(bot_name="Acme Bot", allow_from=[OUTSIDER])
    ack, respond = await _run(channel, _command("ask hello"))
    assert _said(ack, respond) == "You are not permitted to use Acme Bot."

    channel, _ = _channel(bot_name="Acme Bot", warm_chat_types=False)
    channel._granted_scopes = frozenset()
    ack, respond = await _run(channel, _command("status", channel_id=ROOM))
    assert _said(ack, respond) == (
        "Acme Bot cannot verify this conversation's type yet."
        " Send a message the bot can see here, then retry."
    )


@pytest.mark.asyncio
async def test_a_default_install_uses_belle_name() -> None:
    """The point of the default: nobody who set nothing sees a changed word.

    The sentences are written out rather than built from the constant, because
    what is being defended is the text an operator already reads in Slack.
    """
    channel, _ = _channel(allowed_channel_ids=[OTHER_ROOM])

    ack, _ = await _run(channel, _command("help"))
    assert ack.text.startswith("*Belle commands in Slack*")

    ack, respond = await _run(channel, _command("help", team_id="T0OTHERTEAM"))
    assert _said(ack, respond) == "Belle is not set up for this workspace."

    ack, respond = await _run(channel, _command("ask hello", channel_id=ROOM))
    assert _said(ack, respond) == (
        "Belle is not enabled in this conversation."
    )

    channel, _ = _channel(allow_from=[OUTSIDER])
    ack, respond = await _run(channel, _command("ask hello"))
    assert _said(ack, respond) == "You are not permitted to use Belle."


def test_belle_example_ships_the_default_bot_name() -> None:
    repo_root = Path(jiuwenswarm.__file__).resolve().parent
    text = (repo_root / "resources" / "config.yaml").read_text(
        encoding="utf-8"
    )
    written = [
        line.strip() for line in text.splitlines()
        if line.strip().startswith("bot_name:")
    ]
    assert written.count(f'bot_name: "{DEFAULT_BOT_NAME}"') == 0

    example = Path(__file__).resolve().parents[2] / "examples/config/belle-config.yaml"
    assert yaml.safe_load(example.read_text())["platforms"]["slack"]["bot_name"] == DEFAULT_BOT_NAME


def test_addressee_detection_reads_the_same_name_as_the_notices() -> None:
    """One key serves both. The identity string is display text with a token.

    ``bot_identity`` keeps ``or "the assistant"`` for a name that is genuinely
    empty, and the connector now resolves before calling it, so that fallback
    is unreachable from here rather than wrong.
    """
    from belle_agent.platforms.slack import decision_questions

    assert decision_questions.bot_identity("", "") == "the assistant"

    channel, _ = _channel(bot_name="Acme Bot")
    assert decision_questions.bot_identity(
        channel._bot_display_name(), BOT_USER
    ) == f"Acme Bot (mentioned as <@{BOT_USER}>)"

    channel, _ = _channel()
    assert decision_questions.bot_identity(
        channel._bot_display_name(), BOT_USER
    ).startswith(DEFAULT_BOT_NAME)
