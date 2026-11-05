"""Shared Slack API failures for channel tests."""

from types import SimpleNamespace
from typing import Any


class FakeSlackError(Exception):
    status_code = 200

    def __init__(self, error: str, detail: str = "") -> None:
        super().__init__("sanitized fake failure")
        data = {"ok": False, "error": error}
        if detail:
            data["detail"] = detail
        self.response = SimpleNamespace(
            status_code=self.status_code,
            data=data,
            headers={},
        )


class FakeSlackServerError(FakeSlackError):
    status_code = 500


class RecordingSlackClient:
    def __init__(self, fail: Any = None) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def record_call(self, method: str, payload: dict[str, Any]) -> None:
        self.calls.append((method, payload))
        if callable(self.fail):
            refusal = self.fail(method, payload)
        elif isinstance(self.fail, dict):
            refusal = self.fail.get(method)
        else:
            refusal = self.fail
        if refusal:
            if isinstance(refusal, tuple):
                raise FakeSlackError(*refusal)
            raise FakeSlackError(str(refusal))

    def payload(self, method: str) -> dict[str, Any]:
        for name, payload in self.calls:
            if name == method:
                return payload
        raise AssertionError(f"{method} was never called: {self.calls}")

    def payloads(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]


class SlackPeople(RecordingSlackClient):
    """A Slack whose ``users.info`` names the users in ``bots`` as bots, and anybody else as a person."""

    def __init__(self, bots: Any = (), fail: Any = None) -> None:
        super().__init__(fail)
        self.bots = set(bots)

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("users_info", kwargs)
        user = kwargs.get("user", "")
        return {"ok": True, "user": {"id": user, "is_bot": user in self.bots}}
