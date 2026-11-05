"""Discord edits and deletes stay with their requesting turn and bot."""

import asyncio
import json

import pytest
from discord_transport import TOKEN, FakeRest

from belle_agent.backend import ToolCall
from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.settings import DiscordSettings
from belle_agent.platforms.discord.targets import READ_HISTORY, VIEW
from belle_agent.tools.host import BoundTurn, ToolHost

GUILD = "1000000000000000200"
CHANNEL = "1000000000000000300"
MESSAGE = "1000000000000000800"
FILE = "1000000000000000900"
BOT = "1000000000000000116"
USER = "1000000000000000400"


def setup(*, author=BOT, attachments=None):
    rest = FakeRest()
    platform = DiscordPlatform(DiscordSettings(token=TOKEN, bot="east"), client=DiscordClient(TOKEN, transport=rest.transport()))
    platform.bot_user_id = BOT
    metadata = {"discord_bot": "east", "discord_channel_id": CHANNEL, "discord_user_id": USER,
                "discord_guild_id": GUILD}
    toolkit = platform.toolkits(lambda: metadata)[4]
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 0, "guild_id": GUILD}))
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
                "roles": [{"id": GUILD, "permissions": str(VIEW | READ_HISTORY)}]}))
    for member in (BOT, USER):
        rest.answer("GET", f"/guilds/{GUILD}/members/{member}", (200, {"user": {"id": member}, "roles": []}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": member}, "roles": []} for member in (BOT, USER)]))
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {
        "id": MESSAGE, "channel_id": CHANNEL, "author": {"id": author}, "content": "old",
        "embeds": [], "components": [], "attachments": attachments if attachments is not None else [],
        "flags": 0, "edited_timestamp": None,
    }))
    host = ToolHost([])
    host.serve("discord", platform.toolkits(lambda: metadata))
    turn = BoundTurn("turn", platform="discord", metadata=metadata)
    host.bind("conversation", turn)
    return toolkit, rest, host, turn


async def approve(host, call):
    verdict = await host.authorize(call)
    assert verdict.action == "ask"
    return json.loads(await host.run_confirmed(call, host._turns[call.conversation]))


async def test_edit_preserves_omitted_attachments_and_does_not_ping():
    _, rest, host, _ = setup(attachments=[{"id": FILE, "filename": "old.txt", "description": "old"}])
    rest.answer("PATCH", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {"id": MESSAGE, "channel_id": CHANNEL,
                "attachments": [{"id": FILE}]}))
    call = ToolCall("conversation", "edit_message", {"message_id": MESSAGE, "content": "new <@123>"}, "call-edit")
    result = await approve(host, call)
    assert result["ok"] and result["attachment_ids"] == [FILE]
    body = rest.calls("PATCH", f"/channels/{CHANNEL}/messages/{MESSAGE}")[0].body
    assert body["attachments"] == [{"id": FILE, "filename": "old.txt", "description": "old"}]
    assert body["allowed_mentions"] == {"parse": [], "replied_user": False}
    assert body["content"] == "new <@123>"


async def test_explicit_empty_attachment_list_removes_files():
    _, rest, host, _ = setup(attachments=[{"id": FILE, "filename": "old.txt"}])
    rest.answer("PATCH", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {"id": MESSAGE, "channel_id": CHANNEL,
                "attachments": []}))
    call = ToolCall("conversation", "edit_message", {"message_id": MESSAGE, "attachments": []}, "call-remove")
    assert (await approve(host, call))["ok"]
    assert rest.calls("PATCH", f"/channels/{CHANNEL}/messages/{MESSAGE}")[0].body["attachments"] == []


async def test_other_author_never_gets_edit_or_delete_approval():
    _, rest, host, _ = setup(author=USER)
    for name in ("edit_message", "delete_message"):
        args = {"message_id": MESSAGE, **({"content": "new"} if name == "edit_message" else {})}
        call = ToolCall("conversation", name, args, name)
        assert json.loads(await host.call(call))["error"] == "not_this_bot_message"
    assert not rest.calls("PATCH", f"/channels/{CHANNEL}/messages/{MESSAGE}")
    assert not rest.calls("DELETE", f"/channels/{CHANNEL}/messages/{MESSAGE}")


async def test_confirmed_delete_runs_only_in_originating_turn():
    _, rest, host, turn = setup()
    call = ToolCall("conversation", "delete_message", {"message_id": MESSAGE}, "call-delete")
    assert (await approve(host, call))["deleted"]
    assert rest.calls("DELETE", f"/channels/{CHANNEL}/messages/{MESSAGE}")
    late = ToolCall("conversation", "delete_message", {"message_id": MESSAGE}, "call-late")
    assert (await host.authorize(late)).action == "ask"
    host.release("conversation", turn.request_id)
    assert json.loads(await host.run_confirmed(late, turn))["error"] == "turn_ended"
    assert len(rest.calls("DELETE", f"/channels/{CHANNEL}/messages/{MESSAGE}")) == 1


@pytest.mark.parametrize("scope", ["allow", "ask"])
async def test_approval_cannot_be_reused_after_message_changes(scope):
    _, rest, host, turn = setup()
    turn.tools = {"edit_message": scope}
    path = f"/channels/{CHANNEL}/messages/{MESSAGE}"
    first = {"id": MESSAGE, "channel_id": CHANNEL, "author": {"id": BOT}, "content": "old",
             "embeds": [], "components": [], "attachments": [], "flags": 0, "edited_timestamp": None}
    rest.answer("GET", path, (200, first), (200, {**first, "content": "changed",
                                                "edited_timestamp": "2026-10-21T00:00:01Z"}))
    call = ToolCall("conversation", "edit_message", {"message_id": MESSAGE, "content": "new"}, "call-stale")
    assert (await host.authorize(call)).action == "ask"
    assert json.loads(await host.run_confirmed(call, turn))["error"] == "approval_stale"
    assert not rest.calls("PATCH", path)


@pytest.mark.parametrize("name", ["edit_message", "delete_message"])
async def test_scope_ask_captures_toolkit_snapshot_and_approves_once(name):
    _, rest, host, turn = setup()
    turn.tools = {name: "ask"}
    path = f"/channels/{CHANNEL}/messages/{MESSAGE}"
    rest.answer("PATCH", path, (200, {"id": MESSAGE, "channel_id": CHANNEL, "attachments": []}))
    args = {"message_id": MESSAGE, **({"content": "new"} if name == "edit_message" else {})}
    call = ToolCall("conversation", name, args, "call-scope")
    questions = []

    async def confirm(held_call, bound, question):
        questions.append(question)
        assert held_call is call and bound is turn
        return True

    host._confirm = confirm
    assert json.loads(await host.call(call))["error"] == "awaiting_confirmation"
    assert len(questions) == 1
    assert name.split("_")[0].capitalize() in questions[0].text
    assert not rest.calls("PATCH", path) and not rest.calls("DELETE", path)
    assert json.loads(await host.run_confirmed(call, turn))["ok"]
    method = "PATCH" if name == "edit_message" else "DELETE"
    assert len(rest.calls(method, path)) == 1


@pytest.mark.parametrize("name", ["edit_message", "delete_message"])
@pytest.mark.parametrize("end", ["release", "stop", "replace"])
async def test_turn_ending_during_message_read_prevents_mutation(monkeypatch, name, end):
    _, rest, host, turn = setup()
    args = {"message_id": MESSAGE, **({"content": "new"} if name == "edit_message" else {})}
    call = ToolCall("conversation", name, args, "call-paused")
    assert (await host.authorize(call)).action == "ask"
    toolkit = host._served["discord"][name][1].__self__
    original = toolkit._message_for
    entered, resume = asyncio.Event(), asyncio.Event()

    async def read(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await resume.wait()
        return result

    monkeypatch.setattr(toolkit, "_message_for", read)
    running = asyncio.create_task(host.run_confirmed(call, turn))
    await asyncio.wait_for(entered.wait(), 2)
    if end == "release":
        host.release("conversation", turn.request_id)
    elif end == "stop":
        turn.stopped = True
    else:
        host.bind("conversation", BoundTurn("next", platform="discord", metadata=turn.metadata))
    resume.set()
    assert json.loads(await running)["error"] == "turn_ended"
    assert not any(request.method in {"PATCH", "DELETE"} for request in rest.requests)
