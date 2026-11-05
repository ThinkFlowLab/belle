"""The REST client: the bot's token in the header, and refusals with a code and no token."""

import logging

import pytest
from transport import TOKEN, URL, EventFeeder, FakeRest

from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError


def _client(rest: FakeRest) -> MattermostClient:
    return MattermostClient(URL, TOKEN, transport=rest.transport())


async def test_a_request_carries_the_token_and_returns_the_body():
    rest = FakeRest()
    client = _client(rest)

    me = await client.me()

    assert me["username"] == "belle"
    assert [(request.method, request.path) for request in rest.requests] == [("GET", "/users/me")]
    assert rest.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    await client.close()


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "bad_request"), (401, "token_rejected"), (403, "forbidden"), (404, "not_found"), (413, "too_large"),
     (429, "rate_limited"), (500, "server_error"), (503, "server_error"), (409, "http_409")],
)
async def test_a_refusal_has_a_code_by_status_and_holds_no_token(status, code, caplog):
    rest = FakeRest(routes={("POST", "/posts"): (status, {"id": "api.post.synthetic.app_error", "message": "no"})})
    client = _client(rest)

    with caplog.at_level(logging.DEBUG), pytest.raises(MattermostError) as refused:
        await client.request("POST", "/posts", json={"channel_id": "c" * 26, "message": "hi"})

    error = refused.value
    assert (error.code, error.status, error.server_id) == (code, status, "api.post.synthetic.app_error")
    assert str(error) == f"{code}: POST /posts -> {status} (api.post.synthetic.app_error)"
    assert TOKEN not in str(error) + repr(error) + caplog.text
    await client.close()


async def test_an_unreachable_server_is_a_code_and_holds_no_token(caplog):
    client = _client(FakeRest(unreachable=True))

    with caplog.at_level(logging.DEBUG), pytest.raises(MattermostError) as refused:
        await client.me()

    assert refused.value.code == "unreachable"
    assert str(refused.value) == "unreachable: GET /users/me -> no answer"
    assert TOKEN not in caplog.text
    await client.close()


@pytest.mark.parametrize(("url", "socket"), [(URL, "ws://mattermost.invalid"), ("https://chat.example.com/team", "wss://chat.example.com/team")])
async def test_the_websocket_opens_at_the_api_path_of_the_server(url, socket):
    feeder = EventFeeder()
    client = MattermostClient(url, TOKEN, transport=FakeRest().transport(), open_events=feeder.open)

    await client.events()

    assert feeder.opened == [(f"{socket}/api/v4/websocket", TOKEN)]
    await client.close()
