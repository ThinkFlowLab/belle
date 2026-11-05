"""The hook endpoint checks the token, caps the size, and fails a hook closed when its answer fails."""

import time

import httpx
import pytest

from belle_agent import hook_server
from belle_agent.hook_server import HookServer

TOKEN = "hook-token"


@pytest.fixture
async def endpoint():
    answered: list[tuple[str, dict]] = []

    async def lookup(value: str) -> str:
        return value.upper()

    def answer(hook: str, event, headers):
        answered.append((hook, dict(event)))
        endpoint.headers = headers
        if hook == "broken":
            raise RuntimeError("no answer")
        # The answer may run a coroutine on the loop that started the endpoint.
        return {"seen": endpoint.run(lookup(event.get("value", "")))}

    endpoint = HookServer(answer, token=TOKEN)
    await endpoint.start()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{endpoint.port}") as client:
        endpoint.client, endpoint.answered = client, answered
        yield endpoint
    await endpoint.close()


def _post(endpoint, path: str, body, token: str = TOKEN) -> httpx.Response:
    return endpoint.client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


async def test_a_hook_is_answered_from_the_answer_function(endpoint):
    response = await _post(endpoint, "/hook/user_prompt_submit", {"value": "abc"})

    assert response.status_code == 200 and response.json() == {"seen": "ABC"}
    assert endpoint.answered == [("user_prompt_submit", {"value": "abc"})]


async def test_a_request_without_the_token_is_refused_and_answers_nothing(endpoint):
    for token in ("guess", TOKEN[:-1]):
        assert (await _post(endpoint, "/hook/any", {}, token=token)).status_code == 401
    assert (await endpoint.client.post("/hook/any", json={})).status_code == 401
    assert endpoint.answered == []


async def test_a_request_too_large_or_not_a_hook_is_refused(endpoint, monkeypatch):
    monkeypatch.setattr(hook_server, "MAX_REQUEST_BYTES", 16)

    headers = {"Authorization": f"Bearer {TOKEN}"}
    large = await endpoint.client.post("/hook/any", content=b"x" * 32, headers=headers)
    assert large.status_code == 413
    assert large.json() == {"error": "the event is 32 bytes; a hook event may hold at most 16"}
    assert (await _post(endpoint, "/elsewhere", {})).status_code == 404
    assert (await endpoint.client.post("/hook/any", content=b"{nope", headers=headers)).status_code == 400
    form = {**headers, "Content-Type": hook_server.FORM}
    assert (await endpoint.client.post("/hook/any", content=b"a=1&&=", headers=form)).status_code == 400
    assert endpoint.answered == []


async def test_the_endpoint_keeps_its_last_refusal(endpoint, monkeypatch):
    assert endpoint.refused == {}
    monkeypatch.setattr(hook_server, "MAX_REQUEST_BYTES", 16)

    endpoint.watch_session("s1")
    endpoint.watch_session("s2")
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Belle-Session": "s1"}
    await endpoint.client.post("/hook/context", content=b"x" * 32, headers=headers)
    assert endpoint.refused["s1"][1] == "HTTP 413 for /hook/context: the event is 32 bytes; a hook event may hold at most 16"
    await endpoint.client.post("/hook/any", content=b"{nope", headers={**headers, "X-Belle-Session": "s2"})
    assert endpoint.refused["s2"][1].startswith("HTTP 400 for /hook/any:")
    await endpoint.client.post("/hook/any", json={}, headers={**headers, "Authorization": "Bearer guess"})
    assert len(endpoint.refused) == 2
    await endpoint.client.post("/hook/any", content=b"{nope", headers={**headers, "X-Belle-Session": "unknown"})
    assert len(endpoint.refused) == 2
    # An answered hook leaves the last refusal as it was.
    await endpoint.client.post("/hook/any", json={}, headers=headers)
    assert endpoint.refused["s1"][1].startswith("HTTP 413")


async def test_the_last_refusal_of_a_caller_with_the_token_is_kept_with_or_without_a_session(endpoint, monkeypatch):
    monkeypatch.setattr(hook_server, "MAX_REQUEST_BYTES", 16)
    began = time.monotonic()
    assert endpoint.refusal_since(began) == ""

    headers = {"Authorization": f"Bearer {TOKEN}"}
    await endpoint.client.post("/hook/docker_agent", content=b"x" * 32, headers=headers)
    assert endpoint.refusal_since(began) == (
        "HTTP 413 for /hook/docker_agent: the event is 32 bytes; a hook event may hold at most 16"
    )
    # A caller without the token is refused and leaves nothing to read, its path included.
    await endpoint.client.post("/hook/its-own-words", json={}, headers={"Authorization": "Bearer guess"})
    assert endpoint.refusal_since(began).startswith("HTTP 413 for /hook/docker_agent")
    # A refusal before the time asked about is none.
    assert endpoint.refusal_since(time.monotonic()) == ""


async def test_a_form_is_answered_with_its_fields_as_the_event(endpoint):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = await endpoint.client.post("/hook/command", data={"value": "abc", "empty": ""}, headers=headers)

    assert response.status_code == 200 and response.json() == {"seen": "ABC"}
    assert endpoint.answered == [("command", {"value": "abc", "empty": ""})]


async def test_a_hook_is_answered_with_the_request_headers_by_lower_case_name(endpoint):
    await endpoint.client.post("/hook/any", json={}, headers={"Authorization": f"Bearer {TOKEN}", "X-Turn": "t1"})

    assert endpoint.headers["x-turn"] == "t1"
    assert endpoint.headers["authorization"] == f"Bearer {TOKEN}"


async def test_a_hook_is_named_by_the_path_after_the_prefix():
    answered = []
    endpoint = HookServer(lambda hook, event, headers: answered.append(hook) or {}, prefix="/")
    await endpoint.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{endpoint.port}") as client:
            assert (await client.post("/action", json={"a": 1})).status_code == 200
    finally:
        await endpoint.close()
    assert answered == ["action"]


async def test_a_hook_whose_answer_fails_fails_closed(endpoint):
    response = await _post(endpoint, "/hook/broken", {})

    assert response.status_code == 500 and response.json() == {"error": "hook endpoint failed"}
