"""The REST client: the bot's token and user agent, refusals by name, rate-limit buckets, 429s and the invalid-request stop."""

import asyncio
import logging

import httpx
import pytest
from discord_transport import TOKEN, Clock, FakeRest

from belle_agent.platforms.discord.client import USER_AGENT, DiscordClient, DiscordError, route_key

CHANNEL = "1000000000000000300"
OTHER = "1000000000000000301"


@pytest.fixture
def rest():
    return FakeRest()


@pytest.fixture
def clock():
    return Clock()


def _client(rest, clock, **kwargs):
    return DiscordClient(TOKEN, transport=rest.transport(), clock=clock, sleep=clock.sleep, **kwargs)


async def test_a_request_carries_the_bot_token_and_the_user_agent_and_returns_the_body(rest, clock):
    rest.answer("GET", "/users/@me", (200, {"id": "1", "username": "belle"}))

    found = await _client(rest, clock).request("GET", "/users/@me")

    (sent,) = rest.requests
    assert found["username"] == "belle"
    assert (sent.headers["authorization"], sent.headers["user-agent"]) == (f"Bot {TOKEN}", USER_AGENT)


async def test_a_refusal_is_named_with_discord_s_code_and_holds_no_token(rest, clock):
    rest.answer("POST", f"/channels/{CHANNEL}/messages", (403, {"code": 50013, "message": "Missing Permissions"}))

    with pytest.raises(DiscordError) as raised:
        await _client(rest, clock).request("POST", f"/channels/{CHANNEL}/messages", json={"content": "hi"})

    assert (raised.value.code, raised.value.status, raised.value.discord_code) == ("forbidden", 403, 50013)
    assert TOKEN not in str(raised.value)


async def test_a_429_is_waited_out_as_long_as_it_asks_and_the_request_sent_again(rest, clock):
    rest.answer("GET", f"/channels/{CHANNEL}", (429, {"retry_after": 1.25, "global": False}), (200, {"id": CHANNEL}))

    found = await _client(rest, clock).request("GET", f"/channels/{CHANNEL}")

    assert found == {"id": CHANNEL} and clock.slept == [1.25] and len(rest.requests) == 2


async def test_a_rate_limit_that_lasts_past_the_retries_is_raised_with_its_wait(rest, clock):
    rest.answer("GET", f"/channels/{CHANNEL}", (429, {"retry_after": 0.5, "global": False}))

    with pytest.raises(DiscordError) as raised:
        await _client(rest, clock).request("GET", f"/channels/{CHANNEL}")

    assert (raised.value.code, raised.value.retry_after) == ("rate_limited", 0.5) and len(rest.requests) == 4


async def test_a_spent_bucket_is_waited_for_on_its_route_and_resource_only(rest, clock):
    spent = {"X-RateLimit-Bucket": "abc", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2.5"}
    rest.answer("POST", f"/channels/{CHANNEL}/messages", (200, {"id": "9"}, spent), (200, {"id": "10"}))
    client = _client(rest, clock)

    await client.request("POST", f"/channels/{CHANNEL}/messages", json={"content": "a"})
    await client.request("POST", f"/channels/{OTHER}/messages", json={"content": "b"})
    assert clock.slept == []
    await client.request("POST", f"/channels/{CHANNEL}/messages", json={"content": "c"})
    assert clock.slept == [2.5]


async def test_a_global_rate_limit_holds_every_route(rest, clock):
    rest.answer("GET", f"/channels/{CHANNEL}", (429, {"retry_after": 3.0, "global": True}, {"X-RateLimit-Global": "true"}), (200, {}))
    client = _client(rest, clock)

    await client.request("GET", f"/channels/{CHANNEL}")
    clock.now -= 1.0  # the other route is called while the global limit still has a second to run
    await client.request("GET", f"/channels/{OTHER}")

    assert clock.slept == [3.0, 1.0]


async def test_the_client_stops_calling_before_the_invalid_requests_would_restrict_the_address(rest, clock):
    rest.answer("GET", f"/channels/{CHANNEL}", (403, {"code": 50001}))
    client = _client(rest, clock, invalid_budget=3)
    for _ in range(3):
        with pytest.raises(DiscordError, match="forbidden"):
            await client.request("GET", f"/channels/{CHANNEL}")

    with pytest.raises(DiscordError) as raised:
        await client.request("GET", f"/channels/{OTHER}")
    assert raised.value.code == "invalid_request_budget" and len(rest.requests) == 3

    clock.now += 601
    rest.answer("GET", f"/channels/{OTHER}", (200, {"id": OTHER}))
    assert await client.request("GET", f"/channels/{OTHER}") == {"id": OTHER}


async def test_no_answer_is_unreachable_and_a_body_that_is_no_object_is_malformed(rest, clock):
    rest.answer("GET", "/gateway/bot", (200, ["not", "an", "object"]))
    with pytest.raises(DiscordError, match="bad_response"):
        await _client(rest, clock).gateway_bot()
    rest.unreachable = True
    with pytest.raises(DiscordError, match="unreachable"):
        await _client(rest, clock).request("GET", "/gateway/bot")


def test_a_route_keeps_its_major_id_and_names_every_other_id_by_its_place():
    assert route_key("PATCH", f"/channels/{CHANNEL}/messages/1000000000000000999") == f"PATCH /channels/{CHANNEL}/messages/:id"
    assert route_key("GET", "/users/1000000000000000999") == "GET /users/:id"


async def _gateway_server(on_socket):
    from aiohttp import WSMsgType, web

    async def handle(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await on_socket(socket)
        async for message in socket:
            if message.type == WSMsgType.CLOSE:
                break
        return socket

    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"ws://127.0.0.1:{port}/"


async def test_a_connection_discord_closes_releases_its_http_session():
    pytest.importorskip("aiohttp")
    from belle_agent.platforms.discord.client import _open_aiohttp

    async def hello_then_close(socket):
        await socket.send_str('{"op": 10, "d": {"heartbeat_interval": 41250}}')
        await socket.close(code=4000)

    runner, url = await _gateway_server(hello_then_close)
    try:
        socket = await _open_aiohttp(url)
        assert (await socket.receive())["op"] == 10
        assert await socket.receive() is None

        assert socket.close_code == 4000 and socket._session.closed
    finally:
        await runner.cleanup()


async def test_closing_a_connection_releases_its_http_session_even_when_the_close_fails():
    pytest.importorskip("aiohttp")
    from belle_agent.platforms.discord.client import _open_aiohttp

    async def stay_open(socket):
        return None

    runner, url = await _gateway_server(stay_open)
    try:
        socket = await _open_aiohttp(url)

        async def broken_close(**kwargs):
            raise ConnectionResetError

        socket._socket.close = broken_close
        with pytest.raises(ConnectionResetError):
            await socket.close(1000)

        assert socket._session.closed
    finally:
        await runner.cleanup()


INTERACTION = "1000000000000000900"
INTERACTION_TOKEN = "aW50ZXJhY3Rpb246c2VjcmV0LXRva2Vu"
APPLICATION = "1000000000000000116"


async def test_an_interaction_is_answered_on_its_callback_and_edited_and_followed_up_on_its_webhook(rest, clock):
    client = _client(rest, clock)

    await client.respond(INTERACTION, INTERACTION_TOKEN, {"type": 6})
    await client.edit_response(APPLICATION, INTERACTION_TOKEN, {"components": []})
    await client.followup(APPLICATION, INTERACTION_TOKEN, {"content": "no", "flags": 64})

    assert [(sent.method, sent.path, sent.body) for sent in rest.requests] == [
        ("POST", f"/interactions/{INTERACTION}/{INTERACTION_TOKEN}/callback", {"type": 6}),
        ("PATCH", f"/webhooks/{APPLICATION}/{INTERACTION_TOKEN}/messages/@original", {"components": []}),
        ("POST", f"/webhooks/{APPLICATION}/{INTERACTION_TOKEN}", {"content": "no", "flags": 64}),
    ]


async def test_an_interaction_token_is_named_in_no_error_and_no_log_line(rest, clock, caplog):
    caplog.set_level(logging.INFO, logger="belle_agent.platforms.discord.client")
    rest.answer("POST", f"/webhooks/{APPLICATION}/{INTERACTION_TOKEN}", (429, {"retry_after": 0.5, "global": False}))
    rest.answer("POST", f"/interactions/{INTERACTION}/{INTERACTION_TOKEN}/callback", (404, {"code": 10062}))
    client = _client(rest, clock)

    with pytest.raises(DiscordError) as late:
        await client.respond(INTERACTION, INTERACTION_TOKEN, {"type": 6})
    with pytest.raises(DiscordError) as limited:
        await client.followup(APPLICATION, INTERACTION_TOKEN, {"content": "hi"})
    rest.unreachable = True
    with pytest.raises(DiscordError) as unreachable:
        await client.edit_response(APPLICATION, INTERACTION_TOKEN, {"content": "hi"})

    assert late.value.path == f"/interactions/{INTERACTION}/:token/callback" and late.value.discord_code == 10062
    assert limited.value.code == "rate_limited" and unreachable.value.code == "unreachable"
    told = " ".join([str(late.value), str(limited.value), str(unreachable.value), caplog.text])
    assert INTERACTION_TOKEN not in told and ":token" in told


async def test_a_blocked_callback_response_does_not_delay_another_interaction():
    entered, release = asyncio.Event(), asyncio.Event()

    async def answer(request):
        if f"/interactions/{INTERACTION}/" in request.url.path:
            entered.set()
            await release.wait()
        return httpx.Response(204)

    client = DiscordClient(TOKEN, transport=httpx.MockTransport(answer))
    first = asyncio.create_task(client.respond(INTERACTION, INTERACTION_TOKEN, {"type": 6}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(client.respond(OTHER, "other-secret", {"type": 6}), 1)
        assert not first.done()
        assert not client._locks  # One-use callback IDs must not accumulate locks.
    finally:
        release.set()
        await first
        await client.close()


@pytest.mark.parametrize("operation", ["callback", "edit", "followup"])
async def test_interactions_bypass_the_bot_global_wait_but_keep_their_bucket_wait(rest, clock, operation):
    client = _client(rest, clock)
    path = {
        "callback": f"/interactions/{INTERACTION}/{INTERACTION_TOKEN}/callback",
        "edit": f"/webhooks/{APPLICATION}/{INTERACTION_TOKEN}/messages/@original",
        "followup": f"/webhooks/{APPLICATION}/{INTERACTION_TOKEN}",
    }[operation]
    method = "PATCH" if operation == "edit" else "POST"
    spent = {"X-RateLimit-Bucket": "interactions", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2"}
    rest.answer(method, path, (204, b"", spent), (204, b""))
    rest.answer("GET", f"/channels/{CHANNEL}", (429, {"retry_after": 10, "global": True}), (200, {}))
    await client.request("GET", f"/channels/{CHANNEL}")
    clock.now -= 10  # The bot's global limit is still active.

    await client.request(method, path)
    assert clock.slept == [10]
    await client.request(method, path)
    assert clock.slept == [10, 2]
    await client.request("GET", f"/channels/{OTHER}")
    assert clock.slept == [10, 2, 8]
    assert INTERACTION_TOKEN not in repr((client._locks, client._route_buckets, client._buckets))


async def test_callbacks_share_reported_buckets_and_retry_429s(rest, clock):
    spent = {"X-RateLimit-Bucket": "callbacks", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2"}
    rest.answer("POST", f"/interactions/{INTERACTION}/{INTERACTION_TOKEN}/callback", (204, b"", spent))
    rest.answer("POST", f"/interactions/{OTHER}/other-secret/callback", (429, {"retry_after": 0.5}), (204, b""))
    client = _client(rest, clock)

    await client.respond(INTERACTION, INTERACTION_TOKEN, {"type": 6})
    await client.respond(OTHER, "other-secret", {"type": 6})

    assert clock.slept == [2, 0.5]
    assert len(rest.requests) == 3
    assert not client._locks


async def test_an_interaction_429_does_not_set_the_bot_global_limit(rest, clock):
    rest.answer("POST", f"/interactions/{INTERACTION}/{INTERACTION_TOKEN}/callback",
                (429, {"retry_after": 2, "global": True}), (204, b""))
    client = _client(rest, clock)
    await client.respond(INTERACTION, INTERACTION_TOKEN, {"type": 6})
    clock.now -= 1
    await client.request("GET", f"/channels/{CHANNEL}")

    assert clock.slept == [2]


@pytest.mark.parametrize("method, suffix", [("PATCH", "/messages/@original"), ("POST", "")])
async def test_webhook_tokens_have_independent_buckets_and_keep_same_token_waits(rest, clock, method, suffix):
    first = f"/webhooks/{APPLICATION}/{INTERACTION_TOKEN}{suffix}"
    second = f"/webhooks/{APPLICATION}/other-secret{suffix}"
    spent = {"X-RateLimit-Bucket": "webhooks", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "60"}
    rest.answer(method, first, (200, {}, spent), (429, {"retry_after": 0.5}), (200, {}))
    rest.answer(method, second, (200, {}, {"X-RateLimit-Bucket": "webhooks"}), (200, {}))
    client = _client(rest, clock)
    try:
        await client.request(method, first)
        await client.request(method, second)
        await client.request(method, second)
        assert clock.slept == []

        await client.request(method, first)
        assert clock.slept == [60, 0.5]
        stored = repr((client._locks, client._route_buckets, client._buckets))
        assert INTERACTION_TOKEN not in stored and "other-secret" not in stored
    finally:
        await client.close()


async def test_a_blocked_webhook_response_does_not_delay_another_token():
    entered, release = asyncio.Event(), asyncio.Event()

    async def answer(request):
        if INTERACTION_TOKEN in request.url.path:
            entered.set()
            await release.wait()
        return httpx.Response(204)

    client = DiscordClient(TOKEN, transport=httpx.MockTransport(answer))
    first = asyncio.create_task(client.edit_response(APPLICATION, INTERACTION_TOKEN, {"content": "first"}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(client.edit_response(APPLICATION, "other-secret", {"content": "second"}), 1)
        assert not first.done()
    finally:
        release.set()
        await first
        await client.close()


async def test_webhook_endpoints_share_a_reported_bucket_for_the_same_token(rest, clock):
    headers = {"X-RateLimit-Bucket": "webhooks", "X-RateLimit-Remaining": "1", "X-RateLimit-Reset-After": "2"}
    path = f"/webhooks/{APPLICATION}/{INTERACTION_TOKEN}"
    rest.answer("POST", path, (200, {}, headers), (200, {}))
    rest.answer("PATCH", f"{path}/messages/@original", (200, {}, {**headers, "X-RateLimit-Remaining": "0"}))
    client = _client(rest, clock)
    try:
        await client.followup(APPLICATION, INTERACTION_TOKEN, {"content": "first"})
        await client.edit_response(APPLICATION, INTERACTION_TOKEN, {"content": "edit"})
        await client.followup(APPLICATION, INTERACTION_TOKEN, {"content": "second"})
        assert clock.slept == [2]
    finally:
        await client.close()


async def test_expired_webhook_routes_and_idle_locks_do_not_accumulate(rest, clock):
    headers = {"X-RateLimit-Bucket": "webhooks", "X-RateLimit-Remaining": "1", "X-RateLimit-Reset-After": "1"}
    client = _client(rest, clock)
    try:
        for index in range(100):
            path = f"/webhooks/{APPLICATION}/token-{index}/messages/@original"
            rest.answer("PATCH", path, (204, b"", headers))
            await client.request("PATCH", path, json={})
        assert len(client._route_buckets) == len(client._buckets) == 100
        assert not client._locks

        clock.now += 61
        await client.request("GET", "/users/@me")

        assert not client._route_buckets and not client._buckets and not client._locks
    finally:
        await client.close()


async def test_cache_cleanup_retains_buckets_whose_reset_is_still_in_the_future(rest, clock):
    path = f"/webhooks/{APPLICATION}/{INTERACTION_TOKEN}/messages/@original"
    headers = {"X-RateLimit-Bucket": "webhooks", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "120"}
    rest.answer("PATCH", path, (204, b"", headers), (204, b""))
    client = _client(rest, clock)
    try:
        await client.request("PATCH", path, json={})
        clock.now += 61
        await client.request("GET", "/users/@me")
        await client.request("PATCH", path, json={})

        assert clock.slept == [59]
    finally:
        await client.close()


async def test_idle_lock_cleanup_preserves_the_lock_held_by_waiting_requests(clock):
    entered = [asyncio.Event() for _ in range(3)]
    release = [asyncio.Event() for _ in range(3)]
    calls = []

    async def answer(request):
        if request.method == "PATCH":
            index = len(calls)
            calls.append(index)
            entered[index].set()
            await release[index].wait()
        return httpx.Response(204)

    client = DiscordClient(TOKEN, transport=httpx.MockTransport(answer), clock=clock)
    tasks = []
    try:
        tasks.append(asyncio.create_task(client.edit_response(APPLICATION, INTERACTION_TOKEN, {})))
        await asyncio.wait_for(entered[0].wait(), 1)
        tasks.append(asyncio.create_task(client.edit_response(APPLICATION, INTERACTION_TOKEN, {})))
        await asyncio.sleep(0)
        clock.now += 61
        await client.request("GET", "/users/@me")
        tasks.append(asyncio.create_task(client.edit_response(APPLICATION, INTERACTION_TOKEN, {})))
        await asyncio.sleep(0)
        assert calls == [0]

        release[0].set()
        await asyncio.wait_for(entered[1].wait(), 1)
        assert calls == [0, 1]
        release[1].set()
        await asyncio.wait_for(entered[2].wait(), 1)
        release[2].set()
        await asyncio.gather(*tasks)
        assert not client._locks
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*tasks)
        await client.close()
