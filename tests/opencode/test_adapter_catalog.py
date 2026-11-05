"""What the adapter lists: OpenCode's models, skills and subagents.

The first check runs on a scripted wire, with no container. The others read
the catalog of the OpenCode container, with and without the relay plugin.
"""

import httpx
import pytest
from conformance_kit import worlds
from slack_kit import SlackKit
from slack_transport import TEAM

from belle_agent.backend import CATALOG_EXPERTS, CATALOG_MODELS, CATALOG_SKILLS, CATALOG_SUBAGENTS, OP_CATALOG
from belle_agent.host import OUTCOME_FAILED, OUTCOME_POSTED_FINAL
from belle_agent.integrations.opencode import OpenCodeBackend
from belle_agent.integrations.opencode.pin import OPENCODE_VERSION
from belle_agent.platforms.slack.scope_policy import DEFAULT_COMMAND_NAME, command_token

COMMAND = command_token(DEFAULT_COMMAND_NAME)
# Two scopes that each name a model the harness configuration does not: one of its provider, one of another.
UNDECLARED = """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all]}
    agent: {model_name: configured/standin/undeclared}
  - match: {channel: loopback, chat: C2}
    delivery: {mode: [all]}
    agent: {model_name: openrouter/standin/scripted}
"""

KEY = "sk-the-provider-key"
# Each entry as OpenCode 2.2.5 answers it, less the members no check reads. The settings hold the provider key.
SETTINGS = {"apiKey": KEY, "baseURL": "https://provider.invalid/v1"}
ANSWERS = {
    "/api/model/default": {"providerID": "configured", "id": "vendor/served", "settings": SETTINGS},
    "/api/model": [
        {"providerID": "configured", "id": "vendor/other", "enabled": True, "settings": SETTINGS, "headers": {"authorization": KEY}},
        {"providerID": "configured", "id": "vendor/served", "enabled": True, "settings": SETTINGS},
        {"providerID": "configured", "id": "vendor/switched-off", "enabled": False, "settings": SETTINGS},
        {"providerID": "", "id": "nameless", "enabled": True},
    ],
    "/api/skill": [
        {"id": "opencode", "name": "OpenCode", "description": "About OpenCode itself.", "path": "/builtin/opencode.md", "content": "..."},
        {"id": "chat-digest", "name": "chat-digest", "path": "/workspace/.opencode/skills/chat-digest/SKILL.md", "content": "..."},
    ],
    "/api/agent": [
        {"id": "build", "mode": "primary", "hidden": False, "request": {"headers": {"authorization": KEY}}},
        {"id": "general", "mode": "subagent", "hidden": False, "request": {"headers": {"authorization": KEY}}},
        {"id": "explore", "mode": "subagent", "hidden": False},
        {"id": "internal", "mode": "subagent", "hidden": True},
        {"id": "title", "mode": "primary", "hidden": True},
    ],
}


async def test_the_catalog_keeps_names_and_drops_what_holds_the_provider_key():
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, json={"data": ANSWERS[request.url.path]})

    backend = OpenCodeBackend("http://opencode.invalid:4096", "synthetic", transport=httpx.MockTransport(answer))
    try:
        await backend._ready()  # what a start does first: the default model is known
        catalog = await backend.catalog()
    finally:
        await backend.close()

    # The model that serves by default comes first, once. A model OpenCode has switched off is not offered.
    assert catalog.models.names == ("configured/vendor/served", "configured/vendor/other")
    assert catalog.models.default == "configured/vendor/served"
    # A skill is named by its id, which is what a prompt attaches.
    assert catalog.skills == (
        {"name": "opencode", "description": "About OpenCode itself.", "installed": True},
        {"name": "chat-digest", "description": "", "installed": True},
    )
    # A subagent is an agent the model can delegate to: of that mode, and not hidden.
    assert catalog.subagents == ("general", "explore") and catalog.experts == ()
    assert KEY not in repr(catalog)
    assert asked == ["/api/model/default", "/api/model", "/api/model", "/api/skill", "/api/agent"]


def test_the_adapter_declares_the_catalog_it_lists_and_the_version_it_is_pinned_to():
    capabilities = OpenCodeBackend("http://opencode.invalid:4096", "synthetic").capabilities()

    assert OP_CATALOG in capabilities.operations
    assert capabilities.catalog == {CATALOG_MODELS, CATALOG_SKILLS, CATALOG_SUBAGENTS}
    assert CATALOG_EXPERTS not in capabilities.catalog
    assert capabilities.version == OPENCODE_VERSION
    # What a command that needs one of these says is not provided here.
    assert not {"expert", "subagents_available", "subagent_required"} & capabilities.scope_fields
    assert {"skills_required", "skills_available", "skills_unavailable", "model"} <= capabilities.scope_fields


@pytest.mark.adapter
async def test_opencode_lists_its_models_the_packaged_skills_and_its_subagents(world, kit):
    catalog = await world.host.catalog()

    # The stand-in configuration names two models, and OpenCode lists those two and nothing else.
    # That the first one serves a plain turn is a check of the conformance suite.
    assert catalog.models.names == ("configured/standin/scripted", kit.other_model)
    names = [skill["name"] for skill in catalog.skills]
    # Belle's packaged skills, copied into the image, beside OpenCode's own two.
    assert {"group-chat-participation", "chat-digest", "belle-command-help", "slack-app-home", "opencode", "report"} <= set(names)
    assert all(skill["installed"] is True for skill in catalog.skills)
    assert {"general", "explore"} <= set(catalog.subagents)
    assert not {"build", "plan", "title", "summary", "compaction"} & set(catalog.subagents)
    assert catalog.experts == ()



@pytest.mark.adapter
async def test_a_model_the_configuration_does_not_declare_serves_no_turn(build_world):
    world = await build_world(UNDECLARED)

    await world.send(world.message("SAY=one", conversation="C1"))
    await world.send(world.message("SAY=two", conversation="C2"))
    await world.send(world.direct("SAY=three"))

    # OpenCode takes the switch and fails the turn. What it says of the model is what the person reads.
    assert world.outcomes() == [OUTCOME_FAILED, OUTCOME_FAILED, OUTCOME_POSTED_FINAL]
    assert [post.text for post in world.posts()] == [
        "Model unavailable: configured/standin/undeclared",
        "Model unavailable: openrouter/standin/scripted",
        "three",
    ]


@pytest.mark.adapter
async def test_the_slack_command_lists_the_declared_models_and_takes_no_other(kit, tmp_path, monkeypatch):
    async with worlds(kit, tmp_path, SlackKit(monkeypatch)) as build:
        slack = (await build()).platform.app
        listed, unknown, chosen = [
            (await slack.slash(COMMAND, text, user="U0ASKER001", channel="C0ROOM0001", team=TEAM)).texts[-1]
            for text in ("models list", "models use configured/standin/undeclared", f"models use {kit.other_model}")
        ]

    assert listed == f"Configured models:\n• `configured/standin/scripted`\n• `{kit.other_model}`"
    assert unknown.startswith("Unknown model `configured/standin/undeclared`.")
    assert chosen.startswith(f"Selected `{kit.other_model}`")
