"""Per-request registration tests for the two Slack Home tab tools.

The eighth Slack decision, and the only one whose surface is not a conversation.
The seven before it act in the conversation the gateway named and are mounted on
that; a Home tab belongs to one person, so what these need is that the request
names the person whose turn it is.

Two things are pinned here and nowhere else:

* a request that names nobody -- a scheduled run, a turn from another transport
  -- mounts neither tool;
* the decision is separate from the seven in both directions, so a request
  carrying no audience decision still gets these, and a request carrying no
  conversation at all still gets these.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from belle_agent.integrations.jiuwenswarm import slack_runtime_tools

from belle_agent.platforms.slack.history_policy import (
    METADATA_ASKER_KEY,
    METADATA_ENFORCE_AUDIENCE_KEY,
)
from belle_agent.integrations.jiuwenswarm.slack_tool_context import METADATA_ORIGIN_KEY, ORIGIN_CRON_JOB
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
from slack_runtime_test_helpers import (
    SlackRuntimeAbilityManager as _FakeAbilityManager,
    SlackRuntimeResourceManager as _FakeResourceManager,
    fake_belle_plugin_loader,
    make_slack_runtime_adapter,
    update_slack_runtime_tools as _update_tools,
)

_TOOL_NAMES = {"publish_slack_home_tab", "clear_slack_home_tab"}






class _FakeHomeTabToolkit:
    """Stands in for ``SlackHomeTabToolkit``: two cards and no Slack call.

    The toolkit's own refusals are tested against the real class in
    ``test_slack_home_tab.py``. What is under test here is the gate above it,
    so nothing but the card names matters.
    """

    instances: list["_FakeHomeTabToolkit"] = []

    def __init__(self, *, metadata_provider: Any = None, **_: Any) -> None:
        self.metadata_provider = metadata_provider
        type(self).instances.append(self)

    def get_tools(self) -> list[Any]:
        return [
            SimpleNamespace(card=SimpleNamespace(id=f"{name}-1", name=name))
            for name in sorted(_TOOL_NAMES)
        ]


class _FakeEmptyToolkit:
    """Every other Slack toolkit, mounting nothing so it stays out of the way."""

    def __init__(self, **_: Any) -> None:
        pass

    def update_runtime_context(self, **_: Any) -> None:
        pass

    def get_tools(self) -> list[Any]:
        return []


class _FakeInstance:
    def __init__(self) -> None:
        self.ability_manager = _FakeAbilityManager()
        self.rails: list[Any] = []

    async def register_rail(self, rail: Any) -> None:
        self.rails.append(rail)


@pytest.fixture(autouse=True)
def _toolkits(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeHomeTabToolkit.instances = []
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
        slack_runtime_tools, "SlackHomeTabToolkit", _FakeHomeTabToolkit
    )
    for name in (
        "SlackHistoryToolkit",
        "SlackSearchToolkit",
        "SlackReactionToolkit",
        "SlackPinToolkit",
        "SlackBookmarkToolkit",
        "SlackDirectoryToolkit",
        "SlackPostToolkit",
        "SlackCanvasToolkit",
        "SlackListToolkit",
    ):
        monkeypatch.setattr(slack_runtime_tools, name, _FakeEmptyToolkit)
    monkeypatch.setattr(slack_runtime_tools, "load_plugin", fake_belle_plugin_loader(_FakeResourceManager()))


def _make_adapter() -> tuple[Any, _FakeInstance]:
    instance = _FakeInstance()
    return make_slack_runtime_adapter(instance), instance




def _registered(instance: _FakeInstance) -> set[str]:
    return {getattr(card, "name", "") for card in instance.ability_manager.list()}


def _metadata(**extra: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "slack_channel_id": "C0ROOM",
        "slack_channel_type": "channel",
        "slack_team_id": "T0ACME",
        METADATA_ASKER_KEY: "U0ASKER",
    }
    metadata.update(extra)
    return metadata


@pytest.mark.asyncio
async def test_a_turn_that_names_who_asked_gets_both_tools() -> None:
    adapter, instance = _make_adapter()

    await _update_tools(adapter, metadata=_metadata())

    assert _registered(instance) == _TOOL_NAMES


@pytest.mark.asyncio
async def test_a_turn_naming_nobody_gets_neither() -> None:
    """The requester is the destination, so a request without one has no tab."""
    adapter, instance = _make_adapter()
    metadata = _metadata()
    metadata.pop(METADATA_ASKER_KEY)

    await _update_tools(adapter, metadata=metadata)

    assert _registered(instance) == set()


@pytest.mark.asyncio
async def test_a_scheduled_run_gets_neither() -> None:
    """A job is started by a clock, so there is nobody whose tab it could be."""
    adapter, instance = _make_adapter()

    await _update_tools(
        adapter,
        channel_id="__cron__",
        metadata=_metadata(**{METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB}),
    )

    assert _registered(instance) == set()


@pytest.mark.asyncio
async def test_a_turn_from_another_transport_gets_neither() -> None:
    adapter, instance = _make_adapter()

    await _update_tools(adapter, channel_id="web", metadata=_metadata())

    assert _registered(instance) == set()


@pytest.mark.asyncio
async def test_the_audience_decision_decides_nothing_here() -> None:
    """The audience rule governs reach, and these tools name no target.

    A request carrying no ``slack_enforce_audience`` still gets both of these,
    where the same request is refused the history pair and the posting trio.
    Withholding them would be answering a question about somebody's own private
    page with a rule written about other people's conversations.
    """
    adapter, instance = _make_adapter()
    metadata = _metadata()
    assert METADATA_ENFORCE_AUDIENCE_KEY not in metadata

    await _update_tools(adapter, metadata=metadata)

    assert _registered(instance) == _TOOL_NAMES
    assert instance.rails == [], "there is no audience to ask about"


@pytest.mark.asyncio
async def test_a_turn_with_no_conversation_still_gets_both() -> None:
    """The seven tools beside these need a conversation; a Home tab is not one.

    Separate in both directions, and this is the direction that is easy to lose:
    copying the conversation condition from the tool next door would withhold a
    surface that has nothing to do with a conversation.
    """
    adapter, instance = _make_adapter()
    metadata = _metadata()
    metadata.pop("slack_channel_id")

    await _update_tools(adapter, metadata=metadata)

    assert _registered(instance) == _TOOL_NAMES


@pytest.mark.asyncio
async def test_the_toolkit_is_built_once_and_holds_a_provider_not_a_copy() -> None:
    """One toolkit for the process, reading the request through a provider.

    The cards are shared across concurrent transports, so the registration is
    one-way: withdrawing them because *this* request names nobody would take
    them from a simultaneous request that does. What keeps that safe is that
    the toolkit is handed a provider rather than one request's metadata, so
    the second turn is answered against the second turn's facts.
    """
    adapter, _instance = _make_adapter()

    await _update_tools(adapter, metadata=_metadata())
    await _update_tools(adapter, metadata=_metadata(**{METADATA_ASKER_KEY: "U0OTHER"}))

    assert len(_FakeHomeTabToolkit.instances) == 1
    provider = _FakeHomeTabToolkit.instances[0].metadata_provider
    assert provider is not None
    assert provider()[METADATA_ASKER_KEY] == "U0OTHER"
