"""Discord thread and poll tools keep requester access and pagination explicit."""

import asyncio
import json

import pytest
from discord_transport import TOKEN, FakeRest

from belle_agent.backend import ToolCall
from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.native import CREATE_PRIVATE_THREADS, CREATE_PUBLIC_THREADS, DiscordNativeToolkit
from belle_agent.platforms.discord.settings import DiscordSettings
from belle_agent.platforms.discord.targets import MANAGE_THREADS, READ_HISTORY, SEND, SEND_THREADS, VIEW
from belle_agent.tools.host import BoundTurn, ToolHost

GUILD = "1000000000000000200"
CHANNEL = "1000000000000000300"
THREAD = "1000000000000000301"
HIDDEN = "1000000000000000302"
MESSAGE = "1000000000000000800"
BOT = "1000000000000000116"
USER = "1000000000000000400"
NEW = "1000000000000000401"
FORUM = "1000000000000000303"


@pytest.fixture
def setup():
    rest = FakeRest()
    platform = DiscordPlatform(
        DiscordSettings(token=TOKEN, bot="east", guild_members=True), client=DiscordClient(TOKEN, transport=rest.transport())
    )
    platform.bot_user_id = BOT
    metadata = {"discord_bot": "east", "discord_channel_id": CHANNEL,
                "discord_user_id": USER, "discord_guild_id": GUILD}
    tool = next(tool for tool in platform.toolkits(lambda: metadata) if isinstance(tool, DiscordNativeToolkit))
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 0, "guild_id": GUILD}))
    rest.answer("GET", f"/channels/{THREAD}", (200, {"id": THREAD, "type": 11,
                "guild_id": GUILD, "parent_id": CHANNEL}))
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
                "roles": [{"id": GUILD, "permissions": str(VIEW | READ_HISTORY)}]}))
    for member in (BOT, USER):
        rest.answer("GET", f"/guilds/{GUILD}/members/{member}",
                    (200, {"user": {"id": member}, "roles": []}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": member}, "roles": []} for member in (BOT, USER)]))
    return tool, rest, metadata


def thread(ident=THREAD):
    return {"id": ident, "guild_id": GUILD, "parent_id": CHANNEL, "type": 11,
            "name": "Planning", "thread_metadata": {"archived": False,
            "archive_timestamp": "2026-10-21T00:00:00Z"}}


async def test_native_read_tools_are_registered(setup):
    tool, _, _ = setup
    definitions = {item["name"]: item for item in tool.get_tool_definitions()}
    assert set(definitions) == {
        "list_discord_threads", "list_discord_thread_members", "read_discord_poll", "end_discord_poll",
        "create_discord_thread", "update_discord_thread", "change_discord_thread_member"}
    limit = definitions["list_discord_threads"]["input_params"]["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"]) == (2, 100)


async def test_active_thread_list_filters_unreadable_threads(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": [thread(), thread(HIDDEN)]}))
    rest.answer("GET", f"/channels/{HIDDEN}", (200, {"id": HIDDEN, "type": 12,
                "guild_id": GUILD, "parent_id": CHANNEL}))
    rest.answer("GET", f"/channels/{HIDDEN}/thread-members/{USER}", (404, {}))
    result = json.loads(await tool.list_discord_threads())
    assert [item["thread_id"] for item in result["threads"]] == [THREAD]
    assert result["coverage"]["status"] == "complete"


async def test_tool_host_routes_thread_listing_to_discord_native_tool(setup):
    tool, rest, metadata = setup
    host = ToolHost([])
    host.serve("discord", tool.platform.toolkits(lambda: metadata))
    host.bind("conversation", BoundTurn("turn", platform="discord", metadata=metadata))
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": [thread()]}))

    result = json.loads(await host.call(ToolCall("conversation", "list_discord_threads", {})))

    assert [item["thread_id"] for item in result["threads"]] == [THREAD]
    assert rest.calls("GET", f"/guilds/{GUILD}/threads/active")


async def test_thread_name_listing_keeps_viewers_without_history_in_audience(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {
        "id": CHANNEL, "type": 0, "guild_id": GUILD, "permission_overwrites": [
            {"id": NEW, "type": 1, "deny": str(READ_HISTORY), "allow": "0"}]}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER, NEW)]))
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": [thread()]}))

    result = json.loads(await tool.list_discord_threads())

    assert result["ok"]
    assert [item["thread_id"] for item in result["threads"]] == [THREAD]


async def test_active_thread_list_filters_other_parents_before_access_check(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": [
        {**thread(HIDDEN), "parent_id": FORUM}, thread()]}))
    result = json.loads(await tool.list_discord_threads(parent_channel_id=CHANNEL))
    assert [item["thread_id"] for item in result["threads"]] == [THREAD]
    assert not rest.calls("GET", f"/channels/{HIDDEN}")


@pytest.mark.parametrize("kind", [15, 16])
@pytest.mark.parametrize("mode", ["active", "public_archived"])
async def test_forum_and_media_parents_can_list_threads(setup, kind, mode):
    tool, rest, _ = setup
    record = {**thread(), "parent_id": FORUM}
    rest.answer("GET", f"/channels/{FORUM}", (200, {"id": FORUM, "type": kind, "guild_id": GUILD}))
    rest.answer("GET", f"/channels/{THREAD}", (200, {**record, "type": 11}))
    if mode == "active":
        rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": [record]}))
    else:
        record["thread_metadata"] = {"archived": True, "archive_timestamp": "2026-10-21T00:00:00Z"}
        rest.answer("GET", f"/channels/{FORUM}/threads/archived/public", (200, {
            "threads": [record], "has_more": False}))
    result = json.loads(await tool.list_discord_threads(mode=mode, parent_channel_id=FORUM))
    assert [item["thread_id"] for item in result["threads"]] == [THREAD]


async def test_archived_threads_use_timestamp_cursor_and_source_thread_parent(setup):
    tool, rest, metadata = setup
    metadata["discord_channel_id"] = THREAD
    rest.answer("GET", f"/channels/{CHANNEL}/threads/archived/public", (200, {
        "threads": [{**thread(), "thread_metadata": {"archived": True,
                    "archive_timestamp": "2026-10-21T00:00:00Z"}}], "has_more": True}))
    result = json.loads(await tool.list_discord_threads(mode="public_archived", limit=2))
    assert result["coverage"]["next_before"] == "2026-10-21T00:00:00Z"
    assert result["coverage"]["cursor_type"] == "timestamp"
    assert json.loads(await tool.list_discord_threads(mode="public_archived", before=THREAD))["error"] == "before_invalid"


async def test_private_archive_requires_requester_manage_threads(setup):
    tool, rest, _ = setup
    result = json.loads(await tool.list_discord_threads(mode="private_archived"))
    assert result["error"] == "requester_cannot_list_private_threads"
    assert not rest.calls("GET", f"/channels/{CHANNEL}/threads/archived/private")


@pytest.mark.parametrize("mode", ["public_archived", "private_archived", "joined_private_archived"])
@pytest.mark.parametrize("limit", [1, 101])
async def test_archived_thread_limit_out_of_range_refuses_before_api_call(setup, mode, limit):
    tool, rest, _ = setup

    result = json.loads(await tool.list_discord_threads(mode=mode, limit=limit))

    assert result["error"] == "limit_invalid"
    assert "2 to 100" in result["detail"]
    assert not rest.requests


async def test_private_archive_requires_bot_manage_threads(setup):
    tool, rest, _ = setup
    allow_writes(rest, MANAGE_THREADS)
    forum = {"id": FORUM, "type": 15, "guild_id": GUILD,
             "permission_overwrites": [{"id": BOT, "type": 1, "allow": "0", "deny": str(MANAGE_THREADS)}]}
    rest.answer("GET", f"/channels/{FORUM}", (200, forum))

    result = json.loads(await tool.list_discord_threads(mode="private_archived", parent_channel_id=FORUM, limit=2))

    assert result["error"] == "bot_cannot_list_private_threads"
    assert not rest.calls("GET", f"/channels/{FORUM}/threads/archived/private")


async def test_private_archive_lists_when_requester_and_bot_can_manage_threads(setup):
    tool, rest, _ = setup
    allow_writes(rest, MANAGE_THREADS)
    rest.answer("GET", f"/channels/{FORUM}", (200, {"id": FORUM, "type": 15, "guild_id": GUILD}))
    archive = f"/channels/{FORUM}/threads/archived/private"
    rest.answer("GET", archive, (200, {"threads": [], "has_more": False}))

    result = json.loads(await tool.list_discord_threads(mode="private_archived", parent_channel_id=FORUM, limit=2))

    assert result["ok"] and not result["threads"]
    assert rest.calls("GET", archive)


async def test_joined_private_archive_uses_its_own_endpoint_and_id_cursor(setup):
    tool, rest, _ = setup
    path = f"/channels/{CHANNEL}/users/@me/threads/archived/private"
    rest.answer("GET", path, (200, {"threads": [], "has_more": False}))
    result = json.loads(await tool.list_discord_threads(mode="joined_private_archived", before=THREAD))
    assert result["ok"] and result["coverage"]["cursor_type"] == "thread_id"
    assert rest.calls("GET", path)


async def test_thread_member_page_requires_intent_and_read_access(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/channels/{THREAD}/thread-members", (200, [{"user_id": USER,
                "join_timestamp": "2026-10-21T00:00:00Z", "member": {"user": {"id": USER}}}]))
    result = json.loads(await tool.list_discord_thread_members(THREAD, limit=1))
    assert result["members"][0]["user_id"] == USER
    assert result["coverage"]["next_after"] == USER
    object.__setattr__(tool.platform._settings, "guild_members", False)
    assert json.loads(await tool.list_discord_thread_members(THREAD))["error"] == "guild_members_intent_required"


async def test_poll_read_marks_open_counts_approximate_and_pages_voters(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {
        "id": MESSAGE, "channel_id": CHANNEL, "author": {"id": BOT},
        "poll": {"question": {"text": "Lunch?"}, "answers": [{"answer_id": 1, "poll_media": {"text": "Yes"}}],
                 "results": {"is_finalized": False, "answer_counts": [{"id": 1, "count": 1}]}}}))
    rest.answer("GET", f"/channels/{CHANNEL}/polls/{MESSAGE}/answers/1", (200, {"users": [
        {"id": USER, "username": "alice"}]}))
    result = json.loads(await tool.read_discord_poll(MESSAGE, answer_id=1, limit=1))
    assert result["counts_status"] == "approximate"
    assert result["voters"] == [{"user_id": USER, "username": "alice"}]
    assert result["coverage"]["next_after"] == USER
    assert json.loads(await tool.read_discord_poll(MESSAGE, answer_id=2))["error"] == "answer_id_not_in_poll"


async def test_poll_end_checks_bot_ownership_and_exact_response(setup):
    tool, rest, _ = setup
    path = f"/channels/{CHANNEL}/messages/{MESSAGE}"
    rest.answer("GET", path, (200, {"id": MESSAGE, "channel_id": CHANNEL,
                "author": {"id": USER}, "poll": {"answers": []}}))
    assert json.loads(await tool.end_discord_poll(MESSAGE))["error"] == "poll_not_owned_by_bot"
    assert not rest.calls("POST", f"/channels/{CHANNEL}/polls/{MESSAGE}/expire")
    rest.answer("GET", path, (200, {"id": MESSAGE, "channel_id": CHANNEL,
                "author": {"id": BOT}, "poll": {"answers": []}}))
    rest.answer("POST", f"/channels/{CHANNEL}/polls/{MESSAGE}/expire",
                (200, {"id": MESSAGE, "channel_id": CHANNEL, "poll": {"answers": []}}))
    assert json.loads(await tool.end_discord_poll(MESSAGE))["ended"]
    assert "end_discord_poll" in tool.active_turn_only


async def test_poll_end_requires_turn_bound_approval(setup):
    from belle_agent.backend import ToolCall
    from belle_agent.tools.host import BoundTurn, ToolHost

    tool, rest, metadata = setup
    host = ToolHost([])
    host.serve("discord", tool.platform.toolkits(lambda: metadata))
    host.bind("conversation", BoundTurn("turn", platform="discord", metadata=metadata))
    call = ToolCall("conversation", "end_discord_poll", {"message_id": MESSAGE})
    result = json.loads(await host.call(call))
    assert result["error"] == "approval_required"
    assert host.requires_active_turn("discord", "end_discord_poll")
    assert not rest.calls("POST", f"/channels/{CHANNEL}/polls/{MESSAGE}/expire")


async def test_late_poll_approval_cannot_end_poll(setup):
    from belle_agent.backend import ToolCall
    from belle_agent.tools.host import BoundTurn, ToolHost

    tool, rest, metadata = setup

    async def hold(_call, _turn, _question):
        return True

    host = ToolHost([], confirm=hold)
    host.serve("discord", tool.platform.toolkits(lambda: metadata))
    turn = BoundTurn("turn", platform="discord", metadata=metadata)
    host.bind("conversation", turn)
    call = ToolCall("conversation", "end_discord_poll", {"message_id": MESSAGE}, "call")
    assert json.loads(await host.call(call))["error"] == "awaiting_confirmation"
    host.release("conversation", "turn")
    assert json.loads(await host.run_confirmed(call, turn))["error"] == "turn_ended"
    assert not rest.calls("POST", f"/channels/{CHANNEL}/polls/{MESSAGE}/expire")


def allow_writes(rest, bits=0):
    granted = VIEW | READ_HISTORY | SEND | SEND_THREADS | CREATE_PUBLIC_THREADS | CREATE_PRIVATE_THREADS | bits
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
                "roles": [{"id": GUILD, "permissions": str(granted)}]}))


async def test_create_thread_from_message_checks_source_and_returns_exact_thread(setup):
    tool, rest, _ = setup
    allow_writes(rest)
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}",
                (200, {"id": MESSAGE, "channel_id": CHANNEL, "author": {"id": USER}}))
    rest.answer("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads",
                (201, {**thread(MESSAGE), "id": MESSAGE}))
    result = json.loads(await tool.create_discord_thread(CHANNEL, "Topic", source_message_id=MESSAGE,
                                                          auto_archive_duration=1440))
    assert result["thread_id"] == MESSAGE
    assert rest.calls("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads")[0].body == {
        "name": "Topic", "auto_archive_duration": 1440}
    assert json.loads(await tool.create_discord_thread(CHANNEL, "Topic", kind="public", source_message_id=MESSAGE))[
        "error"] == "source_message_options_invalid"


async def test_create_private_thread_adds_requester_or_reports_partial_side_effect(setup):
    tool, rest, _ = setup
    allow_writes(rest)
    rest.answer("POST", f"/channels/{CHANNEL}/threads", (201, {**thread(), "type": 12}))
    rest.answer("PUT", f"/channels/{THREAD}/thread-members/{USER}", (403, {}))
    result = json.loads(await tool.create_discord_thread(CHANNEL, "Private", kind="private", invitable=True))
    assert result["error"] == "requester_not_added" and result["created_thread_id"] == THREAD


async def test_update_thread_checks_permission_and_sends_only_given_fields(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/channels/{THREAD}", (200, {**thread(), "owner_id": BOT}))
    assert json.loads(await tool.update_discord_thread(THREAD, name="Updated"))[
        "error"] == "requester_cannot_update_thread"
    assert not rest.calls("PATCH", f"/channels/{THREAD}")
    allow_writes(rest, MANAGE_THREADS)
    rest.answer("PATCH", f"/channels/{THREAD}", (200, {**thread(), "name": "Updated"}))
    result = json.loads(await tool.update_discord_thread(THREAD, name="Updated"))
    assert result["name"] == "Updated"
    assert rest.calls("PATCH", f"/channels/{THREAD}")[0].body == {"name": "Updated"}


async def test_locked_thread_owner_cannot_unarchive_without_manage_threads(setup):
    tool, rest, _ = setup
    allow_writes(rest)
    locked = {**thread(), "owner_id": USER, "thread_metadata": {"archived": True, "locked": True}}
    rest.answer("GET", f"/channels/{THREAD}", (200, locked))
    result = json.loads(await tool.update_discord_thread(THREAD, archived=False))
    assert result["error"] == "requester_cannot_unarchive_locked_thread"
    assert not rest.calls("PATCH", f"/channels/{THREAD}")
    allow_writes(rest, MANAGE_THREADS)
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 0, "guild_id": GUILD,
                "permission_overwrites": [{"id": BOT, "type": 1, "allow": "0", "deny": str(MANAGE_THREADS)}]}))
    assert json.loads(await tool.update_discord_thread(THREAD, archived=False))[
        "error"] == "bot_cannot_unarchive_locked_thread"
    assert not rest.calls("PATCH", f"/channels/{THREAD}")
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 0, "guild_id": GUILD}))
    rest.answer("PATCH", f"/channels/{THREAD}", (200, {**locked, "thread_metadata": {"archived": False, "locked": True}}))
    assert json.loads(await tool.update_discord_thread(THREAD, archived=False))["ok"]


async def test_private_thread_without_invites_refuses_before_approval_or_write(setup):
    from belle_agent.backend import ToolCall
    from belle_agent.tools.host import BoundTurn, ToolHost

    tool, rest, _ = setup
    private = {**thread(), "type": 12, "thread_metadata": {"archived": False, "invitable": False}}

    async def permitted_thread(_thread_id):
        return private, USER, {USER: SEND_THREADS, BOT: SEND_THREADS | MANAGE_THREADS}

    tool._thread_for_write = permitted_thread
    args = {"thread_id": THREAD, "action": "add", "user_id": NEW}
    assert await tool.widening_question("change_discord_thread_member", args) is None
    result = json.loads(await tool.change_discord_thread_member(**args))
    assert result["error"] == "requester_cannot_invite_to_private_thread"
    async def unexpected_approval(*_args):
        pytest.fail("the unauthorized invite must not ask for approval")

    host = ToolHost([], confirm=unexpected_approval)
    host.serve("discord", [tool])
    host.bind("conversation", BoundTurn("turn", platform="discord"))
    result = json.loads(await host.call(ToolCall("conversation", "change_discord_thread_member", args)))
    assert result["error"] == "requester_cannot_invite_to_private_thread"
    assert not rest.calls("PUT", f"/channels/{THREAD}/thread-members/{NEW}")


async def test_thread_member_change_checks_authority_and_is_turn_bound(setup):
    tool, rest, _ = setup
    allow_writes(rest)
    rest.answer("GET", f"/channels/{THREAD}", (200, {**thread(), "owner_id": BOT}))
    rest.answer("GET", f"/guilds/{GUILD}/members/{NEW}", (200, {"user": {"id": NEW}, "roles": []}))
    result = json.loads(await tool.change_discord_thread_member(THREAD, "add", NEW))
    assert result["ok"] and result["user_id"] == NEW
    assert rest.calls("PUT", f"/channels/{THREAD}/thread-members/{NEW}")
    assert "change_discord_thread_member" in tool.active_turn_only
    assert json.loads(await tool.change_discord_thread_member(THREAD, "remove", NEW))[
        "error"] == "cannot_remove_thread_member"


async def test_thread_listing_reports_membership_and_excluded_bot_inaccessible_thread(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": [thread(), thread(HIDDEN)]}))
    rest.answer("GET", f"/channels/{THREAD}/thread-members/{USER}", (200, {"user_id": USER}))
    rest.answer("GET", f"/channels/{THREAD}/thread-members/{BOT}", (404, {}))
    rest.answer("GET", f"/channels/{HIDDEN}", (200, {"id": HIDDEN, "type": 12,
                "guild_id": GUILD, "parent_id": CHANNEL}))
    rest.answer("GET", f"/channels/{HIDDEN}/thread-members/{USER}", (200, {"user_id": USER}))
    rest.answer("GET", f"/channels/{HIDDEN}/thread-members/{BOT}", (404, {}))

    result = json.loads(await tool.list_discord_threads())

    assert result["threads"][0]["membership"] == {"requester": "joined", "bot": "not_joined"}
    assert [item["thread_id"] for item in result["threads"]] == [THREAD]
    assert result["coverage"]["excluded_threads"] == 1
    assert result["coverage"]["guild_id"] == GUILD


async def test_active_thread_collection_checks_requester_membership_before_listing(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/guilds/{GUILD}/members/{USER}", (404, {}))

    result = json.loads(await tool.list_discord_threads())

    assert result["error"] == "not_found"
    assert not rest.calls("GET", f"/guilds/{GUILD}/threads/active")


@pytest.mark.parametrize("mode", ["active", "public_archived"])
async def test_thread_listing_marks_missing_audience_evidence_as_unavailable(setup, mode):
    tool, rest, _ = setup
    raw = thread()
    if mode == "active":
        rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": [raw]}))
    else:
        raw["thread_metadata"]["archived"] = True
        rest.answer("GET", f"/channels/{CHANNEL}/threads/archived/public", (200, {"threads": [raw], "has_more": False}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (403, {}))

    result = json.loads(await tool.list_discord_threads(mode=mode))

    assert result["threads"] == []
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["unavailable_threads"] == 1
    assert result["coverage"]["unavailable_reason"] == "read_audience_evidence_unavailable"
    assert result["coverage"]["excluded_threads"] == 0
    assert result["coverage"]["next_before"] is None


@pytest.mark.parametrize("name,args", [
    ("create_discord_thread", {"parent_channel_id": CHANNEL, "name": "Planning", "kind": "public"}),
    ("update_discord_thread", {"thread_id": THREAD, "name": "Renamed"}),
    ("change_discord_thread_member", {"thread_id": THREAD, "action": "join"}),
    ("end_discord_poll", {"message_id": MESSAGE}),
])
@pytest.mark.parametrize("stop", [False, True])
async def test_native_write_refuses_turn_ending_during_read(setup, monkeypatch, name, args, stop):
    tool, rest, metadata = setup
    allow_writes(rest, MANAGE_THREADS)
    rest.answer("GET", f"/channels/{THREAD}", (200, thread()))
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {
        "id": MESSAGE, "channel_id": CHANNEL, "author": {"id": BOT}, "poll": {"answers": []},
    }))
    host = ToolHost([])
    host.serve("discord", [tool])
    turn = BoundTurn("turn", platform="discord", metadata=metadata)
    host.bind("conversation", turn)
    original = tool.platform._client.mapping
    entered, resume = asyncio.Event(), asyncio.Event()

    async def read(method, path, **kwargs):
        result = await original(method, path, **kwargs)
        if method == "GET" and not entered.is_set():
            entered.set()
            await resume.wait()
        return result

    monkeypatch.setattr(tool.platform._client, "mapping", read)
    running = asyncio.create_task(host.run_confirmed(ToolCall("conversation", name, args), turn))
    await asyncio.wait_for(entered.wait(), 2)
    if stop:
        turn.stopped = True
    else:
        host.release("conversation", turn.request_id)
    resume.set()
    assert json.loads(await running)["error"] == "turn_ended"
    assert not any(request.method != "GET" for request in rest.requests)


async def test_turn_ending_during_rate_limit_wait_prevents_write_retry(setup, monkeypatch):
    tool, rest, metadata = setup
    allow_writes(rest, MANAGE_THREADS)
    rest.answer("GET", f"/channels/{THREAD}", (200, thread()))
    path = f"/channels/{THREAD}/thread-members/@me"
    rest.answer("PUT", path, (429, {"retry_after": 1}), (204, b""))
    entered, resume = asyncio.Event(), asyncio.Event()

    async def wait(seconds):
        entered.set()
        await resume.wait()

    monkeypatch.setattr(tool.platform._client, "_sleep", wait)
    host = ToolHost([])
    host.serve("discord", [tool])
    turn = BoundTurn("turn", platform="discord", metadata=metadata)
    host.bind("conversation", turn)
    call = ToolCall("conversation", "change_discord_thread_member", {"thread_id": THREAD, "action": "join"})
    running = asyncio.create_task(host.run_confirmed(call, turn))
    await asyncio.wait_for(entered.wait(), 2)
    host.release("conversation", turn.request_id)
    resume.set()
    assert json.loads(await running)["error"] == "turn_ended"
    assert len(rest.calls("PUT", path)) == 1
