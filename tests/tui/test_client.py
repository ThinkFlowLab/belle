import json
from pathlib import Path

import pytest

from belle_agent.control import client


@pytest.mark.parametrize(("method", "api_method", "identity", "state"), [
    ("stop_work", "work.stop", "work_id", "running"),
    ("drop_work", "work.drop", "work_id", "queued"),
    ("delete_schedule", "schedule.delete", "job_id", "paused"),
])
async def test_control_action_client_contract(monkeypatch, method, api_method, identity, state):
    captured = {}

    async def request(socket, request, request_id, timeout):
        captured.update(json.loads(request), socket=socket, request_id=request_id, timeout=timeout)
        return {"instance_id": "instance"}

    monkeypatch.setattr(client, "_request", request)
    result = await getattr(client, method)(Path("/control.sock"), "item-123", state, "instance")

    assert captured["method"] == api_method
    assert captured["params"] == {identity: "item-123", "expected_state": state, "instance_id": "instance"}
    assert captured["socket"] == Path("/control.sock")
    assert captured["request_id"] == captured["id"]
    assert captured["timeout"] == 2.0
    assert result == {"instance_id": "instance"}


async def test_peek_client_contract(monkeypatch):
    captured = {}

    async def request_result(socket, request, request_id, timeout):
        captured.update(json.loads(request), socket=socket, request_id=request_id, timeout=timeout)
        if captured["method"] == "peek.open":
            return {"peek_id": "peek-1", "work_id": "work-1", "live": True, "frames": [], "lost": 0}
        if captured["method"] == "peek.poll":
            return {"peek_id": "peek-1", "work_id": "work-1", "live": True, "frames": [], "lost": 0}
        return {"closed": True}

    monkeypatch.setattr(client, "_request_result", request_result)
    opened = await client.peek_open(Path("/control.sock"), "work-1", "instance")
    assert captured["method"] == "peek.open"
    assert captured["params"] == {"work_id": "work-1", "instance_id": "instance"}
    assert opened["peek_id"] == "peek-1"
    polled = await client.peek_poll(Path("/control.sock"), "peek-1", 0)
    assert captured["method"] == "peek.poll"
    assert captured["params"] == {"peek_id": "peek-1", "after_seq": 0}
    assert polled["live"] is True
    await client.peek_close(Path("/control.sock"), "peek-1")
    assert captured["method"] == "peek.close"
    assert captured["params"] == {"peek_id": "peek-1"}
