"""JiuwenSwarm Slack calls and listeners stay covered by Belle's policy."""

import ast
import re
from pathlib import Path

import belle_agent
import jiuwenswarm
import pytest
import yaml
from belle_agent.platforms.slack import scope_policy as slack_scope_policy
from belle_agent.platforms.slack.events import EVENT_TYPE_FAMILIES
from belle_agent.platforms.slack.scope_policy import (
    COMMAND_REQUIREMENTS,
    EVENT_REQUIREMENTS,
    MESSAGE_SUBSCRIPTIONS,
    METHOD_REQUIREMENTS,
    TIERS,
    render_manifest,
)

_PACKAGE_ROOT = Path(jiuwenswarm.__file__).resolve().parent
_BELLE_ROOT = Path(belle_agent.__file__).resolve().parent
_BASE_CONNECTOR = (
    _PACKAGE_ROOT
    / "gateway"
    / "channel_manager"
    / "im_platforms"
    / "slack"
    / "slack_connect.py"
)
_CONNECTOR = _BELLE_ROOT / "integrations" / "jiuwenswarm" / "slack_channel.py"
_COMMAND_UI = _BELLE_ROOT / "platforms" / "slack" / "command_ui.py"
_SDK_METHOD_RE = re.compile(r"^[a-z]+_[a-zA-Z0-9_]+$")
_CLIENT_NAMES = frozenset({"client", "_client", "web_client", "slack_client"})
_API_CALL = "api_call"
_CONFIGURED_COMMAND = "_slash_command"


def _slack_sources() -> list[Path]:
    """Every source file that is part of the Slack integration.

    Selected by name rather than listed, so a module added beside the existing
    ones is scanned without anybody remembering to add it here.
    """
    found = [
        path
        for root in (_PACKAGE_ROOT, _BELLE_ROOT)
        for path in root.rglob("*.py")
        if "slack" in path.name.lower()
        or any("slack" in part.lower() for part in path.relative_to(root).parts[:-1])
    ]
    assert found, f"no Slack sources found under {_PACKAGE_ROOT}"
    return sorted(found)


def _normalise(method: str) -> str:
    """One spelling for a method this repository writes two ways.

    ``reactions.add`` and ``reactions_add`` are the same call: the SDK spelling
    where a wrapper is called, the dotted spelling where the call goes through
    ``api_call`` because the pinned SDK has no wrapper for it.
    """
    return method.replace(".", "_")


def _sdk_methods() -> frozenset[str]:
    """Method names the installed Slack SDK exposes, or an empty set.

    An oracle, not a requirement. It is what lets discovery notice a *new*
    method handed to a per-module call helper as a literal -- a name the policy
    module has never heard of and so cannot recognise on its own. Where the SDK
    is absent that arm is simply not available, and the tests say so rather than
    pretending the narrower scan was the whole one.
    """
    try:
        from slack_sdk.web.async_client import AsyncWebClient
    except Exception:  # noqa: BLE001 - the SDK is optional to this check
        return frozenset()
    return frozenset(
        name
        for name in dir(AsyncWebClient)
        if not name.startswith("_") and _SDK_METHOD_RE.match(name)
    )


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, for resolving an argument.

    ``api_call`` is handed a constant rather than a literal at every one of its
    call sites, and the constant is what carries the method name.
    """
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(
            node.value.value, str
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _is_client(node: ast.expr) -> bool:
    """Whether an expression is something this repository calls Slack through."""
    if isinstance(node, ast.Name):
        return node.id in _CLIENT_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _CLIENT_NAMES
    if isinstance(node, ast.Call):
        func = node.func
        return isinstance(func, ast.Attribute) and func.attr.endswith("get_client")
    return False


def _calls_in(path: Path, oracle: frozenset[str]) -> set[str]:
    """The Slack methods one file calls, normalised.

    Three shapes, all three read:

    * ``client.chat_postMessage(...)`` -- an attribute on a client object.
    * ``client.api_call(_SEARCH_METHOD, ...)`` -- the escape hatch, whose first
      argument names the method as a literal or a module constant.
    * ``self._call("pins_list", ...)`` -- a literal handed to a per-module
      helper that does the ``getattr``. Recognised because the literal is a name
      the policy module already knows, or because the SDK oracle says it is a
      method.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constants = _module_constants(tree)
    found: set[str] = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and _is_client(node.value)
            and node.attr != _API_CALL
            and _SDK_METHOD_RE.match(node.attr)
        ):
            found.add(node.attr)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == _API_CALL and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(_normalise(first.value))
                elif isinstance(first, ast.Name) and first.id in constants:
                    found.add(_normalise(constants[first.id]))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            candidate = _normalise(node.value)
            if candidate in METHOD_REQUIREMENTS or candidate in oracle:
                found.add(candidate)

    return found


def _registered_events() -> set[str]:
    """The event types the connector has a listener for.

    Two sources, because the connector registers two ways. The named ones are
    literals passed to ``app.event(...)``. The six non-message ones are
    registered by iterating ``EVENT_TYPE_FAMILIES``, which is imported rather
    than re-parsed: that table is the connector's own declaration of which
    events it handles, so reading it directly cannot disagree with it.

    The catch-all ``app.event(_ANY_EVENT_TYPE)`` contributes nothing. It is a
    compiled pattern rather than a literal, and it subscribes to nothing: it is
    a last-resort bolt listener for an event that arrived with no handler.
    """
    literal: set[str] = set()
    for source in (_BASE_CONNECTOR, _CONNECTOR):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "event":
                literal.update(arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str))

    assert literal, "no app.event(...) literals found; the connector has moved"

    events: set[str] = set()
    for name in literal:
        if name == "message":
            events.update(MESSAGE_SUBSCRIPTIONS)
        else:
            events.add(name)
    events.update(EVENT_TYPE_FAMILIES)
    return events


def _registered_commands() -> set[str]:
    """The commands the connector has a listener for, as the table keys them.

    Ids and not names. The one command's name is configurable, so what stands
    in the source at ``app.command(...)`` is the accessor that reads it rather
    than a word: an accessor call contributes the id of the one command this
    table holds. A literal or a policy-module constant is still read as itself,
    for a second command added later that has a fixed name.
    """
    tree = ast.parse(_COMMAND_UI.read_text(encoding="utf-8"), filename=str(_COMMAND_UI))
    commands: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "command"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "app"
        ):
            continue
        for arg in node.args:
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == _CONFIGURED_COMMAND
            ):
                commands.add(slack_scope_policy.MAIN_COMMAND)
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                commands.add(arg.value)
            elif isinstance(arg, ast.Name):
                value = getattr(slack_scope_policy, arg.id, None)
                assert isinstance(value, str), (
                    f"app.command({arg.id}) names nothing slack_scope_policy exports"
                )
                commands.add(value)
    return commands


def _connector_registers_actions() -> bool:
    """Whether the connector handles ``block_actions`` payloads."""
    tree = ast.parse(_CONNECTOR.read_text(encoding="utf-8"), filename=str(_CONNECTOR))
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "action"
        for node in ast.walk(tree)
    )


@pytest.fixture(scope="module")
def slack_calls() -> set[str]:
    oracle = _sdk_methods()
    found: set[str] = set()
    for path in _slack_sources():
        found |= _calls_in(path, oracle)
    return found


@pytest.fixture(scope="module")
def manifests() -> dict[int, dict]:
    return {tier: yaml.safe_load(render_manifest(tier)) for tier in TIERS}


def _bot_events(manifest: dict) -> set[str]:
    return set(manifest["settings"]["event_subscriptions"]["bot_events"])


def test_slack_sources_are_found() -> None:
    """The scan reaches the connector and every tool, not just one of them."""
    names = {path.name for path in _slack_sources()}
    for expected in (
        "slack_connect.py",
        "slack_channel.py",
        "slack_history.py",
        "slack_pins.py",
        "slack_bookmarks.py",
        "slack_reactions.py",
        "slack_search.py",
    ):
        assert expected in names, f"{expected} is no longer covered by the scan"


def test_discovery_finds_all_three_call_shapes(slack_calls: set[str]) -> None:
    for method, shape in (
        ("chat_postMessage", "an attribute on the connector's client"),
        ("files_upload_v2", "an attribute on the connector's client"),
        ("blocks_validate", "a constant handed to api_call"),
        ("assistant_threads_setStatus", "a constant handed to api_call"),
        ("chat_startStream", "a literal handed to a per-module call helper"),
    ):
        assert method in slack_calls, f"discovery no longer finds {method} ({shape})"


def test_the_sdk_oracle_is_live() -> None:
    oracle = _sdk_methods()
    if not oracle:
        pytest.skip(
            "slack_sdk is not importable, so discovery cannot recognise a Slack"
            " method the policy module has never heard of"
        )
    assert "conversations_history" in oracle
    assert "stream" not in oracle


def test_every_slack_call_is_classified(slack_calls: set[str]) -> None:
    unknown = sorted(slack_calls - set(METHOD_REQUIREMENTS))
    assert not unknown, (
        "Slack methods called but not classified: "
        + ", ".join(unknown)
        + ". Add each to METHOD_REQUIREMENTS in"
        " belle_agent.platforms.slack.scope_policy with the scopes Slack"
        " requires and the tier it belongs to, then run"
        " Belle Agent's scripts/generate_slack_manifest.py."
    )


def test_every_registered_event_is_classified() -> None:
    unknown = sorted(_registered_events() - set(EVENT_REQUIREMENTS))
    assert not unknown, (
        "events the connector listens for but nothing classifies: "
        + ", ".join(unknown)
        + ". Add each to EVENT_REQUIREMENTS, then regenerate."
    )


def test_nothing_is_classified_that_is_not_registered() -> None:
    """The other direction: a subscription with no listener is dropped traffic."""
    extra = sorted(set(EVENT_REQUIREMENTS) - _registered_events())
    assert not extra, (
        "EVENT_REQUIREMENTS names events the connector has no listener for: "
        + ", ".join(extra)
    )


def test_every_registered_command_is_classified() -> None:
    registered = _registered_commands()
    assert registered, "no app.command(...) found; the connector has moved"
    unknown = sorted(registered - set(COMMAND_REQUIREMENTS))
    assert not unknown, (
        "slash commands the connector listens for but nothing classifies: "
        + ", ".join(unknown)
        + ". Add each to COMMAND_REQUIREMENTS, then regenerate."
    )
    assert registered == {slack_scope_policy.MAIN_COMMAND}


def test_no_command_is_declared_that_is_not_registered() -> None:
    """A declared command with no listener fails in front of whoever types it."""
    extra = sorted(set(COMMAND_REQUIREMENTS) - _registered_commands())
    assert not extra, (
        "COMMAND_REQUIREMENTS names commands the connector has no listener for: "
        + ", ".join(extra)
    )


def test_interactivity_is_enabled_in_every_tier(manifests: dict[int, dict]) -> None:
    assert _connector_registers_actions(), (
        "the connector no longer registers app.action listeners; if the buttons"
        " are gone, interactivity can be turned off in the manifests too"
    )
    for tier in TIERS:
        interactivity = manifests[tier]["settings"]["interactivity"]
        assert interactivity["is_enabled"] is True
        assert "request_url" not in interactivity


def test_no_agent_session_event_is_declared_without_a_listener(
    manifests: dict[int, dict],
) -> None:
    agent_events = {
        "app_context_changed",
        "agent_session_stopped",
        "agent_session_title_changed",
    }
    registered = _registered_events()
    for tier in TIERS:
        declared = _bot_events(manifests[tier]) & agent_events
        assert declared <= registered, (
            f"tier {tier} subscribes to {sorted(declared - registered)}, which"
            " the connector has no listener for"
        )
