"""The Discord posting tool: where a turn's answer goes, the thread of a message it names, and nowhere else."""

import json

import httpx
import pytest
from discord_transport import TOKEN, FakeRest

from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.settings import DiscordSettings

CHANNEL = "1000000000000000300"
MESSAGE = "1000000000000000800"
OTHER = "1000000000000000301"


@pytest.fixture
def rest():
    found = FakeRest()
    found.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 0}))
    found.answer("GET", f"/channels/{MESSAGE}", (404, {}))
    found.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {"id": MESSAGE, "channel_id": CHANNEL}))
    found.answer("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads", (201, {"id": MESSAGE, "type": 11, "parent_id": CHANNEL}))
    found.answer("POST", f"/channels/{MESSAGE}/messages", (200, {"id": "1000000000000000901", "channel_id": MESSAGE}))
    found.answer("POST", f"/channels/{CHANNEL}/messages", (200, {"id": "1000000000000000902", "channel_id": CHANNEL}))
    return found


def _toolkit(rest, **metadata):
    platform = DiscordPlatform(DiscordSettings(token=TOKEN), client=DiscordClient(TOKEN, transport=rest.transport()))
    toolkit = platform.toolkits(lambda: {"discord_channel_id": CHANNEL, **metadata})[0]
    return toolkit


async def test_a_post_goes_where_the_turn_s_answer_goes(rest):
    result = json.loads(await _toolkit(rest, discord_reply_thread_id=MESSAGE).post_message("4"))

    assert result == {
        "ok": True, "chat_id": CHANNEL, "message_id": "1000000000000000901", "thread_id": MESSAGE,
        "parts": 1, "part_placements": [
            {"chat_id": CHANNEL, "message_id": "1000000000000000901", "thread_id": MESSAGE},
        ],
    }
    assert rest.calls("POST", f"/channels/{MESSAGE}/messages")[0].body["content"] == "4"


async def test_explicit_source_channel_needs_no_audience_approval(rest):
    result = json.loads(await _toolkit(rest).post_message("same conversation", chat_id=CHANNEL))

    assert result["ok"] is True and result["chat_id"] == CHANNEL
    assert len(rest.calls("POST", f"/channels/{CHANNEL}/messages")) == 1


async def test_a_named_message_is_answered_in_its_thread_and_none_posts_in_the_channel(rest):
    toolkit = _toolkit(rest)

    assert json.loads(await toolkit.post_message("in its thread", reply_to_message_id=MESSAGE, reply_mode="thread"))["thread_id"] == MESSAGE
    assert json.loads(await toolkit.post_message("in the channel"))["message_id"] == "1000000000000000902"


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"text": "x", "chat_id": OTHER}, "target_mismatch"),
        ({"text": "  "}, "nothing_to_post"),
        ({"text": "x", "reply_to_message_id": "general"}, "reply_to_message_id"),
    ],
)
async def test_a_call_that_does_not_fit_is_refused_by_name(rest, arguments, error):
    result = json.loads(await _toolkit(rest).post_message(**arguments))

    assert result["ok"] is False and error in result["error"] + result.get("detail", "")
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")


async def test_a_call_of_no_conversation_is_refused(rest):
    platform = DiscordPlatform(DiscordSettings(token=TOKEN), client=DiscordClient(TOKEN, transport=rest.transport()))
    toolkit = platform.toolkits(dict)[0]

    assert json.loads(await toolkit.post_message("x"))["error"] == "trusted_conversation_context_required"


def test_the_tool_is_named_post_message_on_every_platform(rest):
    assert [tool["name"] for tool in _toolkit(rest).get_tool_definitions()] == [
        "post_message", "forward_discord_message", "crosspost_discord_message",
    ]


async def test_split_top_level_post_reports_each_actual_placement(rest):
    first, second = "1000000000000000902", "1000000000000000903"
    rest.answer("POST", f"/channels/{CHANNEL}/messages", (200, {"id": first, "channel_id": CHANNEL}), (200, {"id": second, "channel_id": CHANNEL}))

    result = json.loads(await _toolkit(rest).post_message("word " * 500))

    assert result["ok"] is True and result["part_message_ids"] == [first, second]
    assert result["part_placements"] == [
        {"chat_id": CHANNEL, "message_id": first, "thread_id": ""},
        {"chat_id": CHANNEL, "message_id": second, "thread_id": ""},
    ]
    bodies = [call.body for call in rest.calls("POST", f"/channels/{CHANNEL}/messages")]
    assert all(body["enforce_nonce"] is True for body in bodies)
    assert len({body["nonce"] for body in bodies}) == len(bodies)


async def test_failure_after_first_part_reports_the_visible_post(rest):
    first = "1000000000000000902"
    rest.answer("POST", f"/channels/{CHANNEL}/messages", (200, {"id": first, "channel_id": CHANNEL}), (403, {"code": 50013}))

    result = json.loads(await _toolkit(rest).post_message("word " * 500))

    assert result["ok"] is False and result["error"] == "forbidden"
    assert result["parts"] == 1 and result["part_message_ids"] == [first]
    assert result["part_placements"] == [{"chat_id": CHANNEL, "message_id": first, "thread_id": ""}]
    assert "duplicate" in result["detail"]


GUILD = "1000000000000000200"
USER = "1000000000000000400"
BOT = "1000000000000000116"
NEW = "1000000000000000401"


def audience(rest, channel, *, deny=()):
    rest.answer("GET", f"/channels/{channel}", (200, {
        "id": channel, "type": 0, "guild_id": GUILD,
        "permission_overwrites": [{"id": user, "type": 1, "deny": str(1 << 10), "allow": "0"} for user in deny],
    }))
    rest.answer("GET", f"/guilds/{GUILD}", (200, {
        "id": GUILD, "owner_id": "owner", "roles": [{"id": GUILD, "permissions": str((1 << 10) | (1 << 11) | (1 << 38))}],
    }))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER, NEW)
    ]))


def requester_toolkit(rest, requester=USER, **metadata):
    toolkit = _toolkit(rest, discord_user_id=requester, **metadata)
    toolkit._platform.bot_user_id = BOT
    return toolkit


async def test_cross_channel_widening_asks_before_send_and_no_requester_refuses(rest):
    audience(rest, CHANNEL, deny=(NEW,))
    audience(rest, OTHER)
    toolkit = requester_toolkit(rest)
    assert await toolkit.widening_question("post_message", {"chat_id": OTHER})
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")
    unattended = requester_toolkit(rest, requester="")
    result = json.loads(await unattended.post_message("x", chat_id=OTHER))
    assert result["error"] == "write_target_widens_audience"
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")


async def test_cross_channel_same_readers_posts_without_question(rest):
    audience(rest, CHANNEL)
    audience(rest, OTHER)
    rest.answer("POST", f"/channels/{OTHER}/messages", (200, {"id": MESSAGE, "channel_id": OTHER}))
    toolkit = requester_toolkit(rest)
    assert await toolkit.widening_question("post_message", {"chat_id": OTHER}) is None
    host, _, call = bound_host(toolkit, {"text": "x", "chat_id": OTHER})
    result = json.loads(await host.call(call))
    assert result["chat_id"] == OTHER
    assert result["url"] == f"https://discord.com/channels/{GUILD}/{OTHER}/{MESSAGE}"


@pytest.mark.parametrize(("destination", "widening", "can_confirm"), [
    (CHANNEL, False, False),
    (OTHER, False, False),
    (OTHER, True, False),
    (OTHER, True, True),
])
async def test_mcp_post_preserves_audience_checks(rest, destination, widening, can_confirm):
    from belle_agent.backend import ToolCall
    from belle_agent.tools.host import BoundTurn, ToolHost
    from belle_agent.tools.server import PATH, ToolServer

    audience(rest, CHANNEL, deny=(NEW,) if widening else ())
    audience(rest, OTHER)
    rest.answer("POST", f"/channels/{destination}/messages", (200, {"id": MESSAGE, "channel_id": destination}))
    toolkit = requester_toolkit(rest)
    held = []

    async def confirm(call, turn, question):
        held.append((call, turn))
        return True

    host = ToolHost([toolkit], confirm=confirm if can_confirm else None)
    turn = BoundTurn("turn")
    host.bind("conversation", turn)

    async def conversation_of(meta, headers):
        return "conversation"

    arguments = {"text": "from MCP", "chat_id": destination}
    # The relay's preliminary decision belongs to a different call object.
    await host.authorize(ToolCall("conversation", "post_message", arguments, call_id="relay-call"))
    server = ToolServer(host, conversation_of)
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}") as client:
            response = await client.post(PATH, headers={"Authorization": f"Bearer {server.token}"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "post_message", "arguments": arguments},
            })
        result = json.loads(response.json()["result"]["content"][0]["text"])
    finally:
        await server.close()

    if widening:
        assert result["error"] == ("awaiting_confirmation" if can_confirm else "approval_required")
        assert not rest.calls("POST", f"/channels/{destination}/messages")
        if can_confirm:
            call, bound = held.pop()
            host.release("conversation", turn.request_id)
            result = json.loads(await host.run_confirmed(call, bound))
        else:
            assert not toolkit._audiences
            return
    assert result["ok"] is True and result["chat_id"] == destination
    assert len(rest.calls("POST", f"/channels/{destination}/messages")) == 1
    assert not toolkit._audiences


@pytest.mark.parametrize("widening", [False, True])
async def test_scope_ask_allows_approved_cross_channel_post(rest, widening):
    audience(rest, CHANNEL, deny=(NEW,) if widening else ())
    audience(rest, OTHER)
    rest.answer("POST", f"/channels/{OTHER}/messages", (200, {"id": MESSAGE, "channel_id": OTHER}))
    host, turn, call = bound_host(requester_toolkit(rest), {"text": "x", "chat_id": OTHER})
    turn.tools = {"post_message": "ask"}
    assert (await host.authorize(call)).action == "ask"
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")
    assert json.loads(await host.run_confirmed(call, turn))["ok"]
    assert len(rest.calls("POST", f"/channels/{OTHER}/messages")) == 1


async def test_member_overwrite_refuses_requester_access(rest):
    audience(rest, CHANNEL)
    audience(rest, OTHER, deny=(USER,))
    result = json.loads(await requester_toolkit(rest).post_message("x", chat_id=OTHER))
    assert result["error"] == "requester_cannot_access_target"
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")


async def test_dm_resolution_and_mutually_exclusive_ids(rest):
    audience(rest, CHANNEL, deny=(NEW,))
    toolkit = requester_toolkit(rest)
    assert await toolkit.widening_question("post_message", {"recipient_user_id": NEW})
    assert not rest.calls("POST", "/users/@me/channels")
    rest.answer("POST", "/users/@me/channels", (200, {"id": OTHER, "type": 1, "recipients": [{"id": NEW}]}))
    rest.answer("POST", f"/channels/{OTHER}/messages", (200, {"id": MESSAGE, "channel_id": OTHER}))
    host, turn, call = bound_host(toolkit, {"text": "x", "recipient_user_id": NEW})
    await host.authorize(call)
    assert json.loads(await host.run_confirmed(call, turn))["chat_id"] == OTHER
    assert json.loads(await toolkit.post_message("x", chat_id=OTHER, recipient_user_id=NEW))["ok"] is False


@pytest.mark.parametrize("cached", [False, True])
async def test_foreign_reply_thread_never_receives_a_post(rest, cached):
    toolkit = _toolkit(rest)
    foreign = {"id": MESSAGE, "type": 11, "parent_id": OTHER}
    if cached:
        toolkit._platform._learn_channel(foreign)
    else:
        rest.answer("GET", f"/channels/{MESSAGE}", (200, foreign))
    result = json.loads(await toolkit.post_message("x", reply_to_message_id=MESSAGE, reply_mode="thread"))
    assert result["error"] == "reply_target_outside_channel"
    assert not rest.calls("POST", f"/channels/{MESSAGE}/messages")


async def test_foreign_message_reference_is_refused(rest):
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {"id": MESSAGE, "channel_id": OTHER}))
    assert json.loads(await _toolkit(rest).post_message("x", reply_to_message_id=MESSAGE, reply_mode="thread"))["error"] == "reply_target_outside_channel"


async def test_private_thread_membership_limits_readers(rest):
    from belle_agent.platforms.discord.targets import TargetResolver

    audience(rest, CHANNEL)
    rest.answer("GET", f"/channels/{OTHER}", (200, {"id": OTHER, "type": 12, "guild_id": GUILD, "parent_id": CHANNEL}))
    rest.answer("GET", f"/channels/{OTHER}/thread-members", (200, [{"user_id": USER}, {"user_id": BOT}]))
    target = await TargetResolver(requester_toolkit(rest)._platform).resolve(OTHER, USER)
    assert NEW not in target.readers


async def test_detached_run_needs_requester_for_new_destination(rest):
    audience(rest, OTHER)
    toolkit = requester_toolkit(rest, requester="", discord_channel_id="")
    assert json.loads(await toolkit.post_message("x", chat_id=OTHER))["error"] == "write_target_widens_audience"
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")


def test_role_and_member_overwrites_follow_discord_order():
    from belle_agent.platforms.discord.targets import VIEW, permissions

    guild = {"id": GUILD, "owner_id": "owner", "roles": [
        {"id": GUILD, "permissions": str(VIEW)}, {"id": OTHER, "permissions": "0"},
    ]}
    channel = {"permission_overwrites": [
        {"id": GUILD, "type": 0, "deny": str(VIEW), "allow": "0"},
        {"id": OTHER, "type": 0, "deny": "0", "allow": str(VIEW)},
        {"id": USER, "type": 1, "deny": str(VIEW), "allow": "0"},
    ]}
    assert permissions(guild, channel, {"user": {"id": USER}, "roles": [OTHER]}) & VIEW == 0
    assert permissions(guild, channel, {"user": {"id": NEW}, "roles": [OTHER]}) & VIEW


async def test_private_source_thread_to_parent_requires_approval(rest):
    audience(rest, CHANNEL)
    rest.answer("GET", f"/channels/{MESSAGE}", (200, {"id": MESSAGE, "type": 12, "guild_id": GUILD, "parent_id": CHANNEL}))
    rest.answer("GET", f"/channels/{MESSAGE}/thread-members", (200, [{"user_id": USER}, {"user_id": BOT}]))
    toolkit = requester_toolkit(rest, discord_thread_id=MESSAGE)
    assert await toolkit.widening_question("post_message", {"chat_id": CHANNEL})
    assert await toolkit.widening_question("post_message", {}) is None


async def test_uncertain_delivery_is_not_retried(rest):
    import httpx

    toolkit = _toolkit(rest)
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("accepted but response lost", request=request)

    toolkit._platform._learn_channel({"id": CHANNEL, "type": 0})
    toolkit._platform._client = DiscordClient(TOKEN, transport=httpx.MockTransport(timeout))
    result = json.loads(await toolkit.post_message("x"))
    assert result["error"] == "delivery_uncertain_do_not_retry"
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["enforce_nonce"] is True and body["nonce"].isdigit()


@pytest.mark.parametrize("status", [403, 404])
async def test_unavailable_destination_never_falls_back(rest, status):
    rest.answer("GET", f"/channels/{OTHER}", (status, {}))
    result = json.loads(await requester_toolkit(rest).post_message("x", chat_id=OTHER))
    assert result["ok"] is False
    assert not rest.calls("POST", f"/channels/{CHANNEL}/messages")
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")


async def test_another_guild_is_checked_using_its_own_roles_and_members(rest):
    other_guild = "1000000000000000201"
    audience(rest, CHANNEL)
    audience(rest, OTHER)
    rest.answer("GET", f"/channels/{OTHER}", (200, {"id": OTHER, "type": 0, "guild_id": other_guild}))
    rest.answer("GET", f"/guilds/{other_guild}", (200, {
        "id": other_guild, "owner_id": "owner", "roles": [{"id": other_guild, "permissions": str((1 << 10) | (1 << 11))}],
    }))
    rest.answer("GET", f"/guilds/{other_guild}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER, NEW)
    ]))
    rest.answer("POST", f"/channels/{OTHER}/messages", (200, {"id": MESSAGE, "channel_id": OTHER}))
    host, _, call = bound_host(requester_toolkit(rest), {"text": "x", "chat_id": OTHER})
    assert json.loads(await host.call(call))["ok"] is True
    assert rest.calls("GET", f"/guilds/{other_guild}/members")



def bound_host(toolkit, arguments):
    from belle_agent.backend import ToolCall
    from belle_agent.tools.host import BoundTurn, ToolHost

    host = ToolHost([toolkit])
    turn = BoundTurn("turn")
    host.bind("conversation", turn)
    call = ToolCall("conversation", "post_message", arguments, call_id="call")
    return host, turn, call


@pytest.mark.parametrize("scope", ["allow", "ask"])
@pytest.mark.parametrize("initial_widening", [False, True])
async def test_call_bound_audience_change_refuses_after_decision(rest, initial_widening, scope):
    audience(rest, CHANNEL, deny=(NEW,))
    audience(rest, OTHER, deny=() if initial_widening else (NEW,))
    toolkit = requester_toolkit(rest)
    host, turn, call = bound_host(toolkit, {"text": "x", "chat_id": OTHER})
    turn.tools = {"post_message": scope}
    verdict = await host.authorize(call)
    assert bool(verdict.question) is (initial_widening or scope == "ask")
    # A newly admitted user changes the exact reader set after either decision.
    extra = "1000000000000000999"
    audience(rest, OTHER)
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER, NEW, extra)
    ]))
    result = json.loads(await host.run_confirmed(call, turn))
    assert result["error"] == "post_audience_changed"
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")
    assert not toolkit._audiences


async def test_direct_toolhost_call_checks_no_question_snapshot_again(rest):
    from belle_agent.tools.host import ToolHost

    audience(rest, CHANNEL)
    audience(rest, OTHER)
    toolkit = requester_toolkit(rest)
    _, turn, call = bound_host(toolkit, {"text": "x", "chat_id": OTHER})

    class ChangingHost(ToolHost):
        async def run_confirmed(self, call, turn):
            audience(rest, CHANNEL, deny=(NEW,))
            return await super().run_confirmed(call, turn)

    host = ChangingHost([toolkit])
    host.bind("conversation", turn)
    result = json.loads(await host.call(call))
    assert result["error"] == "post_audience_changed"
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")


async def test_widening_without_call_bound_decision_refuses(rest):
    audience(rest, CHANNEL, deny=(NEW,))
    audience(rest, OTHER)
    toolkit = requester_toolkit(rest)
    host, turn, call = bound_host(toolkit, {"text": "x", "chat_id": OTHER})
    assert json.loads(await host.run_confirmed(call, turn))["error"] == "post_audience_check_required"
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")


async def test_denied_or_expired_call_releases_audience_snapshot(rest):
    import gc

    audience(rest, CHANNEL, deny=(NEW,))
    audience(rest, OTHER)
    toolkit = requester_toolkit(rest)
    host, _, call = bound_host(toolkit, {"text": "x", "chat_id": OTHER})
    await host.authorize(call)
    assert len(toolkit._audiences) == 1
    del call
    gc.collect()
    assert not toolkit._audiences


async def test_default_gateway_intents_do_not_bypass_missing_rest_member_access(rest):
    assert DiscordSettings(token=TOKEN).guild_members is False
    audience(rest, CHANNEL)
    audience(rest, OTHER)
    rest.answer("GET", f"/guilds/{GUILD}/members", (403, {"code": 50001}))
    host, _, call = bound_host(requester_toolkit(rest), {"text": "x", "chat_id": OTHER})
    result = json.loads(await host.call(call))
    assert result["error"] == "forbidden"
    assert f"/guilds/{GUILD}/members" in result["detail"]
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")


@pytest.mark.parametrize("mode", ["auto", "top_level", "reply", "thread"])
@pytest.mark.parametrize("fail_second", [False, True])
async def test_each_mode_keeps_split_placement_and_confirmed_failures(rest, mode, fail_second):
    target = MESSAGE if mode == "thread" else CHANNEL
    first, second = "1000000000000000904", "1000000000000000905"
    rest.answer("POST", f"/channels/{target}/messages", (200, {"id": first, "channel_id": target}),
                (403, {"code": 50013}) if fail_second else (200, {"id": second, "channel_id": target}))
    arguments = {"reply_mode": mode}
    if mode in ("reply", "thread"):
        arguments["reply_to_message_id"] = MESSAGE
    result = json.loads(await _toolkit(rest).post_message("word " * 500, **arguments))
    ids = [first] if fail_second else [first, second]
    assert result["ok"] is not fail_second
    assert result["part_message_ids"] == ids
    assert result["part_placements"] == [
        {"chat_id": CHANNEL, "thread_id": MESSAGE if mode == "thread" else "", "message_id": value} for value in ids
    ]
    for request in rest.calls("POST", f"/channels/{target}/messages"):
        assert ("message_reference" in request.body) is (mode == "reply")
        if mode == "reply":
            assert request.body["message_reference"] == {"message_id": MESSAGE, "channel_id": CHANNEL, "fail_if_not_exists": True}
    if mode != "thread":
        assert not rest.calls("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads")


@pytest.mark.parametrize("arguments,field", [
    ({"reply_mode": "other"}, "reply_mode"),
    ({"reply_mode": "top_level", "reply_to_message_id": MESSAGE}, "reply_to_message_id"),
    ({"reply_mode": "reply"}, "reply_to_message_id"),
    ({"reply_mode": "thread"}, "reply_to_message_id"),
    ({"reply_mode": "reply", "reply_to_message_id": MESSAGE, "thread": {}}, "thread"),
    ({"reply_mode": "thread", "reply_to_message_id": MESSAGE, "thread": {"applied_tag_ids": []}}, "thread"),
    ({"reply_mode": "thread", "reply_to_message_id": MESSAGE, "thread": {"name": ""}}, "thread.name"),
    ({"reply_mode": "thread", "reply_to_message_id": MESSAGE, "thread": {"auto_archive_minutes": 61}}, "thread.auto_archive_minutes"),
    ({"reply_mode": "thread", "reply_to_message_id": MESSAGE, "thread": {"rate_limit_per_user": True}}, "thread.rate_limit_per_user"),
    ({"reply_mode": "thread", "recipient_user_id": USER, "reply_to_message_id": MESSAGE}, "reply_mode"),
])
async def test_placement_conflicts_do_not_make_remote_calls(rest, arguments, field):
    result = json.loads(await _toolkit(rest).post_message("x", **arguments))
    assert result["ok"] is False and field in result["error"]
    assert not rest.requests


@pytest.mark.parametrize("mode", ["auto", "top_level", "reply"])
async def test_dm_uses_native_reply_without_creating_a_thread(rest, mode):
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 1}))
    arguments = {"reply_mode": mode}
    if mode != "top_level":
        arguments["reply_to_message_id"] = MESSAGE
    result = json.loads(await _toolkit(rest, discord_channel_type="dm").post_message("word " * 500, **arguments))
    assert result["ok"] is True and result["thread_id"] == ""
    assert len(rest.calls("POST", f"/channels/{CHANNEL}/messages")) == 2
    assert not rest.calls("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads")


async def test_thread_mode_in_dm_refuses_without_remote_calls(rest):
    result = json.loads(await _toolkit(rest, discord_channel_type="dm").post_message("x", reply_mode="thread", reply_to_message_id=MESSAGE))
    assert result["error"] == "reply_mode_thread_unsupported_in_dm"
    assert not rest.requests


async def test_thread_creation_options_map_to_discord_fields(rest):
    result = json.loads(await _toolkit(rest).post_message("x", reply_mode="thread", reply_to_message_id=MESSAGE,
        thread={"name": "Topic", "auto_archive_minutes": 4320, "rate_limit_per_user": 60}))
    assert result["ok"] is True
    assert rest.calls("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads")[0].body == {
        "name": "Topic", "auto_archive_duration": 4320, "rate_limit_per_user": 60,
    }


@pytest.mark.parametrize("cached", [False, True])
async def test_native_reply_rejects_foreign_message(rest, cached):
    toolkit = _toolkit(rest)
    if cached:
        toolkit._platform._learn_message(MESSAGE, OTHER, "foreign")
    else:
        rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {"id": MESSAGE, "channel_id": OTHER}))
    result = json.loads(await toolkit.post_message("x", reply_mode="reply", reply_to_message_id=MESSAGE))
    assert result["error"] == "reply_to_message_id_outside_target_channel"
    assert not [request for request in rest.requests if request.method == "POST"]


@pytest.mark.parametrize("mode", ["auto", "top_level", "reply", "thread"])
async def test_existing_private_thread_validates_membership_and_reports_thread(rest, mode):
    audience(rest, CHANNEL)
    rest.answer("GET", f"/channels/{OTHER}", (200, {"id": OTHER, "type": 12, "guild_id": GUILD, "parent_id": CHANNEL}))
    rest.answer("GET", f"/channels/{OTHER}/thread-members", (200, [{"user_id": USER}, {"user_id": BOT}]))
    rest.answer("GET", f"/channels/{OTHER}/messages/{MESSAGE}", (200, {"id": MESSAGE, "channel_id": OTHER}))
    rest.answer("POST", f"/channels/{OTHER}/messages", (200, {"id": MESSAGE, "channel_id": OTHER}))
    toolkit = requester_toolkit(rest, discord_reply_thread_id=NEW)
    args = {"text": "word " * 500, "chat_id": OTHER, "reply_mode": mode}
    if mode in ("reply", "thread"):
        args["reply_to_message_id"] = MESSAGE
    host, _, call = bound_host(toolkit, args)
    result = json.loads(await host.call(call))
    assert result["ok"] is True and result["thread_id"] == OTHER
    assert all(part["thread_id"] == OTHER for part in result["part_placements"])
    assert rest.calls("GET", f"/channels/{OTHER}/thread-members")
    assert not rest.calls("POST", f"/channels/{OTHER}/messages/{MESSAGE}/threads")


async def test_existing_thread_creation_options_refuse_before_any_post(rest):
    audience(rest, CHANNEL)
    rest.answer("GET", f"/channels/{MESSAGE}", (200, {"id": MESSAGE, "type": 11, "guild_id": GUILD, "parent_id": CHANNEL}))
    result = json.loads(await requester_toolkit(rest).post_message("x", reply_mode="thread", reply_to_message_id=MESSAGE, thread={"name": "ignored"}))
    assert result["error"] == "thread_options_require_new_thread"
    assert not [request for request in rest.requests if request.method == "POST"]


@pytest.mark.parametrize("missing", ["parent", "bot", "requester"])
async def test_existing_thread_requires_parent_and_member_access(rest, missing):
    audience(rest, CHANNEL)
    thread = {"id": OTHER, "type": 12, "guild_id": GUILD, "parent_id": CHANNEL}
    if missing == "parent":
        thread.pop("parent_id")
    rest.answer("GET", f"/channels/{OTHER}", (200, thread))
    members = [user for user in (BOT, USER) if user != (BOT if missing == "bot" else USER) or missing == "parent"]
    rest.answer("GET", f"/channels/{OTHER}/thread-members", (200, [{"user_id": user} for user in members]))
    result = json.loads(await requester_toolkit(rest).post_message("x", chat_id=OTHER, reply_mode="thread"))
    assert result["ok"] is False
    assert not [request for request in rest.requests if request.method == "POST"]


async def test_source_thread_reference_must_belong_to_that_thread(rest):
    audience(rest, CHANNEL)
    rest.answer("GET", f"/channels/{OTHER}", (200, {"id": OTHER, "type": 11, "guild_id": GUILD, "parent_id": CHANNEL}))
    toolkit = requester_toolkit(rest, discord_thread_id=OTHER)
    assert await toolkit.widening_question("post_message", {"reply_mode": "thread", "reply_to_message_id": MESSAGE}) is None
    toolkit._platform._learn_message(MESSAGE, CHANNEL, "parent message")
    host, _, call = bound_host(toolkit, {"text": "x", "reply_mode": "thread", "reply_to_message_id": MESSAGE})
    result = json.loads(await host.call(call))
    assert result["error"] == "reply_to_message_id_outside_target_channel"
    assert not [request for request in rest.requests if request.method == "POST"]


async def test_auto_source_mode_follows_configured_thread_preference(rest):
    native = json.loads(await _toolkit(rest).post_message("x", reply_to_message_id=MESSAGE))
    assert native["ok"] is True and native["thread_id"] == ""
    threaded = json.loads(await _toolkit(rest, discord_reply_thread_id=MESSAGE).post_message("x", reply_to_message_id=MESSAGE))
    assert threaded["ok"] is True and threaded["thread_id"] == MESSAGE


async def test_other_destination_auto_is_native_even_when_source_uses_threads(rest):
    audience(rest, CHANNEL)
    audience(rest, OTHER)
    rest.answer("GET", f"/channels/{OTHER}/messages/{MESSAGE}", (200, {"id": MESSAGE, "channel_id": OTHER}))
    rest.answer("POST", f"/channels/{OTHER}/messages", (200, {"id": MESSAGE, "channel_id": OTHER}))
    host, _, call = bound_host(requester_toolkit(rest, discord_reply_thread_id=MESSAGE), {
        "text": "x", "chat_id": OTHER, "reply_to_message_id": MESSAGE,
    })
    result = json.loads(await host.call(call))
    assert result["ok"] is True and result["thread_id"] == ""
    assert rest.calls("POST", f"/channels/{OTHER}/messages")[0].body["message_reference"]["message_id"] == MESSAGE
    assert not rest.calls("POST", f"/channels/{OTHER}/messages/{MESSAGE}/threads")


async def test_explicit_top_level_overrides_source_thread_preference(rest):
    result = json.loads(await _toolkit(rest, discord_reply_thread_id=MESSAGE).post_message("x", reply_mode="top_level"))
    assert result["ok"] is True and result["thread_id"] == ""
    assert not rest.calls("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads")


async def test_explicit_top_level_from_source_thread_posts_in_parent_channel(rest):
    audience(rest, CHANNEL)
    rest.answer("GET", f"/channels/{OTHER}", (200, {"id": OTHER, "type": 11, "guild_id": GUILD, "parent_id": CHANNEL}))
    toolkit = requester_toolkit(rest, discord_thread_id=OTHER, discord_reply_thread_id=OTHER)
    host, _, call = bound_host(toolkit, {"text": "x", "reply_mode": "top_level"})
    result = json.loads(await host.call(call))
    assert result["ok"] is True and result["thread_id"] == ""
    assert rest.calls("POST", f"/channels/{CHANNEL}/messages")
    assert not rest.calls("POST", f"/channels/{OTHER}/messages")


@pytest.mark.parametrize("arguments", [{"reply_mode": "top_level"}, {"chat_id": CHANNEL}])
async def test_private_thread_parent_requires_audience_approval(rest, arguments):
    audience(rest, CHANNEL)
    rest.answer("GET", f"/channels/{OTHER}", (200, {"id": OTHER, "type": 12, "guild_id": GUILD, "parent_id": CHANNEL}))
    rest.answer("GET", f"/channels/{OTHER}/thread-members", (200, [{"user_id": USER}, {"user_id": BOT}]))
    toolkit = requester_toolkit(rest, discord_thread_id=OTHER, discord_reply_thread_id=OTHER)
    assert await toolkit.widening_question("post_message", {"text": "x", **arguments})
    result = json.loads(await toolkit.post_message("x", **arguments))
    assert result["error"] == "post_audience_check_required"
    assert not [request for request in rest.requests if request.method == "POST"]


@pytest.mark.parametrize("body", [
    b"not JSON", None, [], {},
    {"id": "invalid", "channel_id": CHANNEL},
    {"id": 1000000000000000901, "channel_id": CHANNEL},
    {"id": "1000000000000000901"},
    {"id": "1000000000000000901", "channel_id": OTHER},
])
@pytest.mark.parametrize("partial", [False, True])
async def test_malformed_success_reports_uncertainty_and_preserves_prior_receipts(rest, body, partial):
    path = f"/channels/{CHANNEL}/messages"
    accepted = {"id": "1000000000000000901", "channel_id": CHANNEL}
    rest.answer("POST", path, *((200, accepted), (200, body)) if partial else ((200, body),))
    result = json.loads(await _toolkit(rest).post_message("x" * (2100 if partial else 1)))
    assert result["error"] == "delivery_uncertain_do_not_retry"
    assert len(rest.calls("POST", path)) == (2 if partial else 1)
    if partial:
        assert result["parts"] == 1 and result["part_message_ids"] == [accepted["id"]]
        assert result["part_placements"] == [{"chat_id": CHANNEL, "message_id": accepted["id"], "thread_id": ""}]
    else:
        assert "message_id" not in result
