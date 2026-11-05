"""Per-request registration tests for the Slack history tool."""

import asyncio
from contextvars import Context
from types import SimpleNamespace
from collections.abc import Callable
from typing import Any

import pytest

from belle_agent.integrations.jiuwenswarm import slack_runtime_tools

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
from slack_runtime_test_helpers import (
    SlackRuntimeAbilityManager as _FakeAbilityManager,
    SlackRuntimeResourceManager as _FakeResourceManager,
    fake_belle_plugin_loader,
    make_slack_runtime_adapter,
    update_slack_runtime_tools as _update_tools,
)


# Both tools the history toolkit mounts. They share one availability
# predicate, one toolkit instance and one registration block, so "the Slack
# history tool is mounted" is a statement about the pair.
_TOOL_NAMES = ("read_slack_conversation", "download_slack_file")






class _FakeSlackHistoryToolkit:
    instances: list["_FakeSlackHistoryToolkit"] = []

    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        metadata_provider: Callable[[], dict[str, Any] | None] | None = None,
        session_id: str | None = None,
        session_id_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.metadata = dict(metadata or {})
        self.metadata_provider = metadata_provider
        self.session_id = session_id
        self.session_id_provider = session_id_provider
        self.updates: list[dict[str, Any]] = []
        self.tools = []
        for tool_name in _TOOL_NAMES:
            tool_id = f"{tool_name}-{len(self.instances)}"
            self.tools.append(
                SimpleNamespace(card=SimpleNamespace(id=tool_id, name=tool_name))
            )
        self.instances.append(self)

    def update_runtime_context(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        self.metadata = dict(metadata or {})
        self.session_id = session_id
        self.updates.append(self.metadata)

    def get_tools(self) -> list[Any]:
        return list(self.tools)


class _FakeSlackNoopToolkit:
    """Stands in for the four toolkits this file has no opinion about.

    ``_update_session_tools`` refreshes every Slack toolkit on each call, each
    on its own predicate, so the real ``SlackReactionToolkit``/
    ``SlackPinToolkit``/``SlackBookmarkToolkit``/``SlackDirectoryToolkit``/
    ``SlackHomeTabToolkit`` would otherwise register ``react_to_message``/
    ``pin_message``/``write_bookmark``/``read_bookmarks``/``find_by_name``/
    ``publish_slack_home_tab``/``clear_slack_home_tab`` alongside the history pair every
    time this file's metadata names a Slack conversation -- which is most of
    these tests, since that is exactly what the history gate also requires. This file's assertions are
    about the history pair only, so the other toolkits are faked out here the
    way ``SlackHistoryToolkit`` itself is, rather than left real: mounting zero
    tools keeps them out of the way without touching the gate that decides
    whether *they* run.
    """

    def __init__(self, *, metadata_provider: Any = None, **_: Any) -> None:
        self.metadata_provider = metadata_provider

    def get_tools(self) -> list[Any]:
        return []


def _make_adapter() -> tuple[Any, _FakeAbilityManager]:
    ability_manager = _FakeAbilityManager()
    instance = SimpleNamespace(ability_manager=ability_manager)
    return make_slack_runtime_adapter(instance), ability_manager


async def _read_provider(
    toolkit: _FakeSlackHistoryToolkit,
    *,
    channel_id: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    channel_token = interface_module._CRON_TOOL_CHANNEL_ID.set(channel_id)
    metadata_token = interface_module._CRON_TOOL_METADATA.set(metadata)
    bound_token = interface_module._CRON_TOOL_BOUND.set(True)
    try:
        await asyncio.sleep(0)
        assert toolkit.metadata_provider is not None
        return toolkit.metadata_provider()
    finally:
        interface_module._CRON_TOOL_BOUND.reset(bound_token)
        interface_module._CRON_TOOL_METADATA.reset(metadata_token)
        interface_module._CRON_TOOL_CHANNEL_ID.reset(channel_token)


@pytest.fixture(autouse=True)
def _patch_runtime(monkeypatch: pytest.MonkeyPatch) -> _FakeResourceManager:
    _FakeSlackHistoryToolkit.instances.clear()
    resource_manager = _FakeResourceManager(reject_duplicates=True)
    monkeypatch.setattr(
        slack_runtime_tools, "SlackHistoryToolkit", _FakeSlackHistoryToolkit
    )
    # Reaction, pin, bookmarks and the name lookup are refreshed on every
    # call alongside history (see _FakeSlackNoopToolkit above); faked out so
    # they cannot add cards this file's set-equality assertions do not expect.
    monkeypatch.setattr(
        slack_runtime_tools, "SlackReactionToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(slack_runtime_tools, "SlackPinToolkit", _FakeSlackNoopToolkit)
    monkeypatch.setattr(
        slack_runtime_tools, "SlackBookmarkToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(
        slack_runtime_tools, "SlackDirectoryToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(
        slack_runtime_tools, "SlackHomeTabToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(
        slack_runtime_tools, "SlackCanvasToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(
        slack_runtime_tools, "SlackListToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(slack_runtime_tools, "load_plugin", fake_belle_plugin_loader(resource_manager))
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {
            "channels": {
                "slack": {"send_file_allowed": False},
                "web": {"send_file_allowed": False},
            }
        },
    )
    return resource_manager


@pytest.mark.asyncio
async def test_slack_history_tool_uses_context_local_metadata_for_every_request() -> (
    None
):
    adapter, abilities = _make_adapter()

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata={
            "slack_channel_id": "C-ONE",
            "slack_user_id": "U-ONE",
            "slack_enforce_audience": True,
        },
    )

    assert {card.name for card in abilities.list()} == set(_TOOL_NAMES)
    assert len(_FakeSlackHistoryToolkit.instances) == 1
    toolkit = _FakeSlackHistoryToolkit.instances[0]
    assert toolkit.metadata == {}
    assert toolkit.updates == []

    first, second, non_slack = await asyncio.gather(
        _read_provider(
            toolkit,
            channel_id="slack",
            metadata={
                "slack_channel_id": "C-ONE",
                "slack_user_id": "U-ONE",
                "slack_enforce_audience": True,
            },
        ),
        _read_provider(
            toolkit,
            channel_id="slack",
            metadata={
                "slack_channel_id": "C-TWO",
                "slack_user_id": "U-TWO",
                "slack_enforce_audience": True,
            },
        ),
        _read_provider(
            toolkit,
            channel_id="web",
            metadata={"slack_channel_id": "C-HIDDEN"},
        ),
    )

    assert first == {
        "slack_channel_id": "C-ONE",
        "slack_user_id": "U-ONE",
        "slack_enforce_audience": True,
    }
    assert second == {
        "slack_channel_id": "C-TWO",
        "slack_user_id": "U-TWO",
        "slack_enforce_audience": True,
    }
    assert non_slack == {}


@pytest.mark.asyncio
async def test_slack_history_tool_keeps_trusted_metadata_across_worker_boundary() -> (
    None
):
    adapter, _abilities = _make_adapter()
    metadata = {
        "slack_channel_id": "C-ONE",
        "slack_channel_type": "channel",
        "slack_user_id": "U-ONE",
        "slack_enforce_audience": True,
    }

    await _update_tools(adapter, channel_id="slack", metadata=metadata)

    toolkit = _FakeSlackHistoryToolkit.instances[0]
    assert toolkit.metadata_provider is not None

    async def _invoke_from_worker() -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return toolkit.metadata_provider()

    provided = await Context().run(asyncio.create_task, _invoke_from_worker())

    assert provided == metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_id", "metadata"),
    [
        ("web", {"slack_channel_id": "C-ONE"}),
        ("slack", {"slack_channel_id": "C-NOT-ALLOWED"}),
        ("slack", {}),
        ("slack", None),
    ],
)
async def test_slack_history_tool_is_not_registered_outside_current_slack_channel(
    channel_id: str,
    metadata: dict[str, Any] | None,
) -> None:
    adapter, abilities = _make_adapter()

    await _update_tools(adapter, channel_id=channel_id, metadata=metadata)

    assert abilities.list() == []
    assert _FakeSlackHistoryToolkit.instances == []


@pytest.mark.asyncio
async def test_registered_slack_history_tool_stays_stable_across_transports() -> None:
    adapter, abilities = _make_adapter()
    abilities.add(SimpleNamespace(id="unrelated-tool", name="unrelated_tool"))

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata={
            "slack_channel_id": "C-ONE",
            "slack_enforce_audience": True,
        },
    )
    await _update_tools(
        adapter,
        channel_id="web",
        metadata={"slack_channel_id": "C-ONE"},
    )
    assert {card.name for card in abilities.list()} == {"unrelated_tool"} | set(_TOOL_NAMES)
    toolkit = _FakeSlackHistoryToolkit.instances[0]
    assert toolkit.metadata_provider is not None
    assert toolkit.metadata_provider() == {}

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata={
            "slack_channel_id": "C-THREE",
            "slack_enforce_audience": True,
        },
    )

    assert {card.name for card in abilities.list()} == {"unrelated_tool"} | set(_TOOL_NAMES)
    assert len(_FakeSlackHistoryToolkit.instances) == 1
    assert _FakeSlackHistoryToolkit.instances[0].updates == []
    assert toolkit.metadata_provider() == {
        "slack_channel_id": "C-THREE",
        "slack_enforce_audience": True,
    }


@pytest.mark.asyncio
async def test_slack_history_tool_is_isolated_between_session_adapters(
    _patch_runtime: _FakeResourceManager,
) -> None:
    first_adapter, first_abilities = _make_adapter()
    second_adapter, second_abilities = _make_adapter()

    await _update_tools(
        first_adapter,
        channel_id="slack",
        metadata={
            "slack_channel_id": "C-ONE",
            "slack_enforce_audience": True,
        },
    )
    await _update_tools(
        second_adapter,
        channel_id="slack",
        metadata={
            "slack_channel_id": "C-TWO",
            "slack_enforce_audience": True,
        },
    )

    # Two adapters, each mounting the toolkit's whole pair, and no sharing.
    assert len(_patch_runtime.tools) == 2 * len(_TOOL_NAMES)
    assert {card.name for card in first_abilities.list()} == set(_TOOL_NAMES)
    assert {card.name for card in second_abilities.list()} == set(_TOOL_NAMES)

    await _update_tools(
        first_adapter,
        channel_id="web",
        metadata={"slack_channel_id": "C-ONE"},
    )

    assert {card.name for card in first_abilities.list()} == set(_TOOL_NAMES)
    assert {card.name for card in second_abilities.list()} == set(_TOOL_NAMES)
    assert _FakeSlackHistoryToolkit.instances[0].updates == []
    assert _FakeSlackHistoryToolkit.instances[1].updates == []
    first_provider = _FakeSlackHistoryToolkit.instances[0].metadata_provider
    second_provider = _FakeSlackHistoryToolkit.instances[1].metadata_provider
    assert first_provider is not None
    assert second_provider is not None
    assert first_provider() == {}
    assert second_provider() == {
        "slack_channel_id": "C-TWO",
        "slack_enforce_audience": True,
    }


# ── Cron turns ───────────────────────────────────────────────────────────────
#
# A cron run arrives on "__cron__" and has no Slack channel of its own. The
# scheduler stamps the conversation the job was created in, and marks the stamp
# as its own doing; the runtime honours that mark and nothing else, so the model
# still cannot say which channel is read and a non-Slack cron job gains nothing.

_CRON_CHANNEL_ID = "__cron__"


def _cron_metadata(**overrides: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "cron": {"job_id": "job-1", "run_id": "job-1:1"},
        "slack_channel_id": "C-ONE",
        "slack_channel_type": "channel",
        "slack_enforce_audience": True,
        "slack_history_origin": "cron_job",
    }
    metadata.update(overrides)
    return metadata


@pytest.mark.asyncio
async def test_slack_history_tool_mounts_for_a_slack_originated_cron_run() -> None:
    adapter, abilities = _make_adapter()
    metadata = _cron_metadata(slack_thread_ts="1712345678.000100")

    await _update_tools(adapter, channel_id=_CRON_CHANNEL_ID, metadata=metadata)

    assert {card.name for card in abilities.list()} == set(_TOOL_NAMES)
    toolkit = _FakeSlackHistoryToolkit.instances[0]
    assert toolkit.metadata_provider is not None
    provided = await _read_provider(
        toolkit, channel_id=_CRON_CHANNEL_ID, metadata=metadata
    )
    assert provided is not None
    assert provided["slack_channel_id"] == "C-ONE"
    assert provided["slack_thread_ts"] == "1712345678.000100"


@pytest.mark.asyncio
async def test_slack_history_tool_mounts_for_an_unenforced_deployment() -> None:
    """``false`` is no audience bound, not no Slack tools.

    The gate asks whether a decision was taken, not what it was. Withholding the
    pair for ``false`` would answer a question the operator did not ask, and the
    key is passed through so the toolkit reads the same answer at call time.
    """
    adapter, abilities = _make_adapter()
    metadata = _cron_metadata(slack_enforce_audience=False)

    await _update_tools(adapter, channel_id=_CRON_CHANNEL_ID, metadata=metadata)

    assert {card.name for card in abilities.list()} == set(_TOOL_NAMES)
    toolkit = _FakeSlackHistoryToolkit.instances[0]
    assert toolkit.metadata_provider is not None
    provided = await _read_provider(
        toolkit, channel_id=_CRON_CHANNEL_ID, metadata=metadata
    )
    assert provided is not None
    assert provided["slack_enforce_audience"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (
            # Nobody holding the configuration settled this run. Mounting
            # nothing is the only safe reading of an absent key, and it is a
            # different state from a deployment that settled the key to false.
            {
                "cron": {"job_id": "job-1", "run_id": "job-1:1"},
                "slack_channel_id": "C-ONE",
                "slack_channel_type": "channel",
                "slack_history_origin": "cron_job",
            },
            "no audience decision was stamped on this run",
        ),
        (
            # The security case: a cron job whose Slack-looking session id was
            # never proven has no origin mark, because the scheduler never
            # stamped one. Slack-shaped metadata that the scheduler did not put
            # there is not a Slack context.
            {
                "cron": {"job_id": "job-1", "run_id": "job-1:1"},
                "slack_channel_id": "C-PRIVATE",
                "slack_channel_type": "channel",
                "slack_enforce_audience": True,
            },
            "unproven Slack session leaves no origin mark",
        ),
        (
            {"cron": {"job_id": "job-1", "run_id": "job-1:1"}},
            "non-Slack cron job",
        ),
        (None, "no metadata at all"),
    ],
    ids=["audience-unsettled", "unproven-session", "non-slack-cron", "no-metadata"],
)
async def test_slack_history_tool_does_not_mount_for_other_cron_runs(
    metadata: dict[str, Any] | None,
    reason: str,
) -> None:
    adapter, abilities = _make_adapter()

    await _update_tools(adapter, channel_id=_CRON_CHANNEL_ID, metadata=metadata)

    assert abilities.list() == [], reason
    assert _FakeSlackHistoryToolkit.instances == [], reason


@pytest.mark.asyncio
async def test_cron_origin_mark_does_not_travel_to_other_channels() -> None:
    """The mark answers "which cron run is this", not "trust this metadata"."""
    adapter, abilities = _make_adapter()

    await _update_tools(adapter, channel_id="web", metadata=_cron_metadata())

    assert abilities.list() == []
    assert _FakeSlackHistoryToolkit.instances == []
