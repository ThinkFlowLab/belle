"""Per-request registration tests for the three Slack posting tools.

The seventh Slack decision, and the only one that needs a rail. The six before
it are mounted for any turn whose conversation the gateway named, because each of
them acts in that conversation and can reach nowhere else. These can name a
conversation, so a post can reach people who cannot read the conversation it
came from.

Three things are pinned here and nowhere else:

* a request carrying no ``slack_enforce_audience`` mounts nothing at all, so a
  path nobody taught to stamp the key loses the tools rather than gaining
  ungoverned ones;
* the rail that asks before a post widens is registered beside the tools, and
  the tools are not mounted without it;
* ``enforce_audience: false`` still mounts all three, because it is a deployment
  that holds no audience bound rather than one with no Slack tools.
"""

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
from slack_runtime_test_helpers import (
    SlackRuntimeAbilityManager as _FakeAbilityManager,
)
from slack_runtime_test_helpers import (
    SlackRuntimeResourceManager as _FakeResourceManager,
)
from slack_runtime_test_helpers import (
    fake_belle_plugin_loader,
    make_slack_runtime_adapter,
)
from slack_runtime_test_helpers import (
    update_slack_runtime_tools as _update_tools,
)

from belle_agent.integrations.jiuwenswarm import slack_runtime_tools
from belle_agent.platforms.slack.history_policy import (
    METADATA_ENFORCE_AUDIENCE_KEY,
)

_TOOL_NAMES = ("post_message", "edit_message", "delete_message")






class _FakePostToolkit:
    """Stands in for ``SlackPostToolkit``: it mounts three cards and no gate.

    The real toolkit's own decision -- whether the audience bound is held for
    this request -- is read off the request metadata at call time and is tested
    against the real class in ``test_slack_post_message.py``. Nothing about
    mounting depends on it, so the stand-in reads no metadata at all.
    """

    instances: ClassVar[list["_FakePostToolkit"]] = []

    def __init__(self, *, metadata_provider: Any = None, **_: Any) -> None:
        self.metadata_provider = metadata_provider
        type(self).instances.append(self)

    def get_tools(self) -> list[Any]:
        return [
            SimpleNamespace(card=SimpleNamespace(id=f"{name}-1", name=name))
            for name in _TOOL_NAMES
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
    def __init__(self, *, rail_fails: bool = False) -> None:
        self.ability_manager = _FakeAbilityManager()
        self.rails: list[Any] = []
        self._rail_fails = rail_fails

    async def register_rail(self, rail: Any) -> None:
        if self._rail_fails:
            raise RuntimeError("this agent takes no rails")
        self.rails.append(rail)


@pytest.fixture(autouse=True)
def _toolkits(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakePostToolkit.instances = []
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
    monkeypatch.setattr(slack_runtime_tools, "SlackPostToolkit", _FakePostToolkit)
    for name in (
        "SlackHistoryToolkit",
        "SlackSearchToolkit",
        "SlackReactionToolkit",
        "SlackPinToolkit",
        "SlackBookmarkToolkit",
        "SlackDirectoryToolkit",
        "SlackHomeTabToolkit",
        "SlackCanvasToolkit",
        "SlackListToolkit",
        "SlackScheduledToolkit",
    ):
        monkeypatch.setattr(slack_runtime_tools, name, _FakeEmptyToolkit)
    monkeypatch.setattr(slack_runtime_tools, "load_plugin", fake_belle_plugin_loader(_FakeResourceManager()))


def _make_adapter(*, rail_fails: bool = False) -> tuple[Any, _FakeInstance]:
    instance = _FakeInstance(rail_fails=rail_fails)
    return make_slack_runtime_adapter(instance), instance




def _registered(instance: _FakeInstance) -> set[str]:
    return {getattr(card, "name", "") for card in instance.ability_manager.list()}


def _metadata(enforce: bool = True, **extra: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "slack_channel_id": "C0ROOM",
        "slack_channel_type": "channel",
        "slack_team_id": "T0ACME",
        "slack_user_id": "U0ASKER",
        METADATA_ENFORCE_AUDIENCE_KEY: enforce,
    }
    metadata.update(extra)
    return metadata


@pytest.mark.asyncio
async def test_an_unstamped_request_mounts_nothing() -> None:
    """No key means nobody with the configuration settled this request.

    Different from settling it to ``false``, and mounting nothing is the only
    safe reading: a path that has not been taught to stamp the key loses the
    tools rather than gaining ungoverned ones.
    """
    adapter, instance = _make_adapter()
    metadata = _metadata()
    metadata.pop(METADATA_ENFORCE_AUDIENCE_KEY)
    await _update_tools(adapter, metadata=metadata)

    assert _registered(instance) == set()
    assert instance.rails == []


@pytest.mark.asyncio
async def test_a_request_from_another_transport_mounts_nothing() -> None:
    adapter, instance = _make_adapter()
    await _update_tools(adapter, channel_id="web", metadata=_metadata())

    assert _registered(instance) == set()


@pytest.mark.asyncio
async def test_a_settled_request_mounts_the_rail_beside_the_tools() -> None:
    """The rail goes on with the tools, for every settled request.

    A write out of a private conversation into a wider one is a question
    whatever the config says, so there is no mounting path that leaves the rail
    off.
    """
    adapter, instance = _make_adapter()
    await _update_tools(adapter, metadata=_metadata())

    assert _registered(instance) == set(_TOOL_NAMES)
    assert len(instance.rails) == 1
    assert type(instance.rails[0]).__name__ == "SlackWriteConfirmationRail"


@pytest.mark.asyncio
async def test_an_unenforced_deployment_still_mounts_all_three() -> None:
    """``false`` is no audience bound, not no Slack tools.

    Withholding the tools for it would answer a question the operator did not
    ask. Whether a tool may be offered at all is ``permissions.tools``.
    """
    adapter, instance = _make_adapter()
    await _update_tools(adapter, metadata=_metadata(False))

    assert _registered(instance) == set(_TOOL_NAMES)
    assert len(instance.rails) == 1


@pytest.mark.asyncio
async def test_nothing_mounts_when_the_rail_cannot_be_registered() -> None:
    """Keeping the rule and dropping the asking is the one silent failure here.

    The audience rule puts a post reaching people outside this conversation to
    whoever asked before it is sent, and the rail is the only thing that can put
    it -- a tool returns a string and has no way to stop and ask. Mounting the
    tools without it would leave the operator reading ``enforce_audience: true``
    in their config while posts go out unasked, with nothing on either side
    saying so.
    """
    adapter, instance = _make_adapter(rail_fails=True)
    await _update_tools(adapter, metadata=_metadata())

    assert _registered(instance) == set()
    assert instance.rails == []


@pytest.mark.asyncio
async def test_registration_is_one_way_within_a_process() -> None:
    """The cards are shared across concurrent transports.

    Withdrawing them because *this* request may not post would take them from a
    simultaneous request that may, so the mount is one-way and every tool
    refuses per request instead. The rail is registered once for the same
    reason.
    """
    adapter, instance = _make_adapter()
    await _update_tools(adapter, metadata=_metadata())
    await _update_tools(adapter, metadata=_metadata())
    assert len(instance.rails) == 1
    assert adapter._slack_manager.rails() == tuple(instance.rails)
    assert len(_FakePostToolkit.instances) == 1

    await _update_tools(adapter, channel_id="web", metadata=None)
    assert _registered(instance) == set(_TOOL_NAMES)
