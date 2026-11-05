"""Test scoped denials and permission snapshots at their runtime hooks."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from belle_agent.config import BelleConfigError

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
)
from belle_agent.integrations.jiuwenswarm import scope_permissions
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    SKILLS_REBUILD_SILENT,
    TOOL_PERMISSION_CHANNEL_ID,
    TOOL_PERMISSION_CHAT_ID,
)


def _scene_hook_input(tool_name: str, user_input=None):
    from openjiuwen.harness.security.host import PermissionSceneHookInput

    return PermissionSceneHookInput(
        ctx=SimpleNamespace(session=None),
        tool_call=SimpleNamespace(id="call_1", name=tool_name, arguments={}),
        user_input=user_input,
        normalized_tool_name=tool_name,
        tool_args={},
        engine=None,
    )


def _config(tools=None):
    return {
        "channels": {},
        "permissions": {
            "enabled": True,
            "tools": tools if tools is not None else {"bash": "allow"},
            "defaults": {"*": "allow"},
        },
    }


@pytest.fixture
def deployment(monkeypatch):
    """Install a config, bind a conversation, and hand back the two call sites."""

    bound: list = []

    def install(scopes, *, channel="slack", chat="C0", tools=None, people=None, roles=None):
        import jiuwenswarm.common.config as config_module

        data = _config(tools)
        monkeypatch.setattr(config_module, "get_config", lambda: data)
        monkeypatch.setattr(
            scope_permissions, "load_belle_config",
            lambda: {"scopes": scopes, "people": people, "roles": roles},
        )
        scope_permissions.reset_cache()
        scope_permissions.activate()
        # Bound the way the request handler binds them, rather than patched:
        # the ContextVar *is* the mechanism under test. ``channel=None`` binds nothing.
        if channel is not None:
            bound.append((TOOL_PERMISSION_CHANNEL_ID, TOOL_PERMISSION_CHANNEL_ID.set(channel)))
            bound.append((TOOL_PERMISSION_CHAT_ID, TOOL_PERMISSION_CHAT_ID.set(chat)))

        rail = build_permission_rail({"permissions": data["permissions"]})
        assert rail is not None
        return SimpleNamespace(
            hook=rail._host.permission_scene_hook,
            snapshot=rail._host.get_permissions_snapshot,
            persist_user=rail._host.persist_allow_rule,
        )

    yield install
    for var, token in reversed(bound):
        var.reset(token)
    scope_permissions.deactivate()
    scope_permissions.reset_cache()


def _deny_bash(chat="C0"):
    return [
        {
            "match": {"channel": "slack", "chat": chat},
            "permissions": {"tools": {"bash": "deny"}},
        }
    ]


def _ask_bash(chat="C0"):
    return [
        {
            "match": {"channel": "slack", "chat": chat},
            "permissions": {"tools": {"bash": "ask"}},
        }
    ]


# The scene hook branch


def test_a_scope_deny_is_refused_before_anything_can_ask(deployment):
    sites = deployment(_deny_bash())

    outcome = asyncio.run(sites.hook(_scene_hook_input("bash")))

    assert outcome[0] == "reject"
    assert "scopes" in outcome[1]


def test_it_fires_without_any_permission_context(deployment):
    """Apply scope denials without a PermissionContext."""
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        TOOL_PERMISSION_CONTEXT,
    )

    assert TOOL_PERMISSION_CONTEXT.get() is None
    sites = deployment(_deny_bash())

    assert asyncio.run(sites.hook(_scene_hook_input("bash")))[0] == "reject"


def test_an_ask_is_left_to_the_engine(deployment):
    """Leave scoped ask to the permission engine."""
    sites = deployment(_ask_bash())

    assert asyncio.run(sites.hook(_scene_hook_input("bash"))) is None
    assert sites.snapshot()["tools"]["bash"] == "ask"


def test_a_scope_for_another_conversation_does_not_reach_this_one(deployment):
    sites = deployment(_deny_bash(chat="C-OTHER"), chat="C0")

    assert asyncio.run(sites.hook(_scene_hook_input("bash"))) is None
    assert sites.snapshot()["tools"]["bash"] == "allow"


def test_a_tool_no_scope_names_still_falls_through(deployment):
    sites = deployment(_deny_bash())

    assert asyncio.run(sites.hook(_scene_hook_input("write_file"))) is None


def test_no_scopes_at_all_changes_nothing(deployment):
    sites = deployment([])

    assert asyncio.run(sites.hook(_scene_hook_input("bash"))) is None
    assert sites.snapshot()["tools"]["bash"] == "allow"


# A call that names no conversation


@pytest.fixture
def scope_warnings(caplog):
    """Capture the module's records once, whether or not its logger propagates."""
    logger = scope_permissions.logger
    propagate = logger.propagate
    logger.addHandler(caplog.handler)
    logger.propagate = False
    caplog.set_level(logging.WARNING, logger=logger.name)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.propagate = propagate


def test_an_unbound_call_is_refused_when_a_scope_sets_tool_levels(deployment, scope_warnings):
    sites = deployment(_deny_bash(chat="C-OTHER"), channel=None)

    outcome = asyncio.run(sites.hook(_scene_hook_input("write_file")))
    asyncio.run(sites.hook(_scene_hook_input("bash")))

    assert outcome == ("reject", scope_permissions.UNKNOWN_CONVERSATION_REFUSAL)
    assert "conversation of this tool call is unknown" in outcome[1]
    warnings = [r.getMessage() for r in scope_warnings.records]
    assert len(warnings) == 1
    assert "TOOL_PERMISSION_CHANNEL_ID" in warnings[0]


def test_an_unbound_snapshot_denies_every_tool(deployment):
    from openjiuwen.harness.security.permission_engine.toolguard.tool_policy import (
        evaluate_tiered_policy,
    )

    import jiuwenswarm.common.config as config_module

    sites = deployment(_ask_bash(), channel=None, tools={"bash": "allow", "write_file": "allow"})
    config_module.get_config()["permissions"]["approval_overrides"] = [
        {"id": "always", "tools": ["bash"], "match_type": "command", "pattern": "ls", "action": "allow"}
    ]

    snapshot = sites.snapshot()

    for tool, args in (("bash", {"command": "ls"}), ("write_file", {}), ("read_file", {})):
        assert evaluate_tiered_policy(snapshot, tool, args)[0].value == "deny"


def test_an_unbound_call_is_left_alone_when_no_scope_sets_tool_levels(deployment, scope_warnings):
    sites = deployment(
        [{"match": {"channel": "slack", "chat": "C0"}, "delivery": {"prompt": "Be terse."}}],
        channel=None,
    )

    assert asyncio.run(sites.hook(_scene_hook_input("bash"))) is None
    assert sites.snapshot()["tools"] == {"bash": "allow"}
    assert scope_warnings.records == []


# What sits above the new branch, and why


def test_ask_user_stays_out_of_scopes_reach(deployment):
    """Keep the ask_user bypass above scope denials."""
    sites = deployment(
        [
            {
                "match": {"channel": "slack", "chat": "C0"},
                "permissions": {"tools": {"ask_user": "deny"}},
            }
        ]
    )

    assert asyncio.run(sites.hook(_scene_hook_input("ask_user"))) == ("approve",)


def test_the_silent_skills_rebuild_session_stays_above_scopes(deployment):
    """Keep silent Skill rebuilds above scope restrictions."""
    sites = deployment(_deny_bash())
    token = SKILLS_REBUILD_SILENT.set(True)
    try:
        assert asyncio.run(sites.hook(_scene_hook_input("bash"))) == ("approve",)
        snapshot = sites.snapshot()
        assert snapshot["defaults"] == {"*": "allow"}
        assert "bash" not in (snapshot.get("tools") or {})
    finally:
        SKILLS_REBUILD_SILENT.reset(token)


# The snapshot


def test_the_snapshot_narrows_the_config_the_engine_evaluates(deployment):
    sites = deployment(_ask_bash(), tools={"bash": "allow", "write_file": "allow"})

    narrowed = sites.snapshot()

    assert narrowed["tools"]["bash"] == "ask"
    assert narrowed["tools"]["write_file"] == "allow"


def test_the_snapshot_cannot_widen(deployment):
    sites = deployment(
        [
            {
                "match": {"channel": "slack", "chat": "C0"},
                "permissions": {"tools": {"bash": "allow"}},
            }
        ],
        tools={"bash": "deny"},
    )

    assert sites.snapshot()["tools"]["bash"] == "deny"


def test_the_two_sites_agree_on_a_deny(deployment):
    sites = deployment(_deny_bash())

    assert asyncio.run(sites.hook(_scene_hook_input("bash")))[0] == "reject"
    assert sites.snapshot()["tools"]["bash"] == "deny"


def test_a_scope_narrowing_to_ask_defeats_a_persisted_always_allow(deployment):
    """Prune persisted overrides that would bypass scoped ask."""
    from openjiuwen.harness.security.permission_engine.toolguard.tool_policy import (
        evaluate_tiered_policy,
    )

    import jiuwenswarm.common.config as config_module

    sites = deployment(_ask_bash())
    data = config_module.get_config()
    data["permissions"]["approval_overrides"] = [
        {
            "id": "always-git",
            "tools": ["bash"],
            "match_type": "command",
            "pattern": "git status",
            "action": "allow",
        }
    ]

    level, _rule = evaluate_tiered_policy(
        sites.snapshot(), "bash", {"command": "git status"}
    )

    assert level.value == "ask"


def test_persisting_after_scope_pruning_restores_operator_approvals(
    deployment, monkeypatch
):
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers

    existing = {"id": "operator", "tools": ["bash"], "action": "allow"}
    added = {"id": "new", "tools": ["write_file"], "action": "allow"}
    monkeypatch.setattr(
        permissions_layers,
        "load_global_permissions",
        lambda: {"approval_overrides": [existing]},
    )
    monkeypatch.setattr(permissions_layers, "load_user_permissions", lambda: {})
    monkeypatch.setattr(
        permissions_layers, "load_session_permissions", lambda _session_id: {}
    )
    saved = []
    monkeypatch.setattr(
        permissions_layers,
        "persist_user_overlay_from_effective",
        lambda permissions, session_id=None: saved.append(permissions) or True,
    )
    sites = deployment(_deny_bash())

    assert sites.persist_user({"approval_overrides": [added]})
    assert saved[0]["approval_overrides"] == [existing, added]


# The conversation the two sites read


def test_request_binds_and_restores_channel_and_chat():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = SimpleNamespace(
        _is_session_scoped_adapter=False,
        _enable_auto_permission=False,
        _permission_rail=None,
        _root_permission_queue=None,
        _resolve_interrupt_session_id=lambda session_id: session_id,
    )
    request = SimpleNamespace(
        params={}, session_id="session", request_id="request",
        channel_id="slack", chat_id="C1",
    )
    channel_token = TOOL_PERMISSION_CHANNEL_ID.set("outer")
    chat_token = TOOL_PERMISSION_CHAT_ID.set("C0")
    try:
        with JiuWenSwarmDeepAdapter._bind_permission_request_context(adapter, request):
            assert TOOL_PERMISSION_CHANNEL_ID.get() == "slack"
            assert TOOL_PERMISSION_CHAT_ID.get() == "C1"
        assert TOOL_PERMISSION_CHANNEL_ID.get() == "outer"
        assert TOOL_PERMISSION_CHAT_ID.get() == "C0"
    finally:
        TOOL_PERMISSION_CHAT_ID.reset(chat_token)
        TOOL_PERMISSION_CHANNEL_ID.reset(channel_token)


# Roles, runtime side


def test_a_role_bearing_permissions_rule_stops_the_agent_start(deployment):
    """Reject sender-filtered permission rules before runtime."""
    with pytest.raises(BelleConfigError, match=r"scopes\[0\]\.permissions cannot be in a rule that matches on role"):
        deployment(
            [
                {
                    "match": {"channel": "slack", "chat": "C0", "not": {"role": "admin"}},
                    "permissions": {"tools": {"bash": "deny"}},
                }
            ],
            people={"hana": {"slack": "U000000AAAA"}},
            roles={"admin": ["hana"]},
        )


def test_a_role_no_config_declares_stops_the_agent_start(deployment):
    with pytest.raises(BelleConfigError, match=r"scopes\[0\]\.match names admin, which is not a valid role"):
        deployment(
            [
                {
                    "match": {"channel": "slack", "chat": "C0", "not": {"role": "admin"}},
                    "delivery": {"prompt": "Be terse."},
                }
            ]
        )


def test_editing_only_the_roles_block_recompiles(deployment, monkeypatch):
    """Recompile cached scopes when roles change."""
    scopes = [
        {
            "match": {"channel": "slack", "chat": "C0", "not": {"role": "admin"}},
            "delivery": {"prompt": "Be terse."},
        }
    ]
    deployment(scopes, people={"hana": {"slack": "U1"}}, roles={"admin": ["hana"]})
    assert scope_permissions.runtime_scopes()[0].match.not_users == ("U1",)

    # The scopes: list is byte for byte what it was. A cache keyed on it alone
    # would serve the old ids until something unrelated changed.
    monkeypatch.setattr(
        scope_permissions, "load_belle_config",
        lambda: {
            "scopes": scopes,
            "people": {"hana": {"slack": "U2"}},
            "roles": {"admin": ["hana"]},
        },
    )

    assert scope_permissions.runtime_scopes()[0].match.not_users == ("U2",)


# Neither entry point may raise into the callback that called it


def test_a_compile_that_raises_leaves_the_permissions_block_as_written(
    deployment, monkeypatch
):
    """Keep base permissions if scope compilation fails."""
    sites = deployment(_deny_bash())

    def explode(*_args, **_kwargs):
        raise RuntimeError("a scope this compiler cannot read")

    monkeypatch.setattr(scope_permissions, "compile_scopes", explode)
    scope_permissions.reset_cache()

    assert scope_permissions.runtime_scopes() == ()
    assert asyncio.run(sites.hook(_scene_hook_input("bash"))) is None
    assert sites.snapshot()["tools"] == {"bash": "allow"}


def test_a_fold_that_raises_leaves_the_permissions_block_as_written(
    deployment, monkeypatch
):
    """Keep base permissions if permission folding fails."""
    sites = deployment(_deny_bash())

    def explode(*_args, **_kwargs):
        raise RuntimeError("levels that cannot be settled")

    monkeypatch.setattr(scope_permissions, "denied_tool_level", explode)
    monkeypatch.setattr(scope_permissions, "narrow_config_for_scopes", explode)

    assert scope_permissions.scope_refusal("bash") is None
    assert asyncio.run(sites.hook(_scene_hook_input("bash"))) is None
    assert sites.snapshot()["tools"] == {"bash": "allow"}
