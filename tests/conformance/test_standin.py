import socket
from email.message import Message

import httpx
import pytest
import standin


def test_each_directive_produces_its_reply_shape():
    standin.selftest()


def test_the_request_log_drops_the_authorization_header(tmp_path):
    headers = Message()  # case-insensitive, as the HTTP server's headers are
    headers["Authorization"] = "Bearer secret-value"
    headers["X-Opencode-Session-Id"] = "ses_1"
    model = standin.StandIn(port=0, log_path=str(tmp_path / "log.jsonl"), host="127.0.0.1")
    try:
        model.record("/v1/chat/completions", headers, {})
    finally:
        model.close()
    assert model.log[0]["session"] == "ses_1"
    assert "secret-value" not in (tmp_path / "log.jsonl").read_text()
    assert "authorization" not in model.log[0]["headers"]


@pytest.fixture
def served():
    model = standin.StandIn(port=0, host="127.0.0.1")
    scripted: list[dict] = []
    answer = model.script

    def kept(body, session):
        scripted.append(body)
        return answer(body, session)

    model.script = kept
    yield model, scripted
    model.close()


def _completions(model) -> str:
    return f"http://127.0.0.1:{model.server.server_address[1]}/v1/chat/completions"


def test_a_body_that_is_no_json_object_asks_the_model_nothing(served):
    model, scripted = served
    for content in (b"{cut", b"[]", b""):
        response = httpx.post(_completions(model), content=content)
        assert response.status_code == 400, content
    assert scripted == [] and model.log == []
    # Control: a request the stand-in can read is scripted and kept.
    assert httpx.post(_completions(model), json={"messages": [{"role": "user", "content": "SAY=read"}]}).status_code == 200
    assert len(scripted) == len(model.log) == 1


def test_a_body_cut_short_by_its_client_asks_the_model_nothing(served):
    model, scripted = served
    body = b'{"messages": [{"role": "user", "content": "SAY=never"}]}'
    with socket.create_connection(model.server.server_address) as connection:
        head = f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body) + 100}\r\n\r\n"
        connection.sendall(head.encode() + body)
        # The client gives up before the rest of what it announced, as a cancelled request does.
        connection.shutdown(socket.SHUT_WR)
        answer = connection.recv(1024)
    assert answer.startswith(b"HTTP/1.1 400")
    assert scripted == [] and model.log == []
