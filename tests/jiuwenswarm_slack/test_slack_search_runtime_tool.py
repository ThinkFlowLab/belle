"""Per-request registration tests for the Slack search tool.

Two tools, two decisions. The search tool is mounted on a term the history tool
does not have -- a permission Slack issues with the inbound event -- so a turn
can have either, both or neither, and neither one's absence may decide the
other's. These tests pin that, and pin the case the design turns on: a
scheduled run has no inbound event, so it never gets the tool.
"""

from contextvars import Context
from types import SimpleNamespace
from collections.abc import Callable
from typing import Any

import pytest

from belle_agent.integrations.jiuwenswarm import slack_routing, slack_runtime_tools

from belle_agent.integrations.jiuwenswarm.slack_history import SlackHistoryToolkit
from belle_agent.integrations.jiuwenswarm.slack_search import (
    SLACK_ACTION_TOKEN_KEY,
    SlackSearchToolkit,
)
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


def _card_name(toolkit: Any, func: Callable[..., Any]) -> str:
    """The name a toolkit gives the tool that calls ``func``, read off its card.

    The fakes below stand in for the real toolkits, so the names here are only
    labels -- but a label that is a literal goes stale silently the next time a
    tool is renamed, which is what happened to this file. Reading the name off
    the card that holds it means a rename either arrives here or fails in the
    one place that decides it.

    A toolkit may mount more than one tool -- the history toolkit mounts
    ``download_slack_file`` alongside the conversation reader -- so the one wanted
    is picked by the method it is bound to rather than by being the only one.
    That keeps the tool's name unwritten here: renaming the card still arrives
    through the card, and renaming the method fails at the attribute rather
    than silently labelling the wrong tool.
    """
    (tool,) = (t for t in toolkit.get_tools() if _bound_func(t) == func)
    return str(tool.card.name)


def _bound_func(tool: Any) -> Any:
    """The callable a tool was built around; ``LocalFunction`` keeps it private."""
    return getattr(tool, "func", None) or tool._func


# Neither toolkit contacts Slack to render a card, and the history toolkit binds
# to a conversation from its metadata rather than from a request.
_SEARCH_TOOLKIT = SlackSearchToolkit()
_SEARCH_TOOL = _card_name(_SEARCH_TOOLKIT, _SEARCH_TOOLKIT.search_slack_workspace)
_HISTORY_TOOLKIT = SlackHistoryToolkit(metadata={"slack_channel_id": "C1"})
_HISTORY_TOOL = _card_name(_HISTORY_TOOLKIT, _HISTORY_TOOLKIT.read_slack_conversation)
_TOKEN = "action-token-for-this-turn"






class _FakeToolkit:
    """Stands in for either toolkit; both are built the same way."""

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
        # Only the history toolkit is built with one -- download_slack_file needs a
        # session to write into -- and this fake stands in for both, so it is
        # accepted here and unused. Named rather than swallowed by **kwargs so
        # that the next argument the adapter starts passing arrives as a failure
        # in this file instead of being absorbed silently.
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


class _FakeSearchToolkit(_FakeToolkit):
    tool_name = _SEARCH_TOOL
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
    _FakeSearchToolkit.instances = []
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
                # web defaults to allowed when the key is absent, so a non-Slack
                # request has to say no explicitly.
                "web": {"send_file_allowed": False},
            }
        },
    )
    monkeypatch.setattr(slack_runtime_tools, "SlackSearchToolkit", _FakeSearchToolkit)
    monkeypatch.setattr(slack_runtime_tools, "SlackHistoryToolkit", _FakeHistoryToolkit)
    monkeypatch.setattr(slack_runtime_tools, "SlackCanvasToolkit", _FakeNoopToolkit)
    monkeypatch.setattr(slack_runtime_tools, "SlackListToolkit", _FakeNoopToolkit)
    monkeypatch.setattr(slack_runtime_tools, "load_plugin", fake_belle_plugin_loader(_FakeResourceManager(reject_duplicates=True)))
    # The toggle, read through the module the predicate lives in.
    monkeypatch.setattr(
        slack_runtime_tools,
        "slack_search_request_metadata",
        _predicate(enabled=True),
    )


def _predicate(*, enabled: bool) -> Callable[..., dict[str, Any]]:
    """The real predicate with the config term settled, rather than a stub.

    Importing the module under test's own function keeps the transport and
    token conditions real; only the operator toggle -- which would otherwise
    read the machine's config file -- is fixed.
    """
    from belle_agent.integrations.jiuwenswarm import slack_search

    def predicate(
        channel_id: str | None, metadata: dict[str, Any] | None
    ) -> dict[str, Any]:
        original = slack_search.slack_search_enabled
        slack_search.slack_search_enabled = lambda *a, **k: enabled  # type: ignore[assignment]
        try:
            return slack_search.slack_search_request_metadata(channel_id, metadata)
        finally:
            slack_search.slack_search_enabled = original  # type: ignore[assignment]

    return predicate


def _make_adapter() -> tuple[Any, _FakeAbilityManager]:
    ability_manager = _FakeAbilityManager()
    instance = SimpleNamespace(ability_manager=ability_manager)
    return make_slack_runtime_adapter(instance), ability_manager


def _registered(ability_manager: _FakeAbilityManager) -> set[str]:
    return {getattr(card, "name", "") for card in ability_manager.list()}


def _slack_metadata(**extra: Any) -> dict[str, Any]:
    """A Slack request both gates read, stated in each gate's own vocabulary.

    The history half is the stamped audience decision, named from the module
    that defines it. That is the term the history gate reads, and stamping it is
    what makes "the two tools are decided separately" a claim about two live
    decisions rather than about one tool and one absence: a request that fails
    the history gate for an unrelated reason would satisfy every assertion here
    while proving nothing.
    """
    metadata: dict[str, Any] = {
        "slack_channel_id": "C-ROOM",
        "slack_channel_type": "channel",
        METADATA_ENFORCE_AUDIENCE_KEY: True,
    }
    metadata.update(extra)
    return metadata


# ---------------------------------------------------------------------------
# Present, absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_with_a_token_gets_the_search_tool() -> None:
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata=_slack_metadata(**{SLACK_ACTION_TOKEN_KEY: _TOKEN}),
    )

    assert _SEARCH_TOOL in _registered(ability_manager)


@pytest.mark.asyncio
async def test_a_turn_without_a_token_does_not() -> None:
    adapter, ability_manager = _make_adapter()

    await _update_tools(adapter, channel_id="slack", metadata=_slack_metadata())

    registered = _registered(ability_manager)
    assert _SEARCH_TOOL not in registered
    # And the other tool is unaffected, because the two are decided separately.
    assert _HISTORY_TOOL in registered


@pytest.mark.asyncio
async def test_a_cron_run_never_gets_the_search_tool() -> None:
    """The case the whole design turns on.

    A scheduled run synthesises its metadata from a stored job. It may read the
    conversation's record -- that is what the history tool is registered for
    here -- and it can never search, because there was no inbound Slack event
    to issue the permission a search needs. Being unavailable is the answer;
    quietly answering from the record instead is not.
    """
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

    registered = _registered(ability_manager)
    assert _HISTORY_TOOL in registered
    assert _SEARCH_TOOL not in registered


@pytest.mark.asyncio
async def test_a_cron_run_carrying_a_token_still_gets_nothing() -> None:
    """Defence in depth: the transport decides, not only the key's presence.

    Nothing writes a token onto a cron run today. If something ever did -- a
    stray copy of an inbound turn's metadata, say -- the run still arrives on
    the cron transport, and that alone is disqualifying.
    """
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id=slack_routing.CRON_CHANNEL_ID,
        metadata=_slack_metadata(
            **{
                SLACK_ACTION_TOKEN_KEY: _TOKEN,
                slack_routing.SLACK_HISTORY_ORIGIN_KEY: (
                    slack_routing.SLACK_HISTORY_ORIGIN_CRON_JOB
                ),
            }
        ),
    )

    assert _SEARCH_TOOL not in _registered(ability_manager)


@pytest.mark.asyncio
async def test_the_toggle_off_keeps_the_tool_unregistered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        slack_runtime_tools, "slack_search_request_metadata", _predicate(enabled=False)
    )
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata=_slack_metadata(**{SLACK_ACTION_TOKEN_KEY: _TOKEN}),
    )

    assert _SEARCH_TOOL not in _registered(ability_manager)


@pytest.mark.asyncio
async def test_a_non_slack_turn_gets_neither_tool() -> None:
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id="web",
        metadata=_slack_metadata(**{SLACK_ACTION_TOKEN_KEY: _TOKEN}),
    )

    assert _registered(ability_manager) == set()


# ---------------------------------------------------------------------------
# Independence, and stability across requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_is_registered_even_when_history_is_refused() -> None:
    """Neither tool's gate may decide the other's.

    History needs a stamped audience decision; search does not, because it reads
    no conversation's record. A turn refused history must still be offered
    search, or one operator setting would silently govern both.

    The absent key is now the only way the history gate refuses a Slack request
    whose conversation is named, so it is what the refusal is spelled as here. A
    stamped ``false`` would not do: that mounts the history pair, and the
    assertion below would fail rather than pass for the wrong reason.
    """
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata={
            "slack_channel_id": "C-ROOM",
            "slack_channel_type": "channel",
            SLACK_ACTION_TOKEN_KEY: _TOKEN,
        },
    )

    registered = _registered(ability_manager)
    assert _SEARCH_TOOL in registered
    assert _HISTORY_TOOL not in registered


@pytest.mark.asyncio
async def test_the_toolkit_is_built_once_and_reads_the_live_request() -> None:
    """Built once, then asked again per request through its provider.

    The card stays registered across concurrent transports -- taking it away
    because *this* request cannot search would take it from a simultaneous one
    that can -- so what has to be per-request is the provider, not the
    registration.
    """
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata=_slack_metadata(**{SLACK_ACTION_TOKEN_KEY: _TOKEN}),
    )
    await _update_tools(
        adapter,
        channel_id="slack",
        metadata=_slack_metadata(**{SLACK_ACTION_TOKEN_KEY: "a-second-token"}),
    )

    assert len(_FakeSearchToolkit.instances) == 1
    provider = _FakeSearchToolkit.instances[0].metadata_provider
    assert provider is not None

    # Read the provider under a request that holds a different token, and
    # again under one that holds none -- the second is a cron run reaching a
    # process that already registered the tool.
    def _read(channel_id: str, metadata: dict[str, Any] | None) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            interface_module._CRON_TOOL_CHANNEL_ID.set(channel_id)
            interface_module._CRON_TOOL_METADATA.set(metadata)
            interface_module._CRON_TOOL_BOUND.set(True)
            adapter._runtime_cron_tool_context.remember_current_binding()
            return provider()

        return Context().run(run)

    live = _read("slack", _slack_metadata(**{SLACK_ACTION_TOKEN_KEY: "third-token"}))
    assert live[SLACK_ACTION_TOKEN_KEY] == "third-token"

    assert (
        _read(
            slack_routing.CRON_CHANNEL_ID,
            _slack_metadata(
                **{
                    slack_routing.SLACK_HISTORY_ORIGIN_KEY: (
                        slack_routing.SLACK_HISTORY_ORIGIN_CRON_JOB
                    )
                }
            ),
        )
        == {}
    )


@pytest.mark.asyncio
async def test_the_card_is_restored_if_something_removed_it() -> None:
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata=_slack_metadata(**{SLACK_ACTION_TOKEN_KEY: _TOKEN}),
    )
    ability_manager.remove(_SEARCH_TOOL)
    assert _SEARCH_TOOL not in _registered(ability_manager)

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata=_slack_metadata(**{SLACK_ACTION_TOKEN_KEY: _TOKEN}),
    )

    assert _SEARCH_TOOL in _registered(ability_manager)
    # Restored as a card, not as a second tool: re-adding the tool to the
    # resource manager would raise on the duplicate id.
    assert len(_FakeSearchToolkit.instances) == 1
