"""Per-request registration tests for ``find_by_name``.

The sixth Slack decision, and the widest. Turning a name into an id reads no
conversation, writes nothing and holds no per-event token, so the only thing it
needs is a request that came from Slack at all. These tests pin the three
consequences that follow: a turn refused the history tool still gets this one,
a scheduled run gets it where the search tool is refused one, and a request on
any other transport gets nothing whatever its metadata claims.
"""

from types import SimpleNamespace
from collections.abc import Callable
from typing import Any

import pytest

from belle_agent.integrations.jiuwenswarm import slack_routing, slack_runtime_tools

from belle_agent.integrations.jiuwenswarm.slack_directory import (
    SlackDirectoryToolkit,
)
from belle_agent.integrations.jiuwenswarm.slack_history import SlackHistoryToolkit
from belle_agent.platforms.slack.history_policy import (
    METADATA_ENFORCE_AUDIENCE_KEY,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
from slack_runtime_test_helpers import (
    SlackRuntimeAbilityManager as _FakeAbilityManager,
    SlackRuntimeResourceManager as _FakeResourceManager,
    fake_belle_plugin_loader,
    make_slack_runtime_adapter,
    update_slack_runtime_tools as _update_tools,
)


def _bound_func(tool: Any) -> Any:
    """The callable a tool was built around; ``LocalFunction`` keeps it private."""
    return getattr(tool, "func", None) or tool._func


def _card_name(toolkit: Any, func: Callable[..., Any]) -> str:
    """The name a toolkit gives one of its tools, read off the card that holds it.

    Never written as a literal here, for the reason the search tool's tests
    give: a renamed card must arrive through the card rather than leaving a
    stale string asserting nothing.
    """
    (tool,) = (
        candidate
        for candidate in toolkit.get_tools()
        if _bound_func(candidate) == func
    )
    return str(tool.card.name)


_FIND_TOOLKIT = SlackDirectoryToolkit()
_FIND_TOOL = _card_name(_FIND_TOOLKIT, _FIND_TOOLKIT.find_by_name)
_HISTORY_TOOLKIT = SlackHistoryToolkit(metadata={"slack_channel_id": "C1"})
_HISTORY_TOOL = _card_name(_HISTORY_TOOLKIT, _HISTORY_TOOLKIT.read_slack_conversation)






class _FakeToolkit:
    """Stands in for a real toolkit; both of the two here are built alike."""

    tool_name = ""
    instances: list["_FakeToolkit"] = []

    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        metadata_provider: Callable[[], dict[str, Any] | None] | None = None,
        session_id_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.metadata = dict(metadata or {})
        self.metadata_provider = metadata_provider
        self.session_id_provider = session_id_provider
        tool_id = f"{self.tool_name}-{len(type(self).instances)}"
        self.tools = [
            SimpleNamespace(card=SimpleNamespace(id=tool_id, name=self.tool_name))
        ]
        type(self).instances.append(self)

    def update_runtime_context(self, *, metadata: dict[str, Any] | None = None) -> None:
        self.metadata = dict(metadata or {})

    def get_tools(self) -> list[Any]:
        return list(self.tools)


class _FakeDirectoryToolkit(_FakeToolkit):
    tool_name = _FIND_TOOL
    instances: list["_FakeToolkit"] = []


class _FakeHistoryToolkit(_FakeToolkit):
    tool_name = _HISTORY_TOOL
    instances: list["_FakeToolkit"] = []


class _FakeNoopToolkit:
    """Stands in for the canvas toolkit, which this file has no opinion about.

    It is refreshed on every call, on a predicate of its own -- a request that
    came from Slack, with no conversation and no requester required -- so on a
    default it would register its six cards beside the one this file's
    set-equality assertions expect. Mounting zero tools keeps it out of the way
    without touching the gate that decides whether it runs.
    """

    def __init__(self, **_: Any) -> None:
        pass

    def get_tools(self) -> list[Any]:
        return []



@pytest.fixture(autouse=True)
def _toolkits(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeDirectoryToolkit.instances = []
    _FakeHistoryToolkit.instances = []
    # send_file is on for Slack in the shipped config and would build a second
    # toolkit on the way past, pulling in half the adapter for a decision this
    # file is not about.
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
    monkeypatch.setattr(
        slack_runtime_tools, "SlackDirectoryToolkit", _FakeDirectoryToolkit
    )
    monkeypatch.setattr(slack_runtime_tools, "SlackHistoryToolkit", _FakeHistoryToolkit)
    monkeypatch.setattr(slack_runtime_tools, "SlackCanvasToolkit", _FakeNoopToolkit)
    monkeypatch.setattr(slack_runtime_tools, "SlackListToolkit", _FakeNoopToolkit)
    monkeypatch.setattr(slack_runtime_tools, "load_plugin", fake_belle_plugin_loader(_FakeResourceManager(reject_duplicates=True)))


def _make_adapter() -> tuple[Any, _FakeAbilityManager]:
    ability_manager = _FakeAbilityManager()
    instance = SimpleNamespace(ability_manager=ability_manager)
    return make_slack_runtime_adapter(instance), ability_manager




def _registered(ability_manager: _FakeAbilityManager) -> set[str]:
    return {getattr(card, "name", "") for card in ability_manager.list()}


def _slack_metadata(**extra: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "slack_channel_id": "C-ROOM",
        "slack_channel_type": "channel",
        "slack_team_id": "T-ACME",
        METADATA_ENFORCE_AUDIENCE_KEY: True,
    }
    metadata.update(extra)
    return metadata


@pytest.mark.asyncio
async def test_an_inbound_slack_turn_gets_the_lookup() -> None:
    adapter, ability_manager = _make_adapter()

    await _update_tools(adapter, channel_id="slack", metadata=_slack_metadata())

    assert _FIND_TOOL in _registered(ability_manager)


@pytest.mark.asyncio
async def test_a_turn_refused_history_still_gets_the_lookup() -> None:
    """The decisions are separate, and this is the one that proves it.

    A request carrying no audience decision loses the history pair, because
    reading a conversation's record out needs a bound somebody settled. A name
    and an id are not a record, so the lookup stands.
    """
    adapter, ability_manager = _make_adapter()
    metadata = _slack_metadata()
    metadata.pop(METADATA_ENFORCE_AUDIENCE_KEY)

    await _update_tools(adapter, channel_id="slack", metadata=metadata)

    registered = _registered(ability_manager)
    assert _FIND_TOOL in registered
    assert _HISTORY_TOOL not in registered


@pytest.mark.asyncio
async def test_a_slack_originated_cron_run_gets_the_lookup() -> None:
    """Unlike search, which needs a token only an inbound event carries."""
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id=slack_routing.CRON_CHANNEL_ID,
        metadata=_slack_metadata(
            **{
                slack_routing.SLACK_HISTORY_ORIGIN_KEY: (
                    slack_routing.SLACK_HISTORY_ORIGIN_CRON_JOB
                )
            }
        ),
    )

    assert _FIND_TOOL in _registered(ability_manager)


@pytest.mark.asyncio
async def test_a_cron_run_the_scheduler_did_not_mark_gets_nothing() -> None:
    """A stray Slack-looking key on a cron request grants nothing."""
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id=slack_routing.CRON_CHANNEL_ID,
        metadata=_slack_metadata(),
    )

    assert _FIND_TOOL not in _registered(ability_manager)


@pytest.mark.asyncio
async def test_a_web_turn_gets_nothing_however_its_metadata_reads() -> None:
    """The transport decides. A web request carrying Slack keys is still web."""
    adapter, ability_manager = _make_adapter()

    await _update_tools(adapter, channel_id="web", metadata=_slack_metadata())

    assert _FIND_TOOL not in _registered(ability_manager)


@pytest.mark.asyncio
async def test_the_toolkit_is_built_once_and_reads_the_live_request() -> None:
    """One toolkit for the life of the process, answering per request.

    The card is shared across concurrent transports, so it is registered once
    and never withdrawn; what keeps a later non-Slack request from using it is
    the provider, which is read at invocation time rather than captured.
    """
    adapter, _ability_manager = _make_adapter()

    await _update_tools(adapter, channel_id="slack", metadata=_slack_metadata())
    await _update_tools(
        adapter,
        channel_id="slack",
        metadata=_slack_metadata(slack_channel_id="C-OTHER"),
    )

    assert len(_FakeDirectoryToolkit.instances) == 1
    provider = _FakeDirectoryToolkit.instances[0].metadata_provider
    assert callable(provider)
    assert provider()["slack_channel_id"] == "C-OTHER"


@pytest.mark.asyncio
async def test_the_provider_fails_closed_once_the_request_is_not_a_slack_one() -> None:
    adapter, _ability_manager = _make_adapter()

    await _update_tools(adapter, channel_id="slack", metadata=_slack_metadata())
    provider = _FakeDirectoryToolkit.instances[0].metadata_provider
    await _update_tools(adapter, channel_id="web", metadata=_slack_metadata())

    assert provider() == {}


@pytest.mark.asyncio
async def test_reaction_provider_uses_the_same_request_origin_gate() -> None:
    adapter, _ability_manager = _make_adapter()
    metadata = _slack_metadata()

    await _update_tools(adapter, channel_id="web", metadata=metadata)
    assert adapter._slack_manager._get_slack_reaction_request_metadata() == {}

    await _update_tools(adapter, channel_id="slack", metadata=metadata)
    assert adapter._slack_manager._get_slack_reaction_request_metadata() == metadata

    await _update_tools(
        adapter,
        channel_id=slack_routing.CRON_CHANNEL_ID,
        metadata=metadata,
    )
    assert adapter._slack_manager._get_slack_reaction_request_metadata() == {}


@pytest.mark.asyncio
async def test_the_card_is_restored_if_something_removed_it() -> None:
    adapter, ability_manager = _make_adapter()

    await _update_tools(adapter, channel_id="slack", metadata=_slack_metadata())
    ability_manager.remove(_FIND_TOOL)
    await _update_tools(adapter, channel_id="slack", metadata=_slack_metadata())

    assert _FIND_TOOL in _registered(ability_manager)
    assert len(_FakeDirectoryToolkit.instances) == 1
