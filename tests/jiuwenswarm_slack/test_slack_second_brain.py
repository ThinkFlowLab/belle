"""Slack second-brain ingestion and delivery routing."""

import asyncio
from typing import Any

import pytest

from belle_agent.integrations.jiuwenswarm import config as belle_config
from belle_agent.platforms.slack.brain_ingest import brain_session_id

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
    SlackChannelOverride,
)


pytestmark = pytest.mark.usefixtures("isolated_slack_dedup_store")


class _FakeClient:
    """Record reactions and permalink requests from ingestion."""

    def __init__(self) -> None:
        self.reactions: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str, str]] = []
        self.permalinks = 0

    async def reactions_add(self, *, channel: str, timestamp: str, name: str) -> dict:
        self.reactions.append((channel, timestamp, name))
        return {"ok": True}

    async def reactions_remove(self, *, channel: str, timestamp: str, name: str) -> dict:
        self.removed.append((channel, timestamp, name))
        return {"ok": True}

    async def chat_getPermalink(self, *, channel: str, message_ts: str) -> dict:
        self.permalinks += 1
        return {"permalink": f"https://acme.slack.com/archives/{channel}/p{message_ts}"}


def _watched_channel(
    *,
    mode: list[str] | None = None,
    brain_channels: list[str] | None = None,
    brain_all: bool = False,
    brain_public: bool = False,
) -> SlackChannel:
    """Configure C-BRAIN for ingestion and mention-only replies by default."""
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            brain_ingest_channels=frozenset(
                brain_channels if brain_channels is not None else ["C-BRAIN"]
            ),
            brain_ingest_all_channels=brain_all,
            brain_ingest_public_channels=brain_public,
            conversation_overrides={
                "C-BRAIN": SlackChannelOverride(mode=frozenset(mode or ["mention"]))
            },
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._client = _FakeClient()
    channel._identity.bot_user_id = "U-BOT"
    return channel


def _file_event(**overrides: Any) -> dict[str, Any]:
    event = {
        "type": "message",
        "subtype": "file_share",
        "user": "U1",
        "team": "T-BRAIN",
        "channel": "C-BRAIN",
        "channel_type": "channel",
        "text": "backbone for Sora",
        "ts": "1710000000.000100",
        "event_id": "Ev1",
        "files": [
            {
                "id": "F1",
                "name": "DiT.pdf",
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/files-pri/T-F1/download/DiT.pdf",
            }
        ],
    }
    event.update(overrides)
    return event


async def _drain(channel: SlackChannel) -> None:
    """Let the detached ingest task finish."""
    for _ in range(50):
        if not channel._brain_tasks:
            return
        await asyncio.sleep(0)
    await asyncio.gather(*list(channel._brain_tasks), return_exceptions=True)


def _hook_recording(calls: list[dict[str, Any]]):
    async def _hook(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params)
        return {"status": "ingested", "doc_id": "d", "title": "t"}

    return _hook


@pytest.mark.asyncio
async def test_file_in_watched_channel_ingests_and_never_becomes_a_turn(
    monkeypatch,
) -> None:
    channel = _watched_channel()
    dispatched: list[Any] = []
    channel.on_message(dispatched.append)

    calls: list[dict[str, Any]] = []

    async def _hook(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params)
        return {"status": "ingested", "doc_id": "dit-9f3a2c1b", "title": "DiT"}

    channel.set_brain_ingest_hook(_hook)

    async def _fake_download(files, session_id):
        return (
            [
                {
                    "type": "document",
                    "filename": "DiT.pdf",
                    "path": f"/sessions/{session_id}/uploads/DiT.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 10,
                }
            ],
            [],
        )

    monkeypatch.setattr(channel, "_download_attachments", _fake_download)

    outcome = await channel._route_message_event(_file_event(), {})
    await _drain(channel)

    assert outcome == "ingested:second-brain"
    assert dispatched == [], "ingestion must never reach the message callback"
    assert len(calls) == 1
    assert calls[0]["kind"] == "file"
    assert calls[0]["path"].endswith("DiT.pdf")
    assert calls[0]["slack"]["poster"] == "U1"
    assert calls[0]["slack"]["slack_permalink"].startswith("https://acme.slack.com/")
    assert channel._client.reactions == [
        ("C-BRAIN", "1710000000.000100", "open_book"),
        ("C-BRAIN", "1710000000.000100", "brain"),
    ]
    assert channel._client.removed == [("C-BRAIN", "1710000000.000100", "open_book")]


@pytest.mark.asyncio
async def test_duplicate_event_id_ingests_once(monkeypatch) -> None:
    channel = _watched_channel()
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    await channel._route_message_event(_file_event(), {})
    await _drain(channel)
    await channel._route_message_event(_file_event(), {})
    await _drain(channel)

    assert len(calls) == 1
    assert channel._client.reactions.count(
        ("C-BRAIN", "1710000000.000100", "open_book")
    ) == 1, "a retried event must not put a second reading mark on the message"


@pytest.mark.asyncio
async def test_a_document_already_in_the_corpus_gets_the_brain(monkeypatch) -> None:
    channel = _watched_channel()

    async def _hook(params: dict[str, Any]) -> dict[str, Any]:
        return {"status": "skipped_duplicate", "doc_id": "d", "title": "t"}

    channel.set_brain_ingest_hook(_hook)
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    await channel._route_message_event(_file_event(), {})
    await _drain(channel)

    assert channel._client.reactions == [
        ("C-BRAIN", "1710000000.000100", "open_book"),
        ("C-BRAIN", "1710000000.000100", "brain"),
    ]


@pytest.mark.asyncio
async def test_the_reading_mark_is_on_before_the_document_is_read(monkeypatch) -> None:
    channel = _watched_channel()
    seen_during: list[tuple[str, str, str]] = []

    async def _hook(params: dict[str, Any]) -> dict[str, Any]:
        seen_during.extend(channel._client.reactions)
        return {"status": "ingested", "doc_id": "d", "title": "t"}

    channel.set_brain_ingest_hook(_hook)
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    await channel._route_message_event(_file_event(), {})
    await _drain(channel)

    assert seen_during == [("C-BRAIN", "1710000000.000100", "open_book")]


@pytest.mark.asyncio
async def test_the_reading_mark_comes_off_even_with_no_verdict(monkeypatch) -> None:
    channel = _watched_channel()

    async def _hook(params: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("nothing was downloaded, so nothing should be offered")

    channel.set_brain_ingest_hook(_hook)

    async def _nothing(files, sid):
        return ([], [])

    monkeypatch.setattr(channel, "_download_attachments", _nothing)

    await channel._route_message_event(_file_event(), {})
    await _drain(channel)

    assert channel._client.reactions == [("C-BRAIN", "1710000000.000100", "open_book")]
    assert channel._client.removed == [("C-BRAIN", "1710000000.000100", "open_book")]


async def _noop_download():
    # Keep the downloader's type field for the ordinary turn path.
    return (
        [
            {
                "type": "document",
                "filename": "a.pdf",
                "path": "/x/a.pdf",
                "mime_type": "application/pdf",
            }
        ],
        [],
    )


@pytest.mark.asyncio
async def test_link_only_message_dispatches_kind_link() -> None:
    channel = _watched_channel()
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))

    event = _file_event(
        subtype=None,
        files=[],
        text="worth a read <https://arxiv.test/abs/2212.09748|DiT>",
        event_id="Ev-link",
    )
    event.pop("subtype")
    outcome = await channel._route_message_event(event, {})
    await _drain(channel)

    assert outcome == "ingested:second-brain"
    assert [c["kind"] for c in calls] == ["link"]
    assert calls[0]["url"] == "https://arxiv.test/abs/2212.09748"


@pytest.mark.asyncio
async def test_unwatched_channel_is_left_alone() -> None:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, group_chat_mode="all"),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._client = _FakeClient()
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))
    dispatched: list[Any] = []
    channel.on_message(dispatched.append)

    event = _file_event(channel="C-OTHER")
    outcome = await channel._route_message_event(event, {})
    await _drain(channel)

    assert calls == []
    assert outcome != "ingested:second-brain"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"brain_channels": [], "brain_all": True},
        {"brain_channels": [], "brain_public": True},
    ],
    ids=["listed-channel", "wildcard", "public"],
)
async def test_direct_message_is_never_ingested(settings: dict[str, Any]) -> None:
    channel = _watched_channel(**settings)
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))

    event = _file_event(channel="D-PRIVATE", channel_type="im", event_id="Ev-dm")
    await channel._route_message_event(event, {})
    await _drain(channel)

    assert calls == []


@pytest.mark.asyncio
async def test_no_hook_means_the_ordinary_path_runs() -> None:
    """A deployment with no shared filesystem behaves as it did before."""
    channel = _watched_channel()
    outcome = await channel._route_message_event(_file_event(), {})
    assert outcome != "ingested:second-brain"


@pytest.mark.asyncio
async def test_hook_failure_reacts_with_the_failure_emoji(monkeypatch) -> None:
    channel = _watched_channel()

    async def _hook(params):
        raise RuntimeError("agentserver said no")

    channel.set_brain_ingest_hook(_hook)
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    await channel._route_message_event(_file_event(), {})
    await _drain(channel)

    assert channel._client.reactions == [
        ("C-BRAIN", "1710000000.000100", "open_book"),
        ("C-BRAIN", "1710000000.000100", "warning"),
    ]


@pytest.mark.asyncio
async def test_a_filtered_link_is_neither_a_failure_nor_a_document(
    monkeypatch,
) -> None:
    channel = _watched_channel()

    async def _hook(params):
        return {"status": "skipped_filtered", "reason": "deny-listed host"}

    channel.set_brain_ingest_hook(_hook)

    event = _file_event(files=[], text="https://github.test/x/y/pull/3", event_id="Ev-f")
    event.pop("subtype")
    await channel._route_message_event(event, {})
    await _drain(channel)

    assert channel._client.reactions == [
        ("C-BRAIN", "1710000000.000100", "open_book"),
        ("C-BRAIN", "1710000000.000100", "x"),
    ]


@pytest.mark.asyncio
async def test_plain_message_in_a_watched_channel_is_not_claimed() -> None:
    channel = _watched_channel()
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))

    event = _file_event(files=[], text="thanks!", event_id="Ev-thanks")
    event.pop("subtype")
    outcome = await channel._route_message_event(event, {})
    await _drain(channel)

    assert calls == []
    assert outcome != "ingested:second-brain"


@pytest.mark.asyncio
async def test_mention_still_answered_in_a_watched_channel(monkeypatch) -> None:
    channel = _watched_channel(mode=["mention"])
    dispatched: list[Any] = []
    channel.on_message(dispatched.append)

    async def _hook(params):
        return {"status": "ingested"}

    channel.set_brain_ingest_hook(_hook)

    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C-BRAIN",
        "channel_type": "channel",
        "text": "<@U-BOT> what do we know about diffusion?",
        "ts": "1710000001.000100",
        "event_id": "Ev-mention",
    }
    outcome = await channel._route_message_event(event, {})
    assert outcome != "ingested:second-brain"


@pytest.mark.asyncio
async def test_stop_cancels_outstanding_ingests(monkeypatch) -> None:
    channel = _watched_channel()
    started = asyncio.Event()

    async def _hook(params):
        started.set()
        await asyncio.sleep(60)
        return {"status": "ingested"}

    channel.set_brain_ingest_hook(_hook)
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    await channel._route_message_event(_file_event(), {})
    await asyncio.wait_for(started.wait(), timeout=2)
    tasks = list(channel._brain_tasks)

    await channel.stop()
    await asyncio.gather(*tasks, return_exceptions=True)

    assert all(task.cancelled() for task in tasks)
    assert not channel._brain_tasks


def test_brain_session_id_is_stable_and_path_safe() -> None:
    event = {"team": "T1", "channel": "C-BRAIN"}
    assert brain_session_id(event) == "slack_brain_T1_C-BRAIN"


@pytest.mark.asyncio
async def test_a_renamed_download_is_still_offered_under_its_posted_name(
    monkeypatch,
) -> None:
    channel = _watched_channel()
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))

    async def _renamed_download(files, session_id):
        return (
            [
                {
                    "type": "document",
                    "filename": "DiT-1.pdf",          # what landed on disk
                    "original_filename": "DiT.pdf",   # what was posted
                    "path": f"/sessions/{session_id}/uploads/DiT-1.pdf",
                    "mime_type": "application/pdf",
                }
            ],
            [],
        )

    monkeypatch.setattr(channel, "_download_attachments", _renamed_download)

    await channel._route_message_event(_file_event(), {})
    await _drain(channel)

    assert calls[0]["filename"] == "DiT.pdf"
    assert calls[0]["path"].endswith("DiT-1.pdf"), "the path is still the real one"


# ---- which conversations are ingested from --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("brain_channels", [["C-OTHER"], []], ids=["other", "empty"])
async def test_a_channel_not_on_the_list_is_not_ingested(
    monkeypatch, brain_channels: list[str]
) -> None:
    channel = _watched_channel(brain_channels=brain_channels)
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    outcome = await channel._route_message_event(_file_event(), {})
    await _drain(channel)

    assert outcome != "ingested:second-brain"
    assert calls == []
    assert channel._client.reactions == []


@pytest.mark.asyncio
async def test_the_wildcard_ingests_from_a_channel_it_never_names(monkeypatch) -> None:
    """``channels: ["*"]`` is every conversation, not every *listed* one."""
    channel = _watched_channel(brain_channels=[], brain_all=True)
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    outcome = await channel._route_message_event(
        _file_event(channel="C-NEVER-NAMED", event_id="Ev-w"), {}
    )
    await _drain(channel)

    assert outcome == "ingested:second-brain"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ingesting_does_not_silence_a_channel_that_would_answer(
    monkeypatch,
) -> None:
    channel = _watched_channel(mode=["all"])
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    dispatched: list[Any] = []

    async def _on_message(message):
        dispatched.append(message)

    channel.on_message(_on_message)

    outcome = await channel._route_message_event(_file_event(), {})
    await _drain(channel)

    assert outcome != "ignored:no-trigger-matched"
    assert len(calls) == 1, "the document was not ingested"
    assert dispatched, "the channel stopped answering a message it was set to answer"


@pytest.mark.asyncio
async def test_a_listed_channel_with_no_triggers_at_all_still_ingests(
    monkeypatch,
) -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            group_chat_mode="off",
            brain_ingest_channels=frozenset({"C-BRAIN"}),
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._client = _FakeClient()
    channel._identity.bot_user_id = "U-BOT"

    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    outcome = await channel._route_message_event(_file_event(), {})
    await _drain(channel)

    assert outcome == "ingested:second-brain"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_public_ingests_from_a_public_channel_it_never_names(
    monkeypatch,
) -> None:
    channel = _watched_channel(brain_channels=[], brain_public=True)
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    outcome = await channel._route_message_event(
        _file_event(channel="C-PUBLIC", channel_type="channel", event_id="Ev-p"),
        {},
    )
    await _drain(channel)

    assert outcome == "ingested:second-brain"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "mpim", ""])
async def test_public_leaves_out_private_group_dm_and_unknown(
    chat_type: str,
) -> None:
    """``"public"`` reads the conversation's kind; an unknown kind is not public."""
    channel = _watched_channel(brain_channels=[], brain_public=True)
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))

    await channel._route_message_event(
        _file_event(channel="G-OTHER", channel_type=chat_type, event_id="Ev-np"),
        {},
    )
    await _drain(channel)

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cached_type", "event_type", "should_ingest"),
    [
        ("channel", None, False),
        ("channel", "group", False),
        ("group", "channel", True),
    ],
)
async def test_public_uses_this_event_type_even_when_cache_disagrees(
    monkeypatch,
    cached_type: str,
    event_type: str | None,
    should_ingest: bool,
) -> None:
    channel = _watched_channel(brain_channels=[], brain_public=True)
    channel._chat_types["C-CHANGED"] = cached_type
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )
    event = _file_event(channel="C-CHANGED", event_id="Ev-changed")
    if event_type is None:
        event.pop("channel_type")
    else:
        event["channel_type"] = event_type

    await channel._route_message_event(event, {})
    await _drain(channel)

    assert len(calls) == int(should_ingest)


@pytest.mark.asyncio
async def test_public_adds_to_a_listed_private_channel(monkeypatch) -> None:
    channel = _watched_channel(brain_channels=["G-LISTED"], brain_public=True)
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    outcome = await channel._route_message_event(
        _file_event(channel="G-LISTED", channel_type="group", event_id="Ev-g"), {}
    )
    await _drain(channel)

    assert outcome == "ingested:second-brain"
    assert len(calls) == 1


# ---- reading second_brain.ingest.channels ---------------------------------


def _load_channels(monkeypatch, config):
    from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect

    monkeypatch.setattr(belle_config, "load_belle_config", lambda: config)
    return belle_config.load_brain_ingest_channels()


@pytest.mark.parametrize(
    "channels,expected",
    [
        (None, (False, True, frozenset())),
        ([], (False, False, frozenset())),
        (["C-ONE", " C-TWO ", ""], (False, False, frozenset({"C-ONE", "C-TWO"}))),
        (["*"], (True, False, frozenset())),
        ("C-ONE", (False, False, frozenset({"C-ONE"}))),
        ("*", (True, False, frozenset())),
        (["*", "C-ONE"], (True, False, frozenset())),
        (["public"], (False, True, frozenset())),
        ("public", (False, True, frozenset())),
        (["public", "G-ONE"], (False, True, frozenset({"G-ONE"}))),
        (["*", "public"], (True, False, frozenset())),
        (7, (False, False, frozenset())),
    ],
)
def test_brain_ingest_channel_config(monkeypatch, channels, expected) -> None:
    second_brain = (
        {"enabled": True} if channels is None else {"ingest": {"channels": channels}}
    )
    assert _load_channels(monkeypatch, {"second_brain": second_brain}) == expected


def test_unreadable_config_ingests_from_nowhere(monkeypatch) -> None:
    def _boom(*a, **kw):
        raise RuntimeError("no config")

    monkeypatch.setattr(belle_config, "load_belle_config", _boom)
    assert belle_config.load_brain_ingest_channels() == (False, False, frozenset())


def test_disabled_second_brain_ingests_from_nowhere(monkeypatch) -> None:
    assert _load_channels(monkeypatch, {"second_brain": {"enabled": False}}) == (False, False, frozenset())


@pytest.mark.asyncio
async def test_one_message_asks_slack_for_its_permalink_once(monkeypatch) -> None:
    channel = _watched_channel()
    calls: list[dict[str, Any]] = []
    channel.set_brain_ingest_hook(_hook_recording(calls))

    async def _two_documents(files, session_id):
        return (
            [
                {
                    "type": "document",
                    "filename": f"{name}.pdf",
                    "path": f"/x/{name}.pdf",
                    "mime_type": "application/pdf",
                }
                for name in ("a", "b")
            ],
            [],
        )

    monkeypatch.setattr(channel, "_download_attachments", _two_documents)

    await channel._route_message_event(
        _file_event(text="two of them https://arxiv.test/one https://arxiv.test/two"),
        {},
    )
    await _drain(channel)

    assert [c["kind"] for c in calls] == ["file", "file", "link", "link"]
    assert channel._client.permalinks == 1

    provenance = [c["slack"] for c in calls]
    assert all(p == provenance[0] for p in provenance), provenance
    assert provenance[0]["slack_permalink"].startswith("https://acme.slack.com/")
    # Equal, and not the same object: each item carries its own copy, so
    # nothing downstream can reach back through one and alter the rest.
    assert len({id(p) for p in provenance}) == len(provenance)


@pytest.mark.asyncio
async def test_a_scan_with_no_text_is_declined_rather_than_claimed(
    monkeypatch,
) -> None:
    channel = _watched_channel()

    async def _hook(params):
        return {
            "status": "skipped_filtered",
            "reason": "no readable text could be extracted from scan.pdf",
        }

    channel.set_brain_ingest_hook(_hook)
    monkeypatch.setattr(
        channel, "_download_attachments", lambda files, sid: _noop_download()
    )

    await channel._route_message_event(_file_event(event_id="Ev-scan"), {})
    await _drain(channel)

    assert channel._client.reactions == [
        ("C-BRAIN", "1710000000.000100", "open_book"),
        ("C-BRAIN", "1710000000.000100", "x"),
    ]
    assert channel._client.removed == [("C-BRAIN", "1710000000.000100", "open_book")]


@pytest.mark.asyncio
async def test_one_document_that_landed_outranks_everything_beside_it(
    monkeypatch,
) -> None:
    channel = _watched_channel()
    answers = iter(
        [
            {"status": "skipped_filtered", "reason": "too thin"},
            {"status": "ingested", "doc_id": "d", "title": "t"},
            {"status": "nonsense-status"},
        ]
    )

    async def _hook(params):
        return next(answers)

    channel.set_brain_ingest_hook(_hook)

    await channel._route_message_event(
        _file_event(
            files=[],
            event_id="Ev-mixed",
            text="a https://a.test/one b https://b.test/two c https://c.test/three",
        ),
        {},
    )
    await _drain(channel)

    assert channel._client.reactions[-1] == ("C-BRAIN", "1710000000.000100", "brain")


@pytest.mark.asyncio
async def test_an_error_outranks_a_decline_when_nothing_landed(monkeypatch) -> None:
    """Of the two "no" verdicts, report the one somebody may need to act on."""
    channel = _watched_channel()
    answers = iter(
        [
            {"status": "skipped_filtered", "reason": "deny-listed host"},
            {"status": "nonsense-status"},
        ]
    )

    async def _hook(params):
        return next(answers)

    channel.set_brain_ingest_hook(_hook)

    event = _file_event(
        files=[],
        event_id="Ev-both",
        text="https://a.test/one and https://b.test/two",
    )
    event.pop("subtype")
    await channel._route_message_event(event, {})
    await _drain(channel)

    assert channel._client.reactions[-1] == ("C-BRAIN", "1710000000.000100", "warning")
