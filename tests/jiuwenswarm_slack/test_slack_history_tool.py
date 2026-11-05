"""Unit tests for the request-scoped Slack history toolkit."""

import asyncio
import json
from collections import defaultdict
from contextvars import ContextVar
from typing import Any

import pytest
from belle_agent.platforms.slack import history as belle_history

import jiuwenswarm.common.config as config_module
import belle_agent.platforms.slack.rich_text_walk as slack_rich_text
from belle_agent.integrations.jiuwenswarm import slack_history
from belle_agent.integrations.jiuwenswarm.slack_history import SlackHistoryToolkit
from slack_card_rules import CANONICAL_CARD_SENTENCES


class _FakeClient:
    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self.responses = {name: list(items) for name, items in responses.items()}
        self.calls: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def _respond(self, method: str, kwargs: dict[str, Any]) -> Any:
        self.calls[method].append(kwargs)
        item = self.responses[method].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def auth_test(self, **kwargs: Any) -> Any:
        return await self._respond("auth_test", kwargs)

    async def conversations_history(self, **kwargs: Any) -> Any:
        return await self._respond("conversations_history", kwargs)

    async def conversations_replies(self, **kwargs: Any) -> Any:
        return await self._respond("conversations_replies", kwargs)

    async def conversations_info(self, **kwargs: Any) -> Any:
        # Declined by a test that scripts no answer, which is what a workspace
        # without the scope does. A read must survive that with its messages.
        return await self._respond("conversations_info", kwargs)

    async def users_info(self, **kwargs: Any) -> Any:
        return await self._respond("users_info", kwargs)


class _ConcurrentClient:
    def __init__(self) -> None:
        self.history_channels: list[str] = []

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"user_id": "U-BOT"}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        channel = str(kwargs["channel"])
        self.history_channels.append(channel)
        await asyncio.sleep(0)
        return {
            "messages": [{"ts": "199999.0", "user": "U1", "text": f"from {channel}"}]
        }


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int,
        data: dict[str, Any],
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.data = data
        self.headers = headers or {}


class _FakeSlackError(Exception):
    def __init__(self, response: _FakeResponse) -> None:
        super().__init__("sanitized fake failure")
        self.response = response


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _toolkit(client: _FakeClient, **kwargs: Any) -> SlackHistoryToolkit:
    return SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C-RESEARCH",
            "slack_channel_type": "channel",
            "slack_enforce_audience": True,
        },
        client=client,
        now=lambda: 200_000.0,
        max_user_lookups=0,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_requires_a_trusted_request_channel_and_declares_a_target() -> None:
    client = _FakeClient({})
    toolkit = SlackHistoryToolkit(metadata={"slack_channel_id": "C1"}, client=client)

    result = json.loads(await toolkit.read_slack_conversation())

    assert result == {
        "ok": False,
        "error": "trusted_slack_channel_context_required",
        "messages": [],
    }
    assert not client.calls
    card = toolkit.get_tools()[0]._card
    assert card.name == "read_slack_conversation"
    assert "chat_id" in card.input_params["properties"]


def test_the_shared_rules_are_worded_the_way_the_other_card_words_them() -> None:
    toolkit = SlackHistoryToolkit(
        metadata={"slack_channel_id": "C1"}, client=_FakeClient({})
    )
    description = toolkit.get_tools()[0]._card.description
    for sentence in CANONICAL_CARD_SENTENCES:
        assert sentence in description, sentence


def test_the_card_states_its_own_paging_without_naming_the_other_tool() -> None:
    toolkit = SlackHistoryToolkit(
        metadata={"slack_channel_id": "C1"}, client=_FakeClient({})
    )
    description = toolkit.get_tools()[0]._card.description
    assert "Paging walks backwards in time" in description
    assert "coverage.next_before_ts as before_ts" in description
    assert "To go forward instead" in description
    assert "never newer" not in description
    for sibling_claim in ("The two Slack tools", "search_slack_workspace"):
        assert sibling_claim not in description, sibling_claim


def test_the_card_describes_the_tool_and_leaves_the_task_to_the_skill() -> None:
    toolkit = SlackHistoryToolkit(
        metadata={"slack_channel_id": "C1"}, client=_FakeClient({})
    )
    description = toolkit.get_tools()[0]._card.description

    for policy in (
        "For a channel digest",
        "write the digest yourself",
        "in Markdown",
        "Link a claim to its evidence",
    ):
        assert policy not in description

    # And the capability half is still said, because nothing else says it.
    assert "Copy a permalink verbatim from this result" in description
    assert "building a Slack link from parts" in description


@pytest.mark.asyncio
async def test_rejects_multipart_direct_message_context() -> None:
    client = _FakeClient({})
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "G1",
            "slack_channel_type": "mpim",
            "slack_enforce_audience": True,
        },
        client=client,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    assert result["ok"] is False
    assert result["error"] == "trusted_slack_channel_context_required"
    assert not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [float("nan"), float("inf"), float("-inf")])
async def test_rejects_non_finite_hours(hours: float) -> None:
    client = _FakeClient({})

    result = json.loads(
        await _toolkit(client).read_slack_conversation(hours=hours)
    )

    assert result == {
        "ok": False,
        "error": "hours_must_be_finite",
        "messages": [],
    }
    assert not client.calls


@pytest.mark.asyncio
async def test_client_receives_token_only_from_resolved_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [{"messages": []}],
        }
    )
    captured: dict[str, str] = {}

    def client_factory(*, token: str) -> _FakeClient:
        captured["token"] = token
        return client

    monkeypatch.setattr(belle_history, "AsyncWebClient", client_factory)
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_enforce_audience": True,
        },
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    assert result["ok"] is True
    assert captured == {"token": "xoxb-config-secret"}
    assert "xoxb-config-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_undocumented_history_limit_config_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "channels": {
                "slack": {
                    "bot_token": "xoxb-config-secret",
                    "history_digest_max_messages": 1,
                    "history_digest_max_api_calls": 1,
                }
            }
        },
    )
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {"ts": "199999.0", "user": "U1", "text": "first"},
                        {"ts": "199998.0", "user": "U2", "text": "second"},
                    ]
                }
            ],
        }
    )

    result = json.loads(await _toolkit(client).read_slack_conversation())

    assert result["ok"] is True
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["messages_returned"] == 2
    # auth.test, the history page, and the conversations.info the snapshot
    # takes its chat_name from.
    assert result["coverage"]["api_calls"] == 3


@pytest.mark.asyncio
async def test_metadata_provider_isolates_concurrent_channel_requests() -> None:
    current_metadata: ContextVar[dict[str, Any] | None] = ContextVar(
        "slack_history_test_metadata", default=None
    )
    client = _ConcurrentClient()
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C-FALLBACK",
            "slack_channel_type": "channel",
            "slack_enforce_audience": True,
        },
        metadata_provider=current_metadata.get,
        client=client,
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )

    async def read_channel(channel_id: str) -> dict[str, Any]:
        token = current_metadata.set(
            {
                "slack_channel_id": channel_id,
                "slack_channel_type": "channel",
                "slack_enforce_audience": True,
            }
        )
        try:
            return json.loads(await toolkit.read_slack_conversation())
        finally:
            current_metadata.reset(token)

    first, second = await asyncio.gather(read_channel("C1"), read_channel("C2"))

    assert first["chat_id"] == "C1"
    assert first["messages"][0]["text"] == "from C1"
    assert second["chat_id"] == "C2"
    assert second["messages"][0]["text"] == "from C2"
    assert sorted(client.history_channels) == ["C1", "C2"]


@pytest.mark.asyncio
async def test_scan_deadline_returns_partial_and_keeps_messages_on_name_lookup() -> (
    None
):
    ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 91.0])
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "hi"}]}
            ],
            "users_info": [
                {
                    "user": {
                        "id": "U1",
                        "profile": {"display_name": "Alice"},
                    }
                }
            ],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_enforce_audience": True,
        },
        client=client,
        now=lambda: 200_000.0,
        monotonic=lambda: next(ticks, 91.0),
        scan_timeout_seconds=90,
        max_user_lookups=10,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    assert result["ok"] is True
    assert result["coverage"]["status"] == "partial"
    assert "scan_time_limit" in result["coverage"]["partial_reasons"]
    assert result["messages"][0]["author_user_id"] == "U1"
    assert result["messages"][0]["author_name"] == "U1"


@pytest.mark.asyncio
async def test_paginates_roots_and_includes_recent_reply_to_old_root() -> None:
    client = _FakeClient(
        {
            "auth_test": [
                {
                    "ok": True,
                    "user_id": "U-BOT",
                    "bot_id": "B-BOT",
                    "url": "https://example.slack.com/",
                }
            ],
            "conversations_history": [
                {
                    "ok": True,
                    "messages": [
                        {
                            "ts": "190000.000000",
                            "user": "U1",
                            "text": "old design discussion",
                            "reply_count": 1,
                            "latest_reply": "199000.000000",
                        },
                        {
                            "ts": "198500.000000",
                            "user": "U-BOT",
                            "bot_id": "B-BOT",
                            "text": "Received. Analyzing…",
                        },
                    ],
                    "response_metadata": {"next_cursor": "page-2"},
                },
                {
                    "ok": True,
                    "messages": [
                        {
                            "ts": "198000.000000",
                            "user": "U2",
                            "text": "new root",
                        }
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            ],
            "conversations_replies": [
                {
                    "ok": True,
                    "messages": [
                        {
                            "ts": "190000.000000",
                            "user": "U1",
                            "text": "old design discussion",
                        },
                        {
                            "ts": "195000.000000",
                            "thread_ts": "190000.000000",
                            "user": "U3",
                            "text": "old reply outside the window",
                        },
                        {
                            "ts": "199000.000000",
                            "thread_ts": "190000.000000",
                            "user": "U2",
                            "text": "decision made",
                        },
                    ],
                    "response_metadata": {"next_cursor": ""},
                }
            ],
        }
    )

    result = json.loads(
        await _toolkit(client).read_slack_conversation(hours=1)
    )

    assert result["ok"] is True
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["scope_note"].startswith(
        "Coverage describes Slack-accessible history"
    )
    assert result["window"]["cutoff_iso_utc"] == "1970-01-03T06:33:20Z"
    assert result["window"]["snapshot_iso_utc"] == "1970-01-03T07:33:20Z"
    assert result["coverage"]["history_pages"] == 2
    assert result["coverage"]["thread_pages"] == 1
    assert [message["ts"] for message in result["messages"]] == [
        "190000.000000",
        "198000.000000",
        "198500.000000",
        "199000.000000",
    ]
    assert result["messages"][0]["outside_window_context"] is True
    assert result["messages"][2]["is_own_bot_message"] is True
    assert result["messages"][3]["is_thread_reply"] is True
    assert result["messages"][3]["is_own_bot_message"] is False
    assert result["coverage"]["context_root_messages_returned"] == 1
    assert result["coverage"]["threads_returned"] == 1
    assert "thread_ts=190000.000000" in result["messages"][3]["permalink"]
    assert result["messages"][0]["source_mrkdwn"] == (
        f"<{result['messages'][0]['permalink']}|source>"
    )
    assert result["messages"][3]["source_mrkdwn"] == (
        "<https://example.slack.com/archives/C-RESEARCH/p199000000000"
        "?thread_ts=190000.000000&cid=C-RESEARCH|source>"
    )
    assert client.calls["conversations_history"][1]["cursor"] == "page-2"
    # Windowed thread scans intentionally omit `oldest` from root history so
    # older roots with recent replies remain discoverable.
    assert "oldest" not in client.calls["conversations_history"][0]
    # Slack can omit valid replies when conversations.replies receives a
    # non-zero `oldest`; replies are fetched without it and filtered locally.
    assert "oldest" not in client.calls["conversations_replies"][0]


@pytest.mark.asyncio
async def test_reports_partial_when_expected_thread_replies_are_not_returned() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {
                            "ts": "198000.0",
                            "user": "U1",
                            "text": "design proposal",
                            "reply_count": 4,
                            "latest_reply": "199500.0",
                        }
                    ]
                }
            ],
            # Some Slack responses contain only the root even though history
            # metadata declares replies. Coverage must not be called complete.
            "conversations_replies": [
                {
                    "messages": [
                        {
                            "ts": "198000.0",
                            "user": "U1",
                            "text": "design proposal",
                        }
                    ]
                }
            ],
        }
    )

    result = json.loads(await _toolkit(client).read_slack_conversation())

    assert result["coverage"]["status"] == "partial"
    assert "thread_replies_not_returned" in result["coverage"]["partial_reasons"]
    assert result["coverage"]["thread_replies_returned"] == 0
    assert result["coverage"]["threads_returned"] == 0


@pytest.mark.asyncio
async def test_preserves_bot_root_and_replies_and_deduplicates_pages() -> None:
    client = _FakeClient(
        {
            "auth_test": [
                {
                    "user_id": "U-BOT",
                    "bot_id": "B-BOT",
                    "url": "https://x.slack.com",
                }
            ],
            "conversations_history": [
                {
                    "messages": [
                        {
                            "ts": "198000.0",
                            "user": "U-BOT",
                            "bot_id": "B-BOT",
                            "text": "substantive bot analysis",
                            "reply_count": 3,
                            "latest_reply": "199500.0",
                        }
                    ]
                }
            ],
            "conversations_replies": [
                {
                    "messages": [
                        {
                            "ts": "198000.0",
                            "user": "U-BOT",
                            "bot_id": "B-BOT",
                            "text": "substantive bot analysis",
                        },
                        {
                            "ts": "199000.0",
                            "user": "U2",
                            "text": "useful",
                        },
                        {
                            "ts": "199100.0",
                            "user": "U-BOT",
                            "text": "generated output",
                        },
                    ],
                    "response_metadata": {"next_cursor": "more"},
                },
                {
                    "messages": [
                        {
                            "ts": "199000.0",
                            "user": "U2",
                            "text": "useful duplicate",
                        },
                        {"ts": "199500.0", "user": "U3", "text": "follow-up"},
                    ]
                },
            ],
        }
    )

    result = json.loads(await _toolkit(client).read_slack_conversation())

    assert [message["ts"] for message in result["messages"]] == [
        "198000.0",
        "199000.0",
        "199100.0",
        "199500.0",
    ]
    assert result["messages"][0]["is_own_bot_message"] is True
    assert result["messages"][1]["is_own_bot_message"] is False
    assert result["messages"][2]["is_own_bot_message"] is True
    assert result["messages"][3]["is_own_bot_message"] is False
    assert client.calls["conversations_replies"][1]["cursor"] == "more"


@pytest.mark.asyncio
async def test_retries_429_using_retry_after() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    rate_limited = _FakeSlackError(
        _FakeResponse(
            status_code=429,
            data={"ok": False, "error": "ratelimited"},
            headers={"Retry-After": "2"},
        )
    )
    client = _FakeClient(
        {
            "auth_test": [rate_limited, {"user_id": "U-BOT"}],
            "conversations_history": [{"messages": []}],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_enforce_audience": True,
        },
        client=client,
        sleep=fake_sleep,
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    assert result["ok"] is True
    assert sleeps == [2.0]
    assert len(client.calls["auth_test"]) == 2


@pytest.mark.asyncio
async def test_redacts_secrets_and_reports_partial_size_limit() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {
                            "ts": "199999.0",
                            "user": "U1",
                            "text": (
                                "token=xoxb-config-secret "
                                "Authorization: Bearer abcdefghijklmnop "
                                "OPENAI_API_KEY=top-secret "
                                "DATABASE_PASSWORD=hunter2 "
                                "OAUTH_CLIENT_SECRET=client-value "
                                "SIGNING_PRIVATE_KEY=private-value "
                                "ALT_SLACK_TOKEN=xoxc-alternate-secret"
                            ),
                        },
                        {"ts": "199998.0", "user": "U2", "text": "second"},
                    ]
                }
            ],
        }
    )

    result = json.loads(
        await _toolkit(client, max_messages=1).read_slack_conversation()
    )
    serialized = json.dumps(result)

    assert result["coverage"]["status"] == "partial"
    assert "message_limit" in result["coverage"]["partial_reasons"]
    assert result["coverage"]["redacted_count"] >= 7
    assert "xoxb-config-secret" not in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "top-secret" not in serialized
    assert "hunter2" not in serialized
    assert "client-value" not in serialized
    assert "private-value" not in serialized
    assert "xoxc-alternate-secret" not in serialized


@pytest.mark.asyncio
async def test_users_read_failure_falls_back_to_user_ids() -> None:
    missing_scope = _FakeSlackError(
        _FakeResponse(
            status_code=200,
            data={"ok": False, "error": "missing_scope"},
        )
    )
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "hi"}]}
            ],
            "users_info": [missing_scope],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_enforce_audience": True,
        },
        client=client,
        now=lambda: 200_000.0,
        max_user_lookups=10,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    assert result["messages"][0]["author_name"] == "U1"
    assert "users_read_scope_unavailable_using_ids" in result["coverage"]["warnings"]


@pytest.mark.asyncio
async def test_every_name_lookup_warning_is_keyed_on_the_field_it_is_about() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {"ts": "199999.0", "user": "U1", "text": "one"},
                        {"ts": "199998.0", "user": "U2", "text": "two"},
                    ]
                }
            ],
            "users_info": [{"user": {"name": "alice"}}],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_enforce_audience": True,
        },
        client=client,
        now=lambda: 200_000.0,
        # Two distinct authors, one lookup allowed: the cap is reported rather
        # than silently applied.
        max_user_lookups=1,
    )

    warnings = json.loads(await toolkit.read_slack_conversation())["coverage"]["warnings"]

    assert "author_name_lookup_limit" in warnings
    assert not [word for word in warnings if word.startswith("user_name")]


@pytest.mark.asyncio
async def test_a_name_lookup_stopped_by_the_call_budget_says_so_as_author_name() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "one"}]}
            ],
            "users_info": [],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_enforce_audience": True,
        },
        client=client,
        now=lambda: 200_000.0,
        # auth.test and conversations.history spend the budget, so the name
        # lookup never gets a call of its own.
        max_api_calls=2,
        max_user_lookups=10,
    )

    warnings = json.loads(await toolkit.read_slack_conversation())["coverage"]["warnings"]

    assert "author_names_not_resolved_api_limit" in warnings
    assert not [word for word in warnings if word.startswith("user_name")]


def _app_post(ts: str, display_name: str, text: str) -> dict[str, Any]:
    """Build a message shaped like the one Slack sends for an app-posted message.

    An app post holds no ``user``: the account fields are replaced by
    ``bot_id``, the name the app posted under in ``username``, and the
    installed app's own profile.
    """
    return {
        "ts": ts,
        "subtype": "bot_message",
        "bot_id": "B0BUILDBOT",
        "username": display_name,
        "bot_profile": {
            "id": "B0BUILDBOT",
            "name": display_name,
            "app_id": "A0BUILDBOT",
        },
        "text": text,
    }


@pytest.mark.asyncio
async def test_app_display_name_is_never_looked_up_as_a_user_id() -> None:
    user_not_found = _FakeSlackError(
        _FakeResponse(
            status_code=200,
            data={"ok": False, "error": "user_not_found"},
        )
    )
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT", "bot_id": "B-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        _app_post("199999.0", "Uptime Robot", "deploy finished"),
                        {"ts": "199998.0", "user": "U1", "text": "thanks"},
                    ]
                }
            ],
            "users_info": [
                {"user": {"id": "U1", "profile": {"display_name": "Alice"}}},
                user_not_found,
            ],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_enforce_audience": True,
        },
        client=client,
        now=lambda: 200_000.0,
        max_user_lookups=10,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    app_post = result["messages"][1]
    assert app_post["ts"] == "199999.0"
    assert app_post["author_user_id"] == ""
    assert app_post["author_name"] == "Uptime Robot"
    assert result["messages"][0]["author_name"] == "Alice"
    assert [call["user"] for call in client.calls["users_info"]] == ["U1"]
    assert "author_name_lookup_failed" not in result["coverage"]["warnings"]


@pytest.mark.asyncio
async def test_app_display_name_is_redacted_like_message_text() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT", "bot_id": "B-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        _app_post("199999.0", "deploy xoxb-leaked-secret", "done")
                    ]
                }
            ],
        }
    )

    result = json.loads(
        await _toolkit(client).read_slack_conversation(include_threads=False)
    )

    assert "xoxb-leaked-secret" not in json.dumps(result)
    assert result["messages"][0]["author_name"] == "deploy [REDACTED_SLACK_TOKEN]"
    assert result["coverage"]["redacted_count"] >= 1
    assert "sensitive_values_redacted" in result["coverage"]["warnings"]


@pytest.mark.asyncio
async def test_without_threads_uses_server_side_time_filter() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [{"messages": []}],
        }
    )

    result = json.loads(
        await _toolkit(client).read_slack_conversation(
            hours=2, include_threads=False
        )
    )

    assert result["coverage"]["status"] == "complete"
    assert client.calls["conversations_history"][0]["oldest"] == "192800.000000"
    assert not client.calls["conversations_replies"]


@pytest.mark.asyncio
async def test_messages_and_coverage_carry_human_readable_utc_dates() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {"ts": "1712777678.062809", "user": "U1", "text": "latest"},
                        {"ts": "1712345678.188269", "user": "U1", "text": "earliest"},
                    ]
                }
            ],
        }
    )

    result = json.loads(
        await SlackHistoryToolkit(
            metadata={
                "slack_channel_id": "D0DIRECT01",
                "slack_channel_type": "im",
                "slack_enforce_audience": True,
            },
            client=client,
            now=lambda: 1_712_777_800.0,
            max_user_lookups=0,
        ).read_slack_conversation(all_history=True, include_threads=False)
    )

    by_ts = {message["ts"]: message for message in result["messages"]}
    assert by_ts["1712345678.188269"]["ts_iso_utc"] == "2024-04-05T19:34:38.188269Z"
    assert by_ts["1712777678.062809"]["ts_iso_utc"] == "2024-04-10T19:34:38.062809Z"

    coverage = result["coverage"]
    # The raw identifiers stay untouched: source_ids and the renderer depend on them.
    assert coverage["earliest_message_ts"] == "1712345678.188269"
    assert coverage["earliest_message_iso_utc"].startswith("2024-04-05T19:34:38")
    assert coverage["latest_message_iso_utc"].startswith("2024-04-10T19:34:38")


def _slack_file(
    file_id: str,
    name: str,
    mimetype: str,
    size: int,
    **extra: Any,
) -> dict[str, Any]:
    """Build a file object shaped like the ones Slack embeds in a message.

    Slack repeats the name in the title and serves the bytes from url_private;
    both are reproduced here so the tests exercise what a real payload holds
    rather than a convenient subset of it.
    """
    return {
        "id": file_id,
        "name": name,
        "title": name,
        "mimetype": mimetype,
        "filetype": mimetype.split("/")[-1],
        "size": size,
        "url_private": f"https://files.slack.com/files-pri/T1-{file_id}/{name}",
        "url_private_download": (
            f"https://files.slack.com/files-pri/T1-{file_id}/download/{name}"
        ),
        "permalink": f"https://example.slack.com/files/U1/{file_id}/{name}",
        **extra,
    }


async def _history_with(
    messages: list[dict[str, Any]],
    read_kwargs: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """One canned page of history.

    ``kwargs`` build the toolkit; ``read_kwargs`` are the call's own arguments.
    """
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT", "url": "https://example.slack.com/"}],
            "conversations_history": [{"messages": messages}],
        }
    )
    return json.loads(
        await _toolkit(client, **kwargs).read_slack_conversation(
            include_threads=False, **(read_kwargs or {})
        )
    )


@pytest.mark.asyncio
async def test_message_with_one_file_carries_the_attachment() -> None:
    result = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "What is this file? Can you summarize it?",
                "files": [
                    _slack_file(
                        "F0FILE0001", "paper.pdf", "application/pdf", 2_215_244
                    )
                ],
            }
        ]
    )

    message = result["messages"][0]
    assert message["text"] == "What is this file? Can you summarize it?"
    assert message["files"] == [
        {
            "name": "paper.pdf",
            "id": "F0FILE0001",
            "mimetype": "application/pdf",
            "permalink": "https://example.slack.com/files/U1/F0FILE0001/paper.pdf",
            "size_bytes": 2_215_244,
        }
    ]
    # The title repeats the name, so it earns no field of its own.
    assert "title" not in message["files"][0]
    assert "files_truncated" not in message


@pytest.mark.asyncio
async def test_file_only_message_is_not_an_empty_event() -> None:
    result = await _history_with(
        [
            {
                "ts": "199998.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "",
                "files": [
                    _slack_file(
                        "F0FILE0002", "screenshot.jpg", "image/jpeg", 1_380_602
                    )
                ],
            }
        ]
    )

    assert result["coverage"]["messages_returned"] == 1
    message = result["messages"][0]
    assert message["text"] == ""
    assert [entry["name"] for entry in message["files"]] == ["screenshot.jpg"]
    assert message["files"][0]["mimetype"] == "image/jpeg"


@pytest.mark.asyncio
async def test_message_with_several_files_keeps_every_attachment() -> None:
    result = await _history_with(
        [
            {
                "ts": "199997.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "the batch",
                "files": [
                    _slack_file("F0FILE0004", "one.jpg", "image/jpeg", 1_147_660),
                    _slack_file("F0FILE0005", "two.jpg", "image/jpeg", 1_637_190),
                    _slack_file("F0FILE0003", "notes.txt", "text/plain", 2_629),
                ],
            }
        ]
    )

    files = result["messages"][0]["files"]
    assert [entry["name"] for entry in files] == ["one.jpg", "two.jpg", "notes.txt"]
    assert [entry["size_bytes"] for entry in files] == [1_147_660, 1_637_190, 2_629]


def _file_dump(count: int) -> list[dict[str, Any]]:
    return [
        {
            "ts": "199996.0",
            "user": "U1",
            "subtype": "file_share",
            "text": "",
            "files": [
                _slack_file(f"F{index:010d}", f"file-{index}.txt", "text/plain", 10)
                for index in range(count)
            ],
        }
    ]


@pytest.mark.asyncio
async def test_attachments_beyond_the_cap_are_reported_not_dropped_silently() -> None:
    default = slack_history._DEFAULT_MAX_FILES_PER_MESSAGE
    result = await _history_with(_file_dump(default + 3))

    message = result["messages"][0]
    assert len(message["files"]) == default
    assert message["files_truncated"] is True
    # The number the flag was measured against, so that the caller can raise it.
    assert result["coverage"]["max_files_per_message"] == default


@pytest.mark.asyncio
async def test_the_attachment_bound_is_the_callers_to_move() -> None:
    default = slack_history._DEFAULT_MAX_FILES_PER_MESSAGE
    raised = await _history_with(
        _file_dump(default + 3), read_kwargs={"max_files_per_message": default + 3}
    )

    message = raised["messages"][0]
    assert len(message["files"]) == default + 3
    assert "files_truncated" not in message
    assert raised["coverage"]["max_files_per_message"] == default + 3

    lowered = await _history_with(
        _file_dump(default + 3), read_kwargs={"max_files_per_message": 2}
    )

    assert len(lowered["messages"][0]["files"]) == 2
    assert lowered["messages"][0]["files_truncated"] is True
    assert lowered["coverage"]["max_files_per_message"] == 2


@pytest.mark.asyncio
async def test_message_without_files_is_unchanged() -> None:
    result = await _history_with(
        [{"ts": "199995.0", "user": "U1", "text": "just words"}]
    )

    message = result["messages"][0]
    assert message["text"] == "just words"
    assert "files" not in message
    assert "files_truncated" not in message


@pytest.mark.asyncio
async def test_attachment_never_exposes_the_authenticated_download_url() -> None:
    result = await _history_with(
        [
            {
                "ts": "199994.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "",
                "files": [_slack_file("F0FILE0003", "notes.txt", "text/plain", 2_629)],
            }
        ]
    )

    entry = result["messages"][0]["files"][0]
    assert entry["permalink"].startswith("https://example.slack.com/files/")
    assert "url_private" not in entry
    assert "files.slack.com" not in json.dumps(result)


@pytest.mark.asyncio
async def test_bot_uploaded_file_is_still_attributed_to_the_bot() -> None:
    result = await _history_with(
        [
            {
                "ts": "199993.0",
                "user": "U-BOT",
                "subtype": "file_share",
                "text": "",
                "files": [_slack_file("F0FILE0004", "report.csv", "text/csv", 4_096)],
            }
        ]
    )

    message = result["messages"][0]
    assert message["is_own_bot_message"] is True
    assert message["files"][0]["name"] == "report.csv"


@pytest.mark.asyncio
async def test_attachment_labels_are_redacted_and_fall_back_when_unnamed() -> None:
    result = await _history_with(
        [
            {
                "ts": "199992.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "",
                "files": [
                    _slack_file(
                        "F0FILE0005",
                        "xoxb-1234567890-abcdefghij.env",
                        "text/plain",
                        128,
                    ),
                    {"id": "F0FILE0002", "name": None, "title": "Untitled dump"},
                    "not a file object",
                ],
            }
        ]
    )

    files = result["messages"][0]["files"]
    assert files[0]["name"] == "[REDACTED_SLACK_TOKEN].env"
    assert "xoxb-" not in json.dumps(result)
    assert result["coverage"]["redacted_count"] >= 1
    # A missing name falls back to the title rather than leaving the entry blank.
    assert files[1]["name"] == "Untitled dump"
    assert "title" not in files[1]
    assert "size_bytes" not in files[1]
    # A malformed entry is skipped instead of failing the scan.
    assert len(files) == 2


@pytest.mark.asyncio
async def test_attachments_are_charged_to_the_total_character_budget() -> None:
    result = await _history_with(
        [
            {
                "ts": f"1999{index:02d}.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "",
                "files": [
                    _slack_file(
                        f"F{index:010d}",
                        f"attachment-{index}.pdf",
                        "application/pdf",
                        1,
                    )
                ],
            }
            for index in range(40, 90)
        ],
        max_total_chars=1_000,
    )

    coverage = result["coverage"]
    assert coverage["status"] == "partial"
    assert "total_character_limit" in coverage["partial_reasons"]
    # Text alone is empty everywhere, so only the attachments can have stopped it.
    assert all(message["text"] == "" for message in result["messages"])
    assert coverage["messages_returned"] < 50


@pytest.mark.asyncio
async def test_reactions_are_charged_to_the_total_character_budget() -> None:
    result = await _history_with(
        [
            {
                "ts": f"1999{index:02d}.0",
                "user": "U1",
                "text": "",
                "reactions": [
                    {
                        "name": f"custom-workspace-emoji-{index}-{slot}",
                        "users": ["U1", "U2"],
                        "count": 2,
                    }
                    for slot in range(20)
                ],
            }
            for index in range(40, 90)
        ],
        max_total_chars=1_000,
    )

    coverage = result["coverage"]
    assert coverage["status"] == "partial"
    assert "total_character_limit" in coverage["partial_reasons"]
    # Text alone is empty everywhere, so only the reactions can have stopped it.
    assert all(message["text"] == "" for message in result["messages"])
    assert coverage["messages_returned"] < 50
    # The records still hold the reactions they were charged for.
    assert len(result["messages"][0]["reactions"]) == 20


@pytest.mark.asyncio
async def test_empty_history_reports_null_iso_dates() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [{"messages": []}],
        }
    )

    result = json.loads(
        await _toolkit(client).read_slack_conversation(include_threads=False)
    )

    assert result["coverage"]["earliest_message_iso_utc"] is None
    assert result["coverage"]["latest_message_iso_utc"] is None


class _ChannelClient:
    """A Slack stand-in that honours ``latest``, ``inclusive`` and paging.

    The continuation tests assert that two calls tile the channel rather than
    nesting, which is only meaningful against a server that actually applies the
    bounds the tool sends. A fake that replays canned pages would pass whatever
    the tool did.
    """

    def __init__(
        self,
        roots: list[dict[str, Any]],
        replies: dict[str, list[dict[str, Any]]] | None = None,
        page_size: int = 200,
    ) -> None:
        self.roots = sorted(roots, key=lambda item: float(item["ts"]), reverse=True)
        self.replies = replies or {}
        self.page_size = page_size
        self.history_calls: list[dict[str, Any]] = []

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        return {"user_id": "U-BOT", "bot_id": "B-BOT", "url": "https://x.slack.com"}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self.history_calls.append(dict(kwargs))
        latest = float(kwargs["latest"])
        inclusive = bool(kwargs.get("inclusive"))
        oldest = float(kwargs["oldest"]) if kwargs.get("oldest") else None
        selected = [
            root
            for root in self.roots
            if (
                float(root["ts"]) <= latest if inclusive else float(root["ts"]) < latest
            )
            and (oldest is None or float(root["ts"]) >= oldest)
        ]
        start = int(kwargs.get("cursor") or 0)
        page = selected[start : start + self.page_size]
        next_start = start + self.page_size
        has_more = next_start < len(selected)
        response: dict[str, Any] = {"messages": page, "has_more": has_more}
        if has_more:
            response["response_metadata"] = {"next_cursor": str(next_start)}
        return response

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        root_ts = str(kwargs["ts"])
        thread = [{"ts": root_ts}] + list(self.replies.get(root_ts, []))
        return {"messages": thread, "has_more": False}


def _sample_channel() -> _ChannelClient:
    """Ten roots, one of which holds replies newer than every later root.

    That thread is the shape a message-timestamp cursor cannot express: its
    root is among the oldest in the channel while its replies are the newest
    messages in it, so a slice that stops in the middle of the channel by
    message timestamp would strand them.
    """
    roots = [
        {"ts": f"19990{index}.000000", "user": "U1", "text": f"root {index}"}
        for index in range(10)
    ]
    roots[1] = {
        "ts": "199901.000000",
        "user": "U1",
        "text": "root 1",
        "reply_count": 2,
        "latest_reply": "199951.000000",
    }
    replies = {
        "199901.000000": [
            {"ts": "199950.000000", "user": "U2", "text": "late reply a"},
            {"ts": "199951.000000", "user": "U2", "text": "late reply b"},
        ]
    }
    return _ChannelClient(roots, replies)


async def _slice(
    client: _ChannelClient, **kwargs: Any
) -> tuple[dict[str, Any], list[str]]:
    result = json.loads(
        await _toolkit(client).read_slack_conversation(
            all_history=True, **kwargs
        )
    )
    assert result["ok"] is True
    return result, [str(item["ts"]) for item in result["messages"]]


@pytest.mark.asyncio
async def test_call_without_arguments_keeps_todays_behaviour() -> None:
    client = _sample_channel()

    result, returned = await _slice(client)

    assert len(returned) == 12
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["max_messages"] == 2_000
    assert result["coverage"]["next_before_ts"] is None
    assert "resume_note" not in result["coverage"]
    assert result["window"]["before_ts"] is None


@pytest.mark.asyncio
async def test_max_messages_returns_fewer_messages_than_an_unbounded_call() -> None:
    unbounded_result, unbounded = await _slice(_sample_channel())
    bounded_result, bounded = await _slice(_sample_channel(), max_messages=4)

    assert unbounded_result["coverage"]["status"] == "complete"
    assert len(unbounded) == 12
    assert len(bounded) == 4
    assert bounded_result["coverage"]["status"] == "partial"
    assert "message_limit" in bounded_result["coverage"]["partial_reasons"]
    assert bounded_result["coverage"]["max_messages"] == 4
    assert set(bounded).issubset(set(unbounded))


@pytest.mark.asyncio
async def test_partial_result_names_an_exclusive_resume_position() -> None:
    result, returned = await _slice(_sample_channel(), max_messages=4)

    coverage = result["coverage"]
    oldest_root = min(str(item["thread_ts"]) for item in result["messages"])
    assert coverage["next_before_ts"] == oldest_root
    assert coverage["next_before_iso_utc"] is not None
    assert "exclusive" in coverage["resume_note"]
    assert coverage["next_before_ts"] in returned


@pytest.mark.asyncio
async def test_resumed_slices_tile_the_channel_without_overlap_or_gap() -> None:
    _, whole = await _slice(_sample_channel())

    walked: list[str] = []
    before_ts: str | None = None
    for _ in range(10):
        result, returned = await _slice(
            _sample_channel(), max_messages=4, before_ts=before_ts
        )
        walked.extend(returned)
        before_ts = result["coverage"]["next_before_ts"]
        if before_ts is None:
            break

    assert before_ts is None
    assert len(walked) == len(set(walked)), "a slice returned a message twice"
    assert sorted(walked) == sorted(whole), "the walk lost or invented a message"
    # The thread whose replies are the newest messages in the channel arrives
    # with its root, in the last slice, rather than with the newest slice.
    assert {"199950.000000", "199951.000000", "199901.000000"}.issubset(set(walked))


@pytest.mark.asyncio
async def test_resuming_from_the_oldest_message_ends_the_walk() -> None:
    result, returned = await _slice(_sample_channel(), before_ts="199900.000000")

    assert returned == []
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["next_before_ts"] is None
    assert result["window"]["before_ts"] == "199900.000000"


@pytest.mark.asyncio
async def test_a_thread_is_returned_whole_even_when_it_busts_the_bound() -> None:
    result, returned = await _slice(
        _sample_channel(), max_messages=2, before_ts="199902.000000"
    )

    # The thread is three messages against a bound of two. Splitting it would
    # strand the replies above any later resume position, so it comes back whole.
    assert sorted(returned) == ["199901.000000", "199950.000000", "199951.000000"]
    assert "thread_exceeded_requested_bounds" in result["coverage"]["warnings"]
    assert result["coverage"]["next_before_ts"] == "199901.000000"


@pytest.mark.asyncio
async def test_a_position_reaches_slack_with_the_characters_it_arrived_with(
) -> None:
    channel = _ChannelClient(
        [{"ts": "199905.000000", "user": "U1", "text": "one"}],
        page_size=200,
    )
    await _read(channel, before_ts="199902.000000", include_threads=False)

    assert channel.history_calls[0]["latest"] == "199902.000000"

    thread = _thread_client()
    await _read(thread, ts="199900.000000", before_ts="199903.000000")

    assert thread.calls["conversations_replies"][0]["latest"] == "199903.000000"


@pytest.mark.asyncio
async def test_an_older_edge_taken_from_a_result_reaches_slack_unchanged() -> None:
    client = _ChannelClient(
        [
            {"ts": "199905.000000", "user": "U1", "text": "new"},
            {"ts": "199900.000000", "user": "U1", "text": "old"},
        ],
        page_size=200,
    )
    result = await _read(client, after_ts="199901.000000", include_threads=False)

    assert client.history_calls[0]["oldest"] == "199901.000000"
    assert result["window"]["cutoff_ts"] == "199901.000000"


@pytest.mark.asyncio
async def test_every_position_the_result_states_is_one_it_accepts_back() -> None:
    result = await _read(_sample_channel(), hours=2)

    stated = [
        result["window"]["cutoff_ts"],
        result["window"]["snapshot_ts"],
        result["coverage"]["earliest_message_ts"],
        result["coverage"]["latest_message_ts"],
        result["coverage"]["next_before_ts"],
    ]
    assert any(value is not None for value in stated)
    for value in stated:
        if value is None:
            continue
        assert slack_history._SLACK_TS_SHAPE.match(value), value
        assert slack_history._slack_message_ts(value) is not None, value


@pytest.mark.asyncio
async def test_before_ts_that_is_not_a_slack_timestamp_is_refused() -> None:
    toolkit = _toolkit(_sample_channel())

    result = json.loads(
        await toolkit.read_slack_conversation(before_ts="yesterday")
    )

    assert result["ok"] is False
    assert result["error"] == "before_ts_must_be_a_slack_timestamp"
    assert result["messages"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argument", "code"),
    [
        ("before_ts", "before_ts_must_be_a_slack_timestamp"),
        ("after_ts", "after_ts_must_be_a_slack_timestamp"),
        ("ts", "ts_must_be_a_slack_timestamp"),
    ],
)
async def test_a_round_number_is_not_a_slack_timestamp(
    argument: str, code: str
) -> None:
    toolkit = _toolkit(_sample_channel())

    result = json.loads(
        await toolkit.read_slack_conversation(**{argument: "1788515000.0"})
    )

    assert result["ok"] is False
    assert result["error"] == code
    assert result["messages"] == []
    assert "six decimal places" in result["detail"]


@pytest.mark.asyncio
async def test_a_ts_in_the_shape_slack_mints_is_still_accepted() -> None:
    result = await _read(_sample_channel(), before_ts="199905.000000")

    assert result["ok"] is True
    assert [item["ts"] for item in result["messages"]][:1] == ["199900.000000"]


@pytest.mark.asyncio
async def test_coverage_hands_back_positions_the_tool_will_take_again() -> None:
    client = _ChannelClient(
        [
            {"ts": "199001.000100", "user": "U1", "text": "first"},
            {"ts": "199002.000000", "user": "U1", "text": "second"},
        ]
    )

    whole = await _read(client, all_history=True)
    coverage = whole["coverage"]

    assert coverage["earliest_message_ts"] == "199001.000100"
    assert coverage["latest_message_ts"] == "199002.000000"
    since = await _read(
        _ChannelClient(list(client.roots)),
        all_history=True,
        after_ts=str(coverage["latest_message_ts"]),
    )
    assert since["ok"] is True
    assert since["messages"] == []


@pytest.mark.asyncio
async def test_the_documented_walk_only_passes_back_what_it_was_given() -> None:
    walked: list[str] = []
    before_ts: str | None = None
    for _ in range(10):
        result = await _read(
            _sample_channel(), all_history=True, max_messages=4, before_ts=before_ts
        )
        assert result["ok"] is True
        walked.extend(str(item["ts"]) for item in result["messages"])
        before_ts = result["coverage"]["next_before_ts"]
        if before_ts is None:
            break

    assert before_ts is None
    assert len(walked) == 12
    assert len(walked) == len(set(walked))


@pytest.mark.asyncio
async def test_a_thread_slice_resumes_from_a_position_of_the_right_shape() -> None:
    client = _LongThreadClient(20)
    first = await _read(client, ts=_LongThreadClient.ROOT_TS, max_messages=2)
    resume = first["coverage"]["next_before_ts"]

    assert resume is not None
    second = await _read(
        _LongThreadClient(20), ts=_LongThreadClient.ROOT_TS, before_ts=resume
    )

    assert second["ok"] is True


@pytest.mark.asyncio
async def test_a_before_ts_older_than_the_default_window_is_not_called_complete(
) -> None:
    result = await _read(_sample_channel(), before_ts="100000.000000")

    assert result["messages"] == []
    assert result["coverage"]["status"] == "partial"
    assert "window_bounds_exclude_each_other" in result["coverage"]["partial_reasons"]
    assert (
        "default_window_applied_beside_before_ts" in result["coverage"]["warnings"]
    )
    # The note names the default, because the default is the half of the
    # contradiction the caller never wrote and cannot see in its own arguments.
    note = result["coverage"]["window_note"]
    assert "default 24 hours" in note
    assert "all_history" in note


@pytest.mark.asyncio
async def test_an_empty_conversation_reads_differently_from_a_narrowed_window(
) -> None:
    empty_channel = await _read(_ChannelClient([]))
    narrowed = await _read(_sample_channel(), before_ts="100000.000000")

    assert empty_channel["messages"] == []
    assert empty_channel["coverage"]["status"] == "complete"
    assert empty_channel["coverage"]["partial_reasons"] == []
    assert "window_note" not in empty_channel["coverage"]
    assert narrowed["coverage"]["status"] == "partial"
    assert "window_note" in narrowed["coverage"]


@pytest.mark.asyncio
async def test_a_before_ts_inside_the_default_window_reports_no_contradiction(
) -> None:
    result = await _read(_ChannelClient([]), before_ts="199900.000000")

    assert result["messages"] == []
    assert result["coverage"]["status"] == "complete"
    assert "window_bounds_exclude_each_other" not in (
        result["coverage"]["partial_reasons"]
    )
    assert "window_note" not in result["coverage"]


@pytest.mark.asyncio
async def test_the_documented_walk_still_tiles_a_default_window_read() -> None:
    whole = await _read(_sample_channel())

    walked: list[str] = []
    statuses: list[str] = []
    before_ts: str | None = None
    for _ in range(10):
        result = await _read(_sample_channel(), max_messages=4, before_ts=before_ts)
        walked.extend(str(item["ts"]) for item in result["messages"])
        statuses.append(result["coverage"]["status"])
        assert "window_bounds_exclude_each_other" not in (
            result["coverage"]["partial_reasons"]
        )
        before_ts = result["coverage"]["next_before_ts"]
        if before_ts is None:
            break

    assert before_ts is None
    assert len(walked) == len(set(walked)), "a slice returned a message twice"
    assert sorted(walked) == sorted(str(item["ts"]) for item in whole["messages"])
    assert statuses[-1] == "complete"


@pytest.mark.asyncio
async def test_a_stated_hours_beside_before_ts_is_honoured_and_not_reported(
) -> None:
    result = await _read(_sample_channel(), hours=48, before_ts="199905.000000")

    assert {item["ts"] for item in result["messages"]} == {
        "199900.000000",
        "199901.000000",
        "199902.000000",
        "199903.000000",
        "199904.000000",
        "199950.000000",
        "199951.000000",
    }
    assert result["window"]["mode"] == "hours"
    assert result["window"]["requested_hours"] == 48
    assert result["coverage"]["warnings"] == []
    assert result["coverage"]["status"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "older_edge",
    [
        {"after_ts": "199905.000000"},
        {"after_iso_utc": "1970-01-03T07:31:45Z"},
    ],
    ids=["after_ts", "after_iso_utc"],
)
async def test_a_stated_older_edge_above_before_ts_is_reported_the_same_way(
    older_edge: dict[str, str],
) -> None:
    result = await _read(
        _sample_channel(), before_ts="199901.000000", **older_edge
    )

    assert result["messages"] == []
    assert result["coverage"]["status"] == "partial"
    assert "window_bounds_exclude_each_other" in result["coverage"]["partial_reasons"]
    # No default applied, so nothing is named as one; the note says to move a
    # bound rather than to state a window that is already stated.
    assert result["coverage"]["warnings"] == []
    assert "default 24 hours" not in result["coverage"]["window_note"]
    assert "Move whichever" in result["coverage"]["window_note"]


@pytest.mark.asyncio
async def test_a_thread_read_carries_no_window_for_before_ts_to_contradict(
) -> None:
    result = await _read(
        _old_thread(), ts="100000.000000", before_ts="100001.000000"
    )

    assert [item["ts"] for item in result["messages"]] == ["100000.000000"]
    assert result["window"]["mode"] == "thread"
    assert "window_bounds_exclude_each_other" not in (
        result["coverage"]["partial_reasons"]
    )


@pytest.mark.asyncio
async def test_bounds_that_exclude_each_other_are_not_reported_when_something_came_back(
) -> None:
    result = await _read(
        _sample_channel(), before_ts="199902.000000", after_ts="199905.000000"
    )

    assert {item["ts"] for item in result["messages"]} == {
        "199901.000000",
        "199950.000000",
        "199951.000000",
    }
    assert "window_bounds_exclude_each_other" not in (
        result["coverage"]["partial_reasons"]
    )
    assert "window_note" not in result["coverage"]


@pytest.mark.asyncio
async def test_the_card_says_what_an_excluded_window_means() -> None:
    card = _toolkit(_sample_channel()).get_tools()[0]._card

    assert "window_bounds_exclude_each_other" in card.description
    assert "coverage.window_note" in card.description
    before_ts = card.input_params["properties"]["before_ts"]
    assert "narrows the window and does not replace it" in before_ts["description"]


@pytest.mark.asyncio
async def test_the_card_names_the_bound_behind_each_truncation_mark() -> None:
    card = _toolkit(_sample_channel()).get_tools()[0]._card
    properties = card.input_params["properties"]

    assert "files_truncated" in properties["max_files_per_message"]["description"]
    assert (
        "reactors_truncated"
        in properties["max_reactors_per_reaction"]["description"]
    )
    assert "coverage.max_files_per_message" in card.description
    assert "coverage.max_reactors_per_reaction" in card.description
    # The cap that was removed is claimed nowhere.
    assert "reactions_truncated" not in card.description


@pytest.mark.asyncio
async def test_configured_bound_caps_a_call_that_asks_for_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        slack_history,
        "load_belle_config",
        lambda: {"platforms": {"slack": {"history_max_messages": 3}}},
    )
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "channels": {
                "slack": {
                    "bot_token": "xoxb-config-secret",
                }
            }
        },
    )

    result, returned = await _slice(_sample_channel())

    assert len(returned) == 3
    assert result["coverage"]["max_messages"] == 3
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["next_before_ts"] is not None


@pytest.mark.asyncio
async def test_a_caller_cannot_ask_for_more_than_the_deployment_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        slack_history,
        "load_belle_config",
        lambda: {"platforms": {"slack": {"history_max_messages": 3}}},
    )
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "channels": {
                "slack": {
                    "bot_token": "xoxb-config-secret",
                }
            }
        },
    )

    result, returned = await _slice(_sample_channel(), max_messages=500)

    assert len(returned) == 3
    assert result["coverage"]["max_messages"] == 3


@pytest.mark.asyncio
async def test_operator_can_raise_history_budgets_above_old_code_ceilings() -> None:
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C-RESEARCH",
            "slack_channel_type": "channel",
            "slack_enforce_audience": True,
        },
        client=_sample_channel(),
        max_messages=6_000,
        max_roots_scanned=60_000,
        max_api_calls=3_000,
        max_message_chars=15_000,
        max_total_chars=600_000,
        max_user_lookups=300,
        scan_timeout_seconds=400,
    )

    await toolkit._load_settings()

    assert toolkit._max_messages == 6_000
    assert toolkit._max_roots_scanned == 60_000
    assert toolkit._max_api_calls == 3_000
    assert toolkit._max_message_chars == 15_000
    assert toolkit._max_total_chars == 600_000
    assert toolkit._max_user_lookups == 300
    assert toolkit._scan_timeout_seconds == 400


def test_tool_config_uses_belle_bounds_and_host_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "channels": {"slack": {"bot_token": "host-token", "history_max_messages": 1}}
        },
    )
    monkeypatch.setattr(
        slack_history,
        "load_belle_config",
        lambda: {"platforms": {"slack": {"history_max_messages": 3}}},
    )
    assert slack_history._slack_tool_config() == {
        "bot_token": "host-token", "history_max_messages": 3
    }


def test_belle_tool_config_prefers_its_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "host-token"}}},
    )
    monkeypatch.setattr(
        slack_history,
        "load_belle_config",
        lambda: {"platforms": {"slack": {"bot_token": "other-token"}}},
    )
    assert slack_history._slack_tool_config()["bot_token"] == "other-token"


# ---------------------------------------------------------------------------
# The redaction pass, which two tools run and only one defines.
#
# ``slack_search`` already imported the regexes by name because they are
# security-critical and a fix to one copy would not reach a second. The pass
# driving them was copied rather than imported, which left exactly the gap the
# import was reasoned about to close. These tests are on the shared function and
# on the one property the two callers do not share.
# ---------------------------------------------------------------------------


def test_both_slack_tools_run_the_same_redaction_pass() -> None:
    from belle_agent.integrations.jiuwenswarm import slack_search

    assert slack_search.redact_credentials is slack_history.redact_credentials


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("run xoxb-11-22-abcdef with it", "run [REDACTED_SLACK_TOKEN] with it"),
        ("Bearer abcdefghijkl", "Bearer [REDACTED]"),
        ("api_key=hunter2hunter2", "api_key=[REDACTED]"),
    ],
)
def test_every_credential_shape_is_masked(value: str, expected: str) -> None:
    text, redacted, truncated = slack_history.redact_credentials(value)

    assert text == expected
    assert redacted == 1
    assert truncated is False


def test_the_known_token_is_masked_by_literal_match_before_the_patterns() -> None:
    text, redacted, _ = slack_history.redact_credentials(
        "token is s3cr3t-not-a-slack-shape", bot_token="s3cr3t-not-a-slack-shape"
    )

    assert text == "token is [REDACTED]"
    assert redacted == 1


def test_a_cap_cuts_and_says_it_cut() -> None:
    text, _redacted, truncated = slack_history.redact_credentials("y" * 40, cap=10)

    assert truncated is True
    assert len(text) == 10
    assert text.endswith("…")


def test_no_cap_means_uncapped_rather_than_a_cap_of_zero() -> None:
    link = "https://example.slack.com/archives/C1/p" + "9" * 400

    text, _redacted, truncated = slack_history.redact_credentials(link)

    assert text == link
    assert truncated is False


# --------------------------------------------------------------------------
# What an app posted, when the app did not post it in ``text``
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_written_in_blocks_is_not_read_as_nearly_empty() -> None:
    result = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "text": "New alert",
                "blocks": [
                    {
                        "type": "section",
                        "block_id": "b1",
                        "text": {"type": "mrkdwn", "text": "Build 4821 failed"},
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": "Branch: develop"},
                            {"type": "mrkdwn", "text": "Owner: release"},
                        ],
                    },
                ],
            }
        ]
    )

    message = result["messages"][0]
    assert message["text"] == "New alert"
    assert message["block_text"] == (
        "Build 4821 failed\nBranch: develop\nOwner: release"
    )


@pytest.mark.asyncio
async def test_block_kit_vocabulary_is_not_filed_as_something_a_person_wrote() -> None:
    result = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "text": "",
                "blocks": [
                    {
                        "type": "actions",
                        "block_id": "B0DEADBEEF",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "approve",
                                "value": "run-77",
                                "text": {"type": "plain_text", "text": "Approve"},
                            }
                        ],
                    }
                ],
            }
        ]
    )

    block_text = result["messages"][0]["block_text"]
    assert block_text == "Approve"
    for noise in ("actions", "B0DEADBEEF", "approve", "run-77", "plain_text"):
        assert noise not in block_text


@pytest.mark.asyncio
async def test_an_attachment_body_is_kept_and_said_only_once() -> None:
    result = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "text": "",
                "attachments": [
                    {
                        "color": "#36a64f",
                        "pretext": "Deploy report",
                        "title": "v30 is live",
                        "text": "Rolled out to every region.",
                        "fallback": "Rolled out to every region.",
                    }
                ],
            }
        ]
    )

    message = result["messages"][0]
    assert message["attachment_text"] == (
        "Deploy report\nv30 is live\nRolled out to every region."
    )
    assert "block_text" not in message


@pytest.mark.asyncio
async def test_message_metadata_is_asked_for_and_kept() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT", "url": "https://example.slack.com/"}],
            "conversations_history": [
                {
                    "messages": [
                        {
                            "ts": "199999.0",
                            "user": "U1",
                            "text": "",
                            "reply_count": 1,
                            "latest_reply": "199999.5",
                            "metadata": {
                                "event_type": "incident_opened",
                                "event_payload": {
                                    "severity": "major",
                                    "summary": "Gateway is refusing writes",
                                },
                            },
                        }
                    ]
                }
            ],
            "conversations_replies": [
                {
                    "messages": [
                        {"ts": "199999.0"},
                        {"ts": "199999.5", "user": "U2", "text": "on it"},
                    ]
                }
            ],
        }
    )

    result = json.loads(await _toolkit(client).read_slack_conversation())

    message = result["messages"][0]
    assert message["metadata_text"] == (
        "incident_opened\nmajor\nGateway is refusing writes"
    )
    assert client.calls["conversations_history"][0]["include_all_metadata"] is True
    assert client.calls["conversations_replies"][0]["include_all_metadata"] is True


# ── reading one thread ───────────────────────────────────────────────────────


class _ThreadClient:
    """``conversations.replies`` answering the way the live endpoint answers.

    A root's ts returns the whole thread. A reply's ts returns that reply on
    its own, whatever the endpoint documents, which is why a thread has to be
    read again from the ``thread_ts`` the answer states. A ts belonging to
    neither is declined as ``thread_not_found``.

    ``expands_a_reply`` models the documented answer instead, where any ts in
    the thread returns all of it. Both are worth covering: the second read
    exists for the first case and must not happen in the second.
    """

    def __init__(
        self,
        root: dict[str, Any],
        replies: list[dict[str, Any]],
        *,
        info: dict[str, Any] | None = None,
        page_size: int = 200,
        expands_a_reply: bool = False,
    ) -> None:
        self.root = root
        self.replies = replies
        self.info = info
        self.page_size = page_size
        self.expands_a_reply = expands_a_reply
        self.calls: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def _thread(self) -> list[dict[str, Any]]:
        root_ts = str(self.root["ts"])
        parent = dict(self.root)
        if self.replies:
            # Slack states thread_ts on a message that is in a thread and on no
            # other, so a message nothing hangs from is its own root and says
            # nothing about a thread.
            parent.setdefault("thread_ts", root_ts)
        return [parent] + [
            dict(reply, thread_ts=root_ts) for reply in self.replies
        ]

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["auth_test"].append(kwargs)
        return {"user_id": "U-BOT", "bot_id": "B-BOT", "url": "https://x.slack.com"}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["conversations_history"].append(kwargs)
        return {"messages": [], "has_more": False}

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["conversations_replies"].append(dict(kwargs))
        thread = self._thread()
        asked = str(kwargs.get("ts") or "")
        if asked != str(self.root["ts"]) and not self.expands_a_reply:
            named = [item for item in thread if str(item["ts"]) == asked]
            if not named:
                raise _FakeSlackError(
                    _FakeResponse(
                        status_code=200,
                        data={"ok": False, "error": "thread_not_found"},
                    )
                )
            return {"messages": named, "has_more": False}
        start = int(kwargs.get("cursor") or 0)
        page = thread[start : start + self.page_size]
        has_more = start + self.page_size < len(thread)
        response: dict[str, Any] = {"messages": page, "has_more": has_more}
        if has_more:
            response["response_metadata"] = {
                "next_cursor": str(start + self.page_size)
            }
        return response

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["conversations_info"].append(kwargs)
        if self.info is None:
            raise _FakeSlackError(
                _FakeResponse(status_code=200, data={"ok": False, "error": "x"})
            )
        return {"channel": self.info}


def _thread_client(**kwargs: Any) -> _ThreadClient:
    """One root at 199900 with three replies, the newest at 199903."""
    return _ThreadClient(
        {"ts": "199900.000000", "user": "U1", "text": "root", "reply_count": 3},
        [
            {"ts": "199901.000000", "user": "U2", "text": "reply one"},
            {"ts": "199902.000000", "user": "U2", "text": "reply two"},
            {"ts": "199903.000000", "user": "U3", "text": "reply three"},
        ],
        **kwargs,
    )


async def _read(client: Any, **kwargs: Any) -> dict[str, Any]:
    return json.loads(await _toolkit(client).read_slack_conversation(**kwargs))


@pytest.mark.asyncio
async def test_a_named_thread_is_entered_directly_and_no_channel_is_walked() -> None:
    client = _thread_client()

    result = await _read(client, ts="199900.000000")

    assert result["ok"] is True
    assert [item["ts"] for item in result["messages"]] == [
        "199900.000000",
        "199901.000000",
        "199902.000000",
        "199903.000000",
    ]
    assert "conversations_history" not in client.calls
    assert result["window"]["mode"] == "thread"
    assert result["window"]["ts"] == "199900.000000"
    assert result["window"]["requested_hours"] is None


@pytest.mark.asyncio
async def test_a_reply_ts_names_the_thread_it_is_in_rather_than_a_root_of_its_own(
) -> None:
    client = _thread_client()

    result = await _read(client, ts="199902.000000")

    by_ts = {item["ts"]: item for item in result["messages"]}
    assert sorted(by_ts) == [
        "199900.000000",
        "199901.000000",
        "199902.000000",
        "199903.000000",
    ]
    assert by_ts["199900.000000"]["is_thread_reply"] is False
    assert result["coverage"]["root_messages_returned"] == 1
    reply = by_ts["199902.000000"]
    assert reply["is_thread_reply"] is True
    assert reply["thread_ts"] == "199900.000000"
    assert "thread_ts=199900.000000" in reply["permalink"]
    # The argument itself is what Slack was asked for first, unaltered, and the
    # root only once the answer named it.
    assert [call["ts"] for call in client.calls["conversations_replies"]] == [
        "199902.000000",
        "199900.000000",
    ]
    assert (
        "ts_named_a_reply_read_from_its_root" in result["coverage"]["warnings"]
    )
    assert result["window"]["ts"] == "199902.000000"


@pytest.mark.asyncio
async def test_a_root_ts_costs_the_one_call_it_has_always_cost() -> None:
    client = _thread_client()

    result = await _read(client, ts="199900.000000")

    assert len(result["messages"]) == 4
    assert len(client.calls["conversations_replies"]) == 1
    assert result["coverage"]["thread_pages"] == 1
    assert (
        "ts_named_a_reply_read_from_its_root"
        not in result["coverage"]["warnings"]
    )


@pytest.mark.asyncio
async def test_a_reply_ts_slack_answers_in_full_is_not_read_a_second_time() -> None:
    client = _thread_client(expands_a_reply=True)

    result = await _read(client, ts="199902.000000")

    assert [item["ts"] for item in result["messages"]] == [
        "199900.000000",
        "199901.000000",
        "199902.000000",
        "199903.000000",
    ]
    assert len(client.calls["conversations_replies"]) == 1
    assert (
        "ts_named_a_reply_read_from_its_root"
        not in result["coverage"]["warnings"]
    )


@pytest.mark.asyncio
async def test_a_ts_naming_a_message_in_no_thread_returns_that_message() -> None:
    client = _ThreadClient({"ts": "199900.000000", "user": "U1", "text": "alone"}, [])

    result = await _read(client, ts="199900.000000")

    assert result["ok"] is True
    assert [item["ts"] for item in result["messages"]] == ["199900.000000"]
    assert result["messages"][0]["is_thread_reply"] is False
    assert result["coverage"]["root_messages_returned"] == 1
    assert result["coverage"]["thread_replies_returned"] == 0
    assert len(client.calls["conversations_replies"]) == 1
    assert result["window"]["mode"] == "thread"


@pytest.mark.asyncio
async def test_a_ts_slack_will_not_resolve_says_which_call_it_declined() -> None:
    client = _thread_client()

    result = await _read(client, ts="100000.000000")

    assert result["ok"] is False
    assert result["error"] == "thread_not_found"
    assert result.get("detail") == "Slack refused conversations.replies"
    assert result["messages"] == []


def _old_thread() -> _ThreadClient:
    """A thread whose every message is older than the default day.

    The toolkit's clock reads 200000, so a 24-hour window starts at 113600 and
    a thread at 100000 falls wholly outside it.
    """
    return _ThreadClient(
        {"ts": "100000.000000", "user": "U1", "text": "old root", "reply_count": 1},
        [{"ts": "100001.000000", "user": "U2", "text": "old reply"}],
    )


@pytest.mark.asyncio
async def test_naming_a_thread_does_not_impose_a_day_long_window() -> None:
    result = await _read(_old_thread(), ts="100000.000000")

    assert [item["ts"] for item in result["messages"]] == [
        "100000.000000",
        "100001.000000",
    ]
    assert result["window"]["mode"] == "thread"
    assert result["window"]["cutoff_ts"] is None
    assert result["window"]["cutoff_iso_utc"] is None
    assert result["window"]["requested_hours"] is None


@pytest.mark.asyncio
async def test_a_stated_twenty_four_hours_is_not_what_omitting_hours_means(
) -> None:
    omitted = await _read(_old_thread(), ts="100000.000000")
    stated = await _read(_old_thread(), ts="100000.000000", hours=24)

    assert omitted["window"]["mode"] == "thread"
    assert len(omitted["messages"]) == 2
    assert stated["window"]["mode"] == "hours"
    assert stated["window"]["requested_hours"] == 24
    # The root still comes back, marked as the context it is, so the answer
    # says what it read rather than reading as an empty thread.
    assert [item["ts"] for item in stated["messages"]] == ["100000.000000"]
    assert stated["messages"][0]["outside_window_context"] is True


@pytest.mark.asyncio
async def test_a_stated_number_windows_a_thread_read_as_it_windows_any_other(
) -> None:
    narrowed = await _read(_old_thread(), ts="100000.000000", hours=1)

    assert [item["ts"] for item in narrowed["messages"]] == ["100000.000000"]
    assert narrowed["messages"][0]["outside_window_context"] is True
    assert narrowed["window"]["mode"] == "hours"


@pytest.mark.asyncio
async def test_the_card_advertises_no_default_a_thread_read_would_ignore(
) -> None:
    card = _toolkit(_thread_client()).get_tools()[0]._card
    hours = card.input_params["properties"]["hours"]

    assert "default" not in hours
    assert "the last 24 hours" in hours["description"]
    assert "Leave this out beside ts" in hours["description"]


@pytest.mark.asyncio
async def test_include_threads_is_reported_as_inert_beside_a_thread_read() -> None:
    for include_threads in (True, False):
        result = await _read(
            _thread_client(), ts="199900.000000", include_threads=include_threads
        )

        assert result["ok"] is True
        assert len(result["messages"]) == 4
        assert (
            "include_threads_not_used_on_a_thread_read"
            in result["coverage"]["warnings"]
        )


@pytest.mark.asyncio
async def test_before_ts_pages_a_thread_by_its_replies_and_keeps_the_root() -> None:
    walked: list[str] = []
    before_ts: str | None = None
    for _ in range(5):
        result = await _read(
            _thread_client(), ts="199900.000000", max_messages=2, before_ts=before_ts
        )
        walked.extend(
            item["ts"] for item in result["messages"] if item["is_thread_reply"]
        )
        before_ts = result["coverage"]["next_before_ts"]
        assert result["messages"][0]["ts"] == "199900.000000"
        if before_ts is None:
            break

    assert before_ts is None
    assert sorted(walked) == [
        "199901.000000",
        "199902.000000",
        "199903.000000",
    ], "the walk lost, repeated or invented a reply"


@pytest.mark.asyncio
async def test_a_bound_reached_inside_a_thread_read_stops_at_a_reply() -> None:
    result = await _read(_thread_client(), ts="199900.000000", max_messages=2)

    assert [item["ts"] for item in result["messages"]] == [
        "199900.000000",
        "199903.000000",
    ]
    assert result["coverage"]["status"] == "partial"
    assert "message_limit" in result["coverage"]["partial_reasons"]
    assert result["coverage"]["next_before_ts"] == "199903.000000"


@pytest.mark.asyncio
async def test_a_ts_that_is_not_a_slack_timestamp_is_refused() -> None:
    result = await _read(_thread_client(), ts="yesterday")

    assert result["ok"] is False
    assert result["error"] == "ts_must_be_a_slack_timestamp"
    assert result["messages"] == []


# ── walking forward ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_after_ts_returns_only_what_arrived_since_the_position_it_names(
) -> None:
    whole, _ = await _slice(_sample_channel())
    latest = whole["coverage"]["latest_message_ts"]

    caught_up = await _read(_sample_channel(), after_ts=latest)

    assert caught_up["ok"] is True
    assert [
        item["ts"]
        for item in caught_up["messages"]
        if not item["outside_window_context"]
    ] == []
    assert caught_up["window"]["mode"] == "after_ts"
    assert caught_up["window"]["after_ts"] == latest
    assert caught_up["window"]["requested_hours"] is None

    since_the_middle = await _read(_sample_channel(), after_ts="199905.000000")
    fresh = {
        item["ts"]
        for item in since_the_middle["messages"]
        if not item["outside_window_context"]
    }
    assert fresh == {
        "199906.000000",
        "199907.000000",
        "199908.000000",
        "199909.000000",
        "199950.000000",
        "199951.000000",
    }


@pytest.mark.asyncio
async def test_after_iso_utc_takes_a_date_where_after_ts_takes_a_position() -> None:
    named_date = slack_history._iso_utc(199_905.0)
    assert named_date is not None

    result = await _read(_sample_channel(), after_iso_utc=named_date)

    fresh = {
        item["ts"]
        for item in result["messages"]
        if not item["outside_window_context"]
    }
    assert fresh == {
        "199906.000000",
        "199907.000000",
        "199908.000000",
        "199909.000000",
        "199950.000000",
        "199951.000000",
    }
    assert result["window"]["mode"] == "after_iso_utc"
    assert result["window"]["after_iso_utc"] == named_date

    refused = await _read(_sample_channel(), after_iso_utc="yesterday")
    assert refused["ok"] is False
    assert refused["error"] == "after_iso_utc_must_be_an_iso_instant"


@pytest.mark.asyncio
async def test_a_position_beats_a_date_beats_a_duration_and_the_result_says_so(
) -> None:
    both = await _read(
        _sample_channel(),
        hours=1,
        all_history=True,
        after_iso_utc="1976-05-01",
        after_ts="199905.000000",
    )
    assert both["window"]["mode"] == "after_ts"
    assert both["window"]["cutoff_ts"] == "199905.000000"
    assert both["window"]["requested_hours"] is None

    without_position = await _read(
        _sample_channel(), hours=1, all_history=True, after_iso_utc="1976-05-01"
    )
    assert without_position["window"]["mode"] == "after_iso_utc"


@pytest.mark.parametrize(
    ("window_arguments", "channel_mode", "thread_mode"),
    [
        ({}, "hours", "thread"),
        ({"hours": 1}, "hours", "hours"),
        ({"all_history": True}, "all_history", "all_history"),
        ({"all_history": True, "hours": 1}, "all_history", "all_history"),
        ({"after_iso_utc": "1976-05-01"}, "after_iso_utc", "after_iso_utc"),
        (
            {"after_iso_utc": "1976-05-01", "all_history": True, "hours": 1},
            "after_iso_utc",
            "after_iso_utc",
        ),
        ({"after_ts": "199905.000000"}, "after_ts", "after_ts"),
        (
            {
                "after_ts": "199905.000000",
                "after_iso_utc": "1976-05-01",
                "all_history": True,
                "hours": 1,
            },
            "after_ts",
            "after_ts",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_older_edge_settles_in_one_order_whether_or_not_ts_is_named(
    window_arguments: dict[str, Any], channel_mode: str, thread_mode: str
) -> None:
    channel = _sample_channel()
    read = await _read(channel, **window_arguments)
    assert read["window"]["mode"] == channel_mode

    thread = _thread_client()
    named = await _read(thread, ts="199900.000000", **window_arguments)
    assert named["window"]["mode"] == thread_mode
    # ``window.mode`` orders the window's older edge and does not name the call
    # that was made. A ts reads the thread through conversations.replies under
    # every one of these, and walks no channel to do it.
    assert thread.calls["conversations_replies"]
    assert "conversations_history" not in thread.calls


# ── what one message now says about itself ───────────────────────────────────


def _one_message(message: dict[str, Any]) -> _FakeClient:
    return _FakeClient(
        {
            "auth_test": [
                {"user_id": "U-BOT", "bot_id": "B-BOT", "url": "https://x.slack.com"}
            ],
            "conversations_history": [{"messages": [message]}],
            "conversations_info": [{"channel": {"name": "research"}}],
        }
    )


@pytest.mark.asyncio
async def test_a_message_that_is_not_a_plain_message_says_which_kind_it_is() -> None:
    joined = await _read(
        _one_message(
            {
                "ts": "199999.0",
                "user": "U1",
                "subtype": "channel_join",
                "text": "has joined the channel",
            }
        )
    )
    assert joined["messages"][0].get("subtype") == "channel_join"

    plain = await _read(
        _one_message({"ts": "199999.0", "user": "U1", "text": "hello"})
    )
    assert "subtype" not in plain["messages"][0]


@pytest.mark.asyncio
async def test_a_message_posted_by_any_app_is_marked_as_one() -> None:
    from_an_app = await _read(
        _one_message(
            {
                "ts": "199999.0",
                "bot_id": "B-DEPLOYER",
                "username": "Deployer",
                "text": "build 41 is out",
            }
        )
    )
    record = from_an_app["messages"][0]
    assert record.get("is_author_bot") is True
    assert record["is_own_bot_message"] is False

    from_a_person = await _read(
        _one_message({"ts": "199999.0", "user": "U1", "text": "thanks"})
    )
    assert from_a_person["messages"][0].get("is_author_bot") is False


@pytest.mark.asyncio
async def test_a_message_changed_after_it_was_posted_says_when() -> None:
    edited = await _read(
        _one_message(
            {
                "ts": "199990.000000",
                "user": "U1",
                "text": "the meeting is at four",
                "edited": {"user": "U1", "ts": "199995.000000"},
            }
        )
    )
    record = edited["messages"][0]
    assert record.get("edited") == {
        "ts": "199995.000000",
        "ts_iso_utc": slack_history._iso_utc(199_995.0),
    }

    untouched = await _read(
        _one_message({"ts": "199990.000000", "user": "U1", "text": "as posted"})
    )
    assert "edited" not in untouched["messages"][0]


# ── naming the conversation, and naming a refused call ───────────────────────


@pytest.mark.asyncio
async def test_the_snapshot_names_the_conversation_and_not_only_its_id() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "hi"}]}
            ],
            "conversations_info": [
                {
                    "channel": {
                        "name": "research",
                        "topic": {"value": "what we are reading"},
                        "purpose": {"value": "the reading group"},
                    }
                }
            ],
        }
    )

    result = await _read(client)

    assert result.get("chat_name") == "research"
    assert result.get("topic") == "what we are reading"
    assert result.get("purpose") == "the reading group"


@pytest.mark.asyncio
async def test_a_conversation_whose_record_is_refused_still_returns_its_messages(
) -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "hi"}]}
            ],
            "conversations_info": [
                _FakeSlackError(
                    _FakeResponse(
                        status_code=200,
                        data={"ok": False, "error": "missing_scope"},
                    )
                )
            ],
        }
    )

    result = await _read(client)

    assert result["ok"] is True
    assert len(result["messages"]) == 1
    assert result.get("chat_name") == ""
    assert result["coverage"].get("slack_calls_refused") == ["conversations.info"]


@pytest.mark.asyncio
async def test_a_scan_that_slack_refuses_says_which_call_it_refused() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                _FakeSlackError(
                    _FakeResponse(
                        status_code=200,
                        data={"ok": False, "error": "missing_scope"},
                    )
                )
            ],
        }
    )

    result = await _read(client)

    assert result["ok"] is False
    assert result["error"] == "missing_scope"
    assert result.get("detail") == "Slack refused conversations.history"


# ── one fact about the workspace, asked once ─────────────────────────────────


@pytest.mark.asyncio
async def test_auth_test_is_asked_once_for_the_life_of_the_toolkit() -> None:
    client = _FakeClient(
        {
            # Two scripted answers, so that a toolkit asking twice still works
            # and the count below is what fails rather than the read.
            "auth_test": [
                {"user_id": "U-BOT", "url": "https://acme.slack.com/"},
                {"user_id": "U-BOT", "url": "https://acme.slack.com/"},
            ],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "one"}]},
                {"messages": [{"ts": "199998.0", "user": "U1", "text": "two"}]},
            ],
            "conversations_info": [
                {"channel": {"name": "research"}},
                {"channel": {"name": "research"}},
            ],
        }
    )
    toolkit = _toolkit(client)

    first = json.loads(await toolkit.read_slack_conversation())
    second = json.loads(await toolkit.read_slack_conversation())

    assert first["ok"] is True and second["ok"] is True
    assert len(client.calls["auth_test"]) == 1
    assert await toolkit._workspace_file_hosts() == frozenset(
        {"files.slack.com", "acme.slack.com"}
    )
    assert len(client.calls["auth_test"]) == 1


# ── a window that is not a window ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_hours_value_longer_than_any_history_is_refused_not_widened(
) -> None:
    refused = await _read(_sample_channel(), hours=1_000_000)

    assert refused["ok"] is False
    assert refused["error"] == "hours_must_name_a_window"
    assert "all_history" in refused["detail"]
    assert refused["messages"] == []

    allowed = await _read(_sample_channel(), hours=24 * 365 * 10)
    assert allowed["ok"] is True
    assert allowed["window"]["mode"] == "hours"


# ── a reply the sender also sent to the channel ──────────────────────────────


class _BroadcastClient(_ChannelClient):
    """A channel walk that also records which ts each thread read asked for."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.replies_ts: list[str] = []

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.replies_ts.append(str(kwargs["ts"]))
        return await super().conversations_replies(**kwargs)


def _broadcast_channel(**broadcast_extra: Any) -> _BroadcastClient:
    """A broadcast at 199905 beside a genuine thread root at 199906.

    Slack sends a broadcast as a channel item of its own, stating the parent's
    ts in ``thread_ts`` and its own in ``ts``. The parent here sits at 199000,
    far older than anything this page holds, which is the case where nothing
    else in the walk corrects the record. A genuine root states a ``thread_ts``
    equal to its own ts, which is the shape 199906 carries.
    """
    roots = [
        {"ts": "199902.000000", "user": "U1", "text": "plain"},
        {
            "ts": "199906.000000",
            "user": "U1",
            "text": "a root of its own",
            "thread_ts": "199906.000000",
            "reply_count": 1,
            "latest_reply": "199908.000000",
        },
        dict(
            {
                "ts": "199905.000000",
                "thread_ts": "199000.000000",
                "subtype": "thread_broadcast",
                "user": "U2",
                "text": "sent to the channel as well",
            },
            **broadcast_extra,
        ),
    ]
    replies = {
        "199906.000000": [
            {"ts": "199908.000000", "user": "U3", "text": "a reply in the thread"}
        ]
    }
    return _BroadcastClient(roots, replies)


@pytest.mark.asyncio
async def test_a_broadcast_reply_states_the_thread_it_was_written_in() -> None:
    result, _ = await _slice(_broadcast_channel())

    broadcast = next(
        item for item in result["messages"] if item["ts"] == "199905.000000"
    )
    assert broadcast["thread_ts"] == "199000.000000"
    assert broadcast["is_thread_reply"] is True
    # A reply count belongs to a root. The broadcast is not one, so it states
    # none rather than stating zero.
    assert "reply_count" not in broadcast

    root = next(item for item in result["messages"] if item["ts"] == "199906.000000")
    assert root["thread_ts"] == "199906.000000"
    assert root["is_thread_reply"] is False
    assert root["reply_count"] == 1


@pytest.mark.asyncio
async def test_a_broadcast_is_not_expanded_as_a_thread_of_its_own() -> None:
    client = _broadcast_channel(reply_count=4, latest_reply="199905.000000")

    result, _ = await _slice(client)

    assert client.replies_ts == ["199906.000000"]
    assert "thread_replies_not_returned" not in result["coverage"]["partial_reasons"]
    assert result["coverage"]["status"] == "complete"


@pytest.mark.asyncio
async def test_a_broadcast_does_not_move_the_resume_position_to_its_parent() -> None:
    client = _BroadcastClient(
        [
            {
                "ts": "199907.000000",
                "thread_ts": "199000.000000",
                "subtype": "thread_broadcast",
                "user": "U2",
                "text": "sent to the channel as well",
            },
            {"ts": "199906.000000", "user": "U1", "text": "plain"},
            {"ts": "199905.000000", "user": "U1", "text": "plain"},
        ],
        {},
    )

    result = json.loads(
        await _toolkit(client).read_slack_conversation(
            all_history=True, max_messages=2
        )
    )

    assert result["ok"] is True
    assert result["coverage"]["status"] == "partial"
    broadcast = next(
        item for item in result["messages"] if item["ts"] == "199907.000000"
    )
    assert broadcast["thread_ts"] == "199000.000000"
    # 199907 and 199906 are in hand and 199905 is not, so the next slice starts
    # above 199906. The parent at 199000 is what a thread_ts read would have
    # offered, and everything between it and 199906 would have been skipped.
    assert result["coverage"]["next_before_ts"] == "199906.000000"


# ── who reacted, and how much of that is known ───────────────────────────────


def _reacted(reactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"ts": "199999.0", "user": "U1", "text": "a message", "reactions": reactions}]


@pytest.mark.asyncio
async def test_a_reaction_names_the_emoji_and_the_people_who_left_it() -> None:
    result = await _history_with(
        _reacted([{"name": "eyes", "count": 2, "users": ["U1", "U2"]}])
    )

    reaction = result["messages"][0]["reactions"][0]
    assert reaction["emoji_name"] == "eyes"
    assert "name" not in reaction
    assert reaction["count"] == 2
    assert reaction["reactor_user_ids"] == ["U1", "U2"]
    # Nothing was short and nothing was cut, so neither is claimed.
    assert "reactors_partial" not in reaction
    assert "reactors_truncated" not in reaction


@pytest.mark.asyncio
async def test_a_reactor_list_shorter_than_its_count_says_that_it_is_short() -> None:
    result = await _history_with(
        _reacted([{"name": "eyes", "count": 4, "users": ["U1"]}])
    )

    reaction = result["messages"][0]["reactions"][0]
    assert reaction["count"] == 4
    assert reaction["reactor_user_ids"] == ["U1"]
    assert reaction["reactors_partial"] is True
    assert "reactors_truncated" not in reaction


@pytest.mark.asyncio
async def test_a_reactor_list_this_record_cut_says_so_as_a_different_thing() -> None:
    reactors = [f"U{index:03d}" for index in range(30)]

    result = await _history_with(
        _reacted([{"name": "eyes", "count": 30, "users": reactors}])
    )

    reaction = result["messages"][0]["reactions"][0]
    assert reaction["count"] == 30
    assert len(reaction["reactor_user_ids"]) == 20
    assert reaction["reactors_truncated"] is True
    # Slack sent everybody it counted, so nothing about its answer was partial.
    assert "reactors_partial" not in reaction
    assert result["coverage"]["max_reactors_per_reaction"] == 20


@pytest.mark.asyncio
async def test_the_reactor_bound_is_the_callers_to_move() -> None:
    reactors = [f"U{index:03d}" for index in range(30)]
    raised = await _history_with(
        _reacted([{"name": "eyes", "count": 30, "users": reactors}]),
        read_kwargs={"max_reactors_per_reaction": 30},
    )

    reaction = raised["messages"][0]["reactions"][0]
    assert reaction["reactor_user_ids"] == reactors
    assert "reactors_truncated" not in reaction
    assert raised["coverage"]["max_reactors_per_reaction"] == 30

    lowered = await _history_with(
        _reacted([{"name": "eyes", "count": 30, "users": reactors}]),
        read_kwargs={"max_reactors_per_reaction": 3},
    )

    cut = lowered["messages"][0]["reactions"][0]
    assert cut["reactor_user_ids"] == reactors[:3]
    assert cut["reactors_truncated"] is True
    assert lowered["coverage"]["max_reactors_per_reaction"] == 3


@pytest.mark.asyncio
async def test_every_reaction_a_message_holds_is_kept() -> None:
    result = await _history_with(
        _reacted(
            [
                {"name": f"emoji-{index}", "count": 1, "users": ["U1"]}
                for index in range(40)
            ]
        )
    )

    message = result["messages"][0]
    assert len(message["reactions"]) == 40
    assert [entry["emoji_name"] for entry in message["reactions"]] == [
        f"emoji-{index}" for index in range(40)
    ]
    # No flag, because there is nothing left for one to be about.
    assert "reactions_truncated" not in message


def _reaction_lookup_client() -> _FakeClient:
    return _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {
                            "ts": "199999.0",
                            "user": "U1",
                            "text": "shipped",
                            "reactions": [
                                {"name": "tada", "count": 2, "users": ["U2", "U3"]}
                            ],
                        },
                        {"ts": "199998.0", "user": "U2", "text": "nice"},
                    ]
                }
            ],
            "users_info": [
                {"user": {"id": "U1", "profile": {"display_name": "Alice"}}},
                {"user": {"id": "U2", "profile": {"display_name": "Bob"}}},
                {"user": {"id": "U3", "profile": {"display_name": "Cleo"}}},
            ],
        }
    )


def _lookup_toolkit(client: _FakeClient, lookups: int) -> SlackHistoryToolkit:
    return SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_enforce_audience": True,
        },
        client=client,
        now=lambda: 200_000.0,
        max_user_lookups=lookups,
    )


@pytest.mark.asyncio
async def test_reactor_ids_are_resolved_in_the_same_pass_the_authors_use() -> None:
    client = _reaction_lookup_client()

    result = json.loads(
        await _lookup_toolkit(client, 10).read_slack_conversation(
            include_threads=False
        )
    )

    reaction = result["messages"][1]["reactions"][0]
    assert reaction["reactor_user_ids"] == ["U2", "U3"]
    assert reaction["reactor_names"] == ["Bob", "Cleo"]
    assert sorted(call["user"] for call in client.calls["users_info"]) == [
        "U1",
        "U2",
        "U3",
    ]


@pytest.mark.asyncio
async def test_the_lookup_budget_is_spent_on_authors_before_reactors() -> None:
    client = _reaction_lookup_client()

    result = json.loads(
        await _lookup_toolkit(client, 2).read_slack_conversation(
            include_threads=False
        )
    )

    assert [call["user"] for call in client.calls["users_info"]] == ["U1", "U2"]
    assert [item["author_name"] for item in result["messages"]] == ["Bob", "Alice"]
    reaction = result["messages"][1]["reactions"][0]
    assert reaction["reactor_names"] == ["Bob", "U3"]
    assert "author_name_lookup_limit" in result["coverage"]["warnings"]


# ── a rich-text walk that stopped short says so ──────────────────────────────


@pytest.mark.asyncio
async def test_block_text_cut_by_the_string_bound_says_it_was_cut() -> None:
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"line {index}"}}
        for index in range(slack_history.MAX_RICH_TEXT_STRINGS + 5)
    ]

    result = await _history_with(
        [{"ts": "199999.0", "user": "U1", "text": "", "blocks": blocks}]
    )

    message = result["messages"][0]
    assert message["block_text_truncated"] is True
    assert len(message["block_text"].splitlines()) == (
        slack_history.MAX_RICH_TEXT_STRINGS
    )


@pytest.mark.asyncio
async def test_attachment_text_cut_by_the_depth_bound_says_it_was_cut() -> None:
    buried: dict[str, Any] = {"text": "the readable line"}
    for _ in range(slack_history.MAX_RICH_TEXT_DEPTH + 2):
        buried = {"nested": buried}

    result = await _history_with(
        [{"ts": "199999.0", "user": "U1", "text": "hello", "attachments": [buried]}]
    )

    message = result["messages"][0]
    # Nothing readable came back at all, which is exactly the case a bare list
    # of strings cannot tell from an attachment that held no text. The flag is
    # what tells them apart, so it is stated with no text beside it.
    assert "attachment_text" not in message
    assert message["attachment_text_truncated"] is True

    shallow = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "text": "hello",
                "attachments": [{"text": "the readable line"}],
            }
        ]
    )
    assert shallow["messages"][0]["attachment_text"] == "the readable line"
    assert "attachment_text_truncated" not in shallow["messages"][0]


@pytest.mark.asyncio
async def test_a_payload_within_both_bounds_claims_no_truncation() -> None:
    result = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "text": "",
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "one"}}
                ],
                "attachments": [{"text": "two"}],
                "metadata": {"event_type": "deploy", "event_payload": {"env": "prod"}},
            }
        ]
    )

    message = result["messages"][0]
    assert message["block_text"] == "one"
    assert message["attachment_text"] == "two"
    assert message["metadata_text"] == "deploy\nprod"
    assert not [key for key in message if key.endswith("_text_truncated")]


def test_the_connector_still_reads_a_bare_list_of_strings() -> None:
    payload = [{"text": "one"}, {"text": "two"}]

    assert slack_rich_text.walk_strings(payload, slack_rich_text.RICH_TEXT_KEYS) == [
        "one",
        "two",
    ]
    assert slack_rich_text.walk_strings_bounded(
        payload, slack_rich_text.RICH_TEXT_KEYS
    ) == (["one", "two"], False)


# ── paging a long thread ─────────────────────────────────────────────────────


class _LongThreadClient:
    """A thread read that honours ``latest`` and pages the range it leaves.

    Slack answers ``conversations.replies`` with the root and then the replies
    inside the bounds, a page at a time. A fake that ignored ``latest`` would
    report the same messages whatever the tool asked for, and the cost this
    tests is exactly what the tool asks for.
    """

    ROOT_TS = "199000.000000"

    def __init__(self, reply_count: int, page_size: int = 200) -> None:
        self.page_size = page_size
        self.root = {
            "ts": self.ROOT_TS,
            "user": "U1",
            "text": "root",
            "thread_ts": self.ROOT_TS,
            "reply_count": reply_count,
        }
        self.replies = [
            {
                "ts": f"199{index:03d}.000000",
                "user": "U2",
                "text": f"reply {index}",
                "thread_ts": self.ROOT_TS,
            }
            for index in range(1, reply_count + 1)
        ]
        self.reply_calls: list[dict[str, Any]] = []

    def _in_range(self, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        latest = float(kwargs["latest"])
        inclusive = bool(kwargs.get("inclusive"))
        return [
            reply
            for reply in self.replies
            if (float(reply["ts"]) <= latest if inclusive else float(reply["ts"]) < latest)
        ]

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        return {"user_id": "U-BOT", "bot_id": "B-BOT", "url": "https://x.slack.com"}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        return {"messages": [], "has_more": False}

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        return {"channel": {"name": "research"}}

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.reply_calls.append(dict(kwargs))
        # The root is answered whatever the bounds say, which is what the
        # neighbouring comment about ``oldest`` records having observed.
        selected = [self.root] + self._in_range(kwargs)
        start = int(kwargs.get("cursor") or 0)
        page = selected[start : start + self.page_size]
        has_more = start + self.page_size < len(selected)
        response: dict[str, Any] = {"messages": page, "has_more": has_more}
        if has_more:
            response["response_metadata"] = {
                "next_cursor": str(start + self.page_size)
            }
        return response


class _LatestIgnoringThreadClient(_LongThreadClient):
    """The same thread, from a server that pays no attention to ``latest``."""

    def _in_range(self, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self.replies)


@pytest.mark.asyncio
async def test_a_resumed_thread_slice_is_not_paged_from_the_newest_reply() -> None:
    bounded = _LongThreadClient(400)
    unbounded = _LongThreadClient(400)

    resumed = await _read(bounded, ts=_LongThreadClient.ROOT_TS, before_ts="199201.000000")
    await _read(unbounded, ts=_LongThreadClient.ROOT_TS)

    assert resumed["ok"] is True
    # 201 messages in range against a page of 200, where the whole thread is
    # 401 and costs three.
    assert len(bounded.reply_calls) == 2
    assert len(unbounded.reply_calls) == 3
    assert {call["latest"] for call in bounded.reply_calls} == {"199201.000000"}
    assert all(call.get("inclusive") is True for call in bounded.reply_calls)


@pytest.mark.asyncio
async def test_the_resume_bound_changes_the_price_and_not_the_messages() -> None:
    honoured = await _read(
        _LongThreadClient(400),
        ts=_LongThreadClient.ROOT_TS,
        before_ts="199201.000000",
    )
    ignored = await _read(
        _LatestIgnoringThreadClient(400),
        ts=_LongThreadClient.ROOT_TS,
        before_ts="199201.000000",
    )

    expected = [_LongThreadClient.ROOT_TS] + [
        f"199{index:03d}.000000" for index in range(1, 201)
    ]
    assert [item["ts"] for item in honoured["messages"]] == expected
    assert [item["ts"] for item in ignored["messages"]] == expected


class _EmptyUnderTheBoundClient(_LongThreadClient):
    """A server that answers nothing at all once ``latest`` is not the snapshot."""

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        if float(kwargs["latest"]) < 200_000.0:
            self.reply_calls.append(dict(kwargs))
            return {"messages": [], "has_more": False}
        return await super().conversations_replies(**kwargs)


@pytest.mark.asyncio
async def test_an_empty_answer_under_the_resume_bound_is_read_again_without_it(
) -> None:
    client = _EmptyUnderTheBoundClient(4)

    result = await _read(
        client, ts=_LongThreadClient.ROOT_TS, before_ts="199003.000000"
    )

    assert [item["ts"] for item in result["messages"]] == [
        _LongThreadClient.ROOT_TS,
        "199001.000000",
        "199002.000000",
    ]
    assert (
        "thread_read_retried_without_the_resume_bound"
        in result["coverage"]["warnings"]
    )
    # One bounded read, one unbounded read, and no third.
    assert [call["latest"] for call in client.reply_calls] == [
        "199003.000000",
        "200000.000000",
    ]


# ── where a windowed threaded scan stops ─────────────────────────────────────


async def _windowed(client: _ChannelClient, **kwargs: Any) -> dict[str, Any]:
    """One hour of a channel, which puts the cutoff at 196400."""
    result = json.loads(
        await _toolkit(client).read_slack_conversation(hours=1, **kwargs)
    )
    assert result["ok"] is True
    return result


@pytest.mark.asyncio
async def test_a_windowed_threaded_scan_stops_at_the_first_page_beyond_it(
) -> None:
    client = _ChannelClient(
        [
            {"ts": "199901.000000", "user": "U1", "text": "today"},
            {"ts": "199900.000000", "user": "U1", "text": "today"},
            {"ts": "190002.000000", "user": "U1", "text": "last week"},
            {"ts": "190001.000000", "user": "U1", "text": "last week"},
            {"ts": "190000.000000", "user": "U1", "text": "last week"},
        ],
        page_size=2,
    )

    result = await _windowed(client)

    # The second page is entirely older than the cutoff and names no reply
    # inside it, so the third is never asked for.
    assert len(client.history_calls) == 2
    assert result["coverage"]["history_pages"] == 2
    assert [item["ts"] for item in result["messages"]] == [
        "199900.000000",
        "199901.000000",
    ]
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["next_before_ts"] is None


@pytest.mark.asyncio
async def test_an_old_root_with_a_recent_reply_keeps_the_scan_going() -> None:
    client = _ChannelClient(
        [
            {"ts": "190001.000000", "user": "U1", "text": "old and quiet"},
            {
                "ts": "190000.000000",
                "user": "U1",
                "text": "old but still going",
                "reply_count": 1,
                "latest_reply": "199950.000000",
            },
            {"ts": "189001.000000", "user": "U1", "text": "older"},
            {"ts": "189000.000000", "user": "U1", "text": "older"},
            {"ts": "188001.000000", "user": "U1", "text": "older still"},
            {"ts": "188000.000000", "user": "U1", "text": "older still"},
        ],
        {
            "190000.000000": [
                {"ts": "199950.000000", "user": "U2", "text": "a reply today"}
            ]
        },
        page_size=2,
    )

    result = await _windowed(client)

    assert [item["ts"] for item in result["messages"]] == [
        "190000.000000",
        "199950.000000",
    ]
    root = result["messages"][0]
    assert root["outside_window_context"] is True
    assert result["messages"][1]["is_thread_reply"] is True
    # The first page held the thread, so the second was still worth asking for
    # and the third was not.
    assert len(client.history_calls) == 2
    assert result["coverage"]["next_before_ts"] is None


@pytest.mark.asyncio
async def test_a_scan_without_threads_still_lets_slack_apply_the_cutoff() -> None:
    client = _ChannelClient(
        [
            {"ts": "199901.000000", "user": "U1", "text": "today"},
            {"ts": "190002.000000", "user": "U1", "text": "last week"},
        ],
        page_size=2,
    )

    result = await _windowed(client, include_threads=False)

    assert client.history_calls[0]["oldest"] == "196400.000000"
    assert [item["ts"] for item in result["messages"]] == ["199901.000000"]
    assert result["coverage"]["next_before_ts"] is None


@pytest.mark.asyncio
async def test_an_all_history_scan_still_walks_the_channel_to_its_beginning(
) -> None:
    client = _ChannelClient(
        [
            {"ts": f"19{index:04d}.000000", "user": "U1", "text": "a message"}
            for index in range(9990, 9996)
        ],
        page_size=2,
    )

    result, returned = await _slice(client)

    assert len(client.history_calls) == 3
    assert len(returned) == 6
    assert result["coverage"]["next_before_ts"] is None


# ── the fields Slack already sent ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_root_says_when_its_thread_was_last_answered() -> None:
    result = await _history_with(
        [
            {
                "ts": "199900.000000",
                "user": "U1",
                "text": "a thread root",
                "reply_count": 7,
                "reply_users_count": 3,
                "latest_reply": "199950.000000",
            },
            {"ts": "199901.000000", "user": "U1", "text": "a message on its own"},
        ]
    )

    root = next(item for item in result["messages"] if item["ts"] == "199900.000000")
    assert root["reply_count"] == 7
    assert root["latest_reply_ts"] == "199950.000000"
    assert root["latest_reply_iso_utc"] is not None
    assert root["reply_users_count"] == 3

    alone = next(item for item in result["messages"] if item["ts"] == "199901.000000")
    assert alone["reply_count"] == 0
    assert "latest_reply_ts" not in alone
    assert "reply_users_count" not in alone


@pytest.mark.asyncio
async def test_an_app_post_names_the_app_and_not_only_its_label() -> None:
    result = await _history_with(
        [
            {
                "ts": "199900.000000",
                "username": "Uptime Robot",
                "bot_id": "B0123",
                "app_id": "A0456",
                "subtype": "bot_message",
                "text": "deploy finished",
            },
            {
                "ts": "199901.000000",
                "username": "Buildbot",
                "bot_id": "B0789",
                "bot_profile": {"name": "Buildbot", "app_id": "A0999"},
                "text": "build green",
            },
            {"ts": "199902.000000", "user": "U1", "text": "a person"},
        ]
    )

    first, second, person = result["messages"]
    assert first["author_bot_id"] == "B0123"
    assert first["author_app_id"] == "A0456"
    assert first["author_name"] == "Uptime Robot"
    assert first["author_user_id"] == ""
    # Slack states app_id on the bot profile rather than beside the message on
    # some payloads, and it is the same identifier either way.
    assert second["author_app_id"] == "A0999"
    assert "author_bot_id" not in person
    assert "author_app_id" not in person


@pytest.mark.asyncio
async def test_a_partial_thread_read_can_hand_back_messages_with_no_resume(
) -> None:
    result = await _read(_thread_client(), ts="199900.000000", max_messages=1)

    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["next_before_ts"] is None
    assert [item["ts"] for item in result["messages"]] == ["199900.000000"]


def test_the_card_states_the_paging_rules_that_hold() -> None:
    description = _toolkit(_thread_client()).get_tools()[0]._card.description

    assert "Thread pages repeat their root" in description
    assert "nothing is fetched twice" not in description
    assert "means the scan returned nothing" not in description
    assert "nothing further to page to" in description
