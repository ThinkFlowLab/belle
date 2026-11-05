"""Belle config rejects incomplete policy files."""

import importlib
from pathlib import Path

import pytest

from belle_agent.config import SECTIONS, BelleConfigError, listener_settings, load_config, model_entry
from belle_agent.typed_decision import decision_model


def test_shipped_example_is_valid(monkeypatch) -> None:
    for key in ("DECISION_API_BASE", "DECISION_API_KEY", "DECISION_MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)
    config = load_config(Path(__file__).resolve().parents[1] / "examples/config/belle-config.yaml")
    assert config["platforms"]["slack"]["enforce_audience"] is True
    assert decision_model(config) is None


@pytest.mark.parametrize(
    ("entry", "field"),
    [
        ("client_provider: DashScope", "client_provider"),
        ("api_base: relative/path", "api_base"),
        ("api_base: 'https://model.test/v1?token=secret'", "api_base"),
    ],
)
def test_summarizer_rejects_settings_the_shared_backend_cannot_use(tmp_path: Path, entry: str, field: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "platforms: {loopback: {enabled: true}}\nmodels:\n  summarizer:\n    entries:\n"
        f"      - id: documents\n        api: chat_completions\n        {entry}\n",
        encoding="utf-8",
    )
    with pytest.raises(BelleConfigError, match=rf"models.summarizer.entries\[documents\].{field}"):
        load_config(path)


def test_embeddings_endpoint_must_be_an_absolute_http_url(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("second_brain: {retrieval: {embeddings: {api_base: file:///tmp/embeddings}}}\n", encoding="utf-8")
    with pytest.raises(BelleConfigError, match="second_brain.retrieval.embeddings.api_base"):
        load_config(path)


def test_slack_tool_settings_are_in_belle_example() -> None:
    example = load_config(Path(__file__).resolve().parents[1] / "examples/config/belle-config.yaml")
    assert example["platforms"]["slack"]["search_enabled"] is True
    assert example["platforms"]["slack"]["history_max_messages"] == 2000
    assert example["platforms"]["slack"]["command_name"] == "belle"
    assert example["second_brain"]["enabled"] is True
    assert example["platforms"]["slack"]["decision_forward"] is True


@pytest.mark.parametrize(
    "backend",
    [pytest.param("opencode", marks=pytest.mark.opencode), pytest.param("docker_agent", marks=pytest.mark.docker_agent)],
)
def test_belle_example_lists_every_key_of_each_backend(backend) -> None:
    settings = importlib.import_module(f"belle_agent.integrations.{backend}.settings")
    example = load_config(Path(__file__).resolve().parents[1] / "examples/config/belle-config.yaml")
    assert set(example["backends"][backend]) == settings._KEYS


EXAMPLES = sorted((Path(__file__).resolve().parents[1] / "examples/config").glob("belle-*.yaml"))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda path: path.name)
def test_every_shipped_example_loads(example: Path) -> None:
    assert load_config(example)


@pytest.mark.parametrize("section", ["conversations", "platfroms"])
def test_an_unknown_section_is_refused_with_the_known_ones(tmp_path, section: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"platforms: {{slack: {{enforce_audience: true}}}}\n{section}: {{}}\n", encoding="utf-8")
    with pytest.raises(BelleConfigError, match=f"config {section} is not a section") as raised:
        load_config(path)
    assert all(name in str(raised.value) for name in SECTIONS)


SLACK = "platforms: {slack: {enforce_audience: true}}\n"
SECRET_FIELDS = [
    ("models: {decision: {entries: [{id: d, api: openai, api_key: VALUE}]}}\n", "models.decision.entries[0].api_key", "DECISION_API_KEY"),
    ("second_brain: {retrieval: {embeddings: {api_key: VALUE}}}\n", "second_brain.retrieval.embeddings.api_key", "EMBEDDINGS_API_KEY"),
    ("backends: {acp: {tools_token: VALUE}}\n", "backends.acp.tools_token", "BELLE_TOOLS_TOKEN"),
    ("platforms: {slack: {enforce_audience: true, bot_token: VALUE}}\n", "platforms.slack.bot_token", "SLACK_BOT_TOKEN"),
    ("platforms: {slack: {enforce_audience: true, app_token: VALUE}}\n", "platforms.slack.app_token", "SLACK_APP_TOKEN"),
    ("platforms: {slack: {enforce_audience: true, workspaces: [{}, {app_token: VALUE}]}}\n", "platforms.slack.workspaces[1].app_token", "SLACK_APP_TOKEN"),
]


def _secret_config(tmp_path, content: str, value: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text((content if content.startswith("platforms") else SLACK + content).replace("VALUE", value), encoding="utf-8")
    return path


@pytest.mark.parametrize("literal", ["sk-written-in-the-file", "'${SECRET} '", "'prefix-${SECRET}'", "12345"])
@pytest.mark.parametrize(("content", "field", "variable"), SECRET_FIELDS)
def test_a_literal_secret_is_refused_by_its_field_and_not_shown(tmp_path, content: str, field: str, variable: str, literal: str) -> None:
    with pytest.raises(BelleConfigError) as raised:
        load_config(_secret_config(tmp_path, content, literal))
    message = str(raised.value)
    assert message == f"Belle config {field} must be an environment reference such as ${{{variable}}}, not a literal value"
    assert literal.strip("'") not in message


@pytest.mark.parametrize("value", ["'${BELLE_TEST_SECRET}'", "''", "null"])
@pytest.mark.parametrize(("content", "field", "variable"), SECRET_FIELDS)
def test_a_secret_field_takes_a_reference_or_nothing(tmp_path, monkeypatch, content: str, field: str, variable: str, value: str) -> None:
    monkeypatch.setenv("BELLE_TEST_SECRET", "xoxb-xapp-secret")
    assert load_config(_secret_config(tmp_path, content, value))


def test_none_returns_defaults() -> None:
    assert load_config(None) == {"platforms": {"slack": {"enforce_audience": True}}}


def test_workspace_tokens_and_question_path_resolve_from_belle_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BELLE_TEST_BOT_TOKEN", "xoxb-test")
    path = tmp_path / "config.yaml"
    path.write_text(
        "platforms:\n"
        "  slack:\n"
        "    enforce_audience: true\n"
        "    workspaces: [{bot_token: '${BELLE_TEST_BOT_TOKEN}', app_token: '${BELLE_TEST_APP_TOKEN}'}]\n"
        "    decision_questions_file: questions.json\n",
        encoding="utf-8",
    )
    slack = load_config(path)["platforms"]["slack"]
    assert slack["workspaces"] == [{"bot_token": "xoxb-test", "app_token": ""}]
    assert slack["decision_questions_file"] == str(tmp_path / "questions.json")


def test_single_workspace_tokens_expand_from_belle_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-belle")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-belle")
    path = tmp_path / "config.yaml"
    path.write_text(
        "platforms: {slack: {enforce_audience: true, bot_token: '${SLACK_BOT_TOKEN}', app_token: '${SLACK_APP_TOKEN}'}}\n"
    )
    assert load_config(path)["platforms"]["slack"]["bot_token"] == "xoxb-belle"
    assert load_config(path)["platforms"]["slack"]["app_token"] == "xapp-belle"


def test_explicit_file_preserves_other_settings(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "platforms:\n"
        "  slack:\n"
        "    enforce_audience: false\n"
        "    future_key: 42\n"
        "scopes: [{match: {channel: slack}, delivery: {reply: required}}]\n"
        "people: {alice: {slack: [U123]}}\n"
        "roles: {maintainers: [alice]}\n",
        encoding="utf-8",
    )
    assert load_config(str(path)) == {
        "platforms": {
            "slack": {
                "enforce_audience": False,
                "future_key": 42,
            },
        },
        "scopes": [{"match": {"channel": "slack"}, "delivery": {"reply": "required"}}],
        "people": {"alice": {"slack": ["U123"]}},
        "roles": {"maintainers": ["alice"]},
    }


@pytest.mark.parametrize(
    "content, message",
    [
        ("", "root must be a mapping"),
        ("[]\n", "root must be a mapping"),
        ("platforms: {slack: []}\n", "slack must be a mapping"),
        ("platforms: {slack: {}}\n", "slack.enforce_audience"),
        ("platforms: {slack: {enforce_audience: 'false'}}\n", "slack.enforce_audience"),
        ("platforms: {slack: {enforce_audience: true, search_enabled: 'true'}}\n", "slack.search_enabled"),
        ("platforms: {slack: {enforce_audience: true, history_max_messages: '3'}}\n", "slack.history_max_messages"),
        ("platforms: {slack: {enforce_audience: true, history_scan_timeout_seconds: '3'}}\n", "slack.history_scan_timeout_seconds"),
        ("platforms: {slack: {enforce_audience: true}}\nscopes: {}\n", "scopes must be a list"),
        ("platforms: {slack: {enforce_audience: true}}\npeople: []\n", "people must be a mapping"),
        ("platforms: {slack: {enforce_audience: true}}\nroles: []\n", "roles must be a mapping"),
        ("platforms: {slack: {enforce_audience: true}}\nsecond_brain: []\n", "second_brain must be a mapping"),
        ("backends: []\n", "backends must be a mapping"),
        ("control: null\n", "control must be a mapping"),
        ("slack: [\n", "Invalid YAML"),
    ],
)
def test_invalid_explicit_file_fails(tmp_path, content: str, message: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BelleConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize("key", ["history_never_read", "history_exempt_members"])
def test_retired_history_settings_fail(tmp_path, key: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"platforms: {{slack: {{enforce_audience: true, {key}: []}}}}\n", encoding="utf-8")
    with pytest.raises(BelleConfigError, match=rf"slack\.{key} is no longer supported"):
        load_config(path)


def test_empty_scope_declarations_are_valid(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "platforms: {slack: {enforce_audience: true}}\nscopes: []\npeople:\nroles:\n",
        encoding="utf-8",
    )
    assert load_config(path) == {
        "platforms": {"slack": {"enforce_audience": True}},
        "scopes": [],
        "people": None,
        "roles": None,
    }


def test_missing_explicit_file_fails(tmp_path) -> None:
    with pytest.raises(BelleConfigError, match="Cannot read Belle config"):
        load_config(tmp_path / "missing.yaml")


def test_non_utf8_file_fails(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(b"\xff")
    with pytest.raises(BelleConfigError, match="Cannot read Belle config"):
        load_config(path)


def test_decision_env_values_are_resolved(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DECISION_API_BASE", "https://openrouter.example/api/v1")
    monkeypatch.setenv("DECISION_API_KEY", "secret")
    monkeypatch.setenv("DECISION_MODEL_NAME", "typesafe/jev-1.13")
    config = load_config(Path(__file__).resolve().parents[1] / "examples/config/belle-config.yaml")
    settings = decision_model(config)
    assert settings is not None
    assert settings.api == "openrouter"
    assert settings.api_key == "secret"
    assert settings.extra_body == {}


def test_embeddings_endpoint_env_values_are_resolved(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "secret")
    monkeypatch.delenv("EMBEDDINGS_MODEL", raising=False)
    path = tmp_path / "belle.yaml"
    path.write_text(
        "platforms: {slack: {enforce_audience: true}}\n"
        "second_brain: {retrieval: {embeddings: {api_base: https://embed.example/v1,"
        " api_key: '${EMBEDDINGS_API_KEY}', model: '${EMBEDDINGS_MODEL}'}}}\n",
        encoding="utf-8",
    )

    embeddings = load_config(path)["second_brain"]["retrieval"]["embeddings"]

    assert embeddings == {"api_base": "https://embed.example/v1", "api_key": "secret", "model": ""}


def test_named_task_models_resolve_env_and_selectors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TASK_API_KEY", "secret")
    path = tmp_path / "belle.yaml"
    path.write_text(
        "platforms: {slack: {enforce_audience: true}}\n"
        "models:\n"
        "  decision:\n"
        "    default: second\n"
        "    entries:\n"
        "      - {id: first, api: openrouter, model_name: first}\n"
        "      - {id: second, api: openai, api_base: https://example.com/v1, api_key: '${TASK_API_KEY}', model_name: decider}\n"
        "  summarizer:\n"
        "    entries:\n"
        "      - {id: documents, api: chat_completions, api_base: https://example.com/v1, api_key: '${TASK_API_KEY}', model_name: summary}\n"
        "second_brain: {ingest: {summarizer: {model: documents}}}\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert decision_model(config).model_name == "decider"
    assert config["models"]["summarizer"]["entries"][0]["api_key"] == "secret"


def test_summarizer_selector_overrides_group_default(tmp_path) -> None:
    path = tmp_path / "belle.yaml"
    path.write_text(
        "platforms: {slack: {enforce_audience: true}}\n"
        "models: {summarizer: {default: first, entries: "
        "[{id: first, api: chat_completions}, {id: second, api: chat_completions}]}}\n"
        "second_brain: {ingest: {summarizer: {model: second}}}\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert model_entry(config, "summarizer", config["second_brain"]["ingest"]["summarizer"]["model"])["id"] == "second"


def test_the_outline_selector_names_a_summarizer_entry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OUTLINE_ENTRY", "first")
    path = tmp_path / "belle.yaml"
    path.write_text(
        "platforms: {slack: {enforce_audience: true}}\n"
        "models: {summarizer: {default: second, entries: "
        "[{id: first, api: chat_completions}, {id: second, api: scaledown}]}}\n"
        "second_brain: {ingest: {summarizer: {outline_model: '${OUTLINE_ENTRY}'}}}\n",
        encoding="utf-8",
    )
    assert load_config(path)["second_brain"]["ingest"]["summarizer"]["outline_model"] == "first"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("missing", "Belle config second_brain.ingest.summarizer.outline_model must name a models.summarizer entry id"),
        ("[first]", "Belle config second_brain.ingest.summarizer.outline_model must be a string"),
    ],
)
def test_an_outline_selector_that_names_no_entry_is_refused(tmp_path, value, message) -> None:
    path = tmp_path / "belle.yaml"
    path.write_text(
        "platforms: {slack: {enforce_audience: true}}\n"
        "models: {summarizer: {entries: [{id: first, api: chat_completions}]}}\n"
        f"second_brain: {{ingest: {{summarizer: {{outline_model: {value}}}}}}}\n",
        encoding="utf-8",
    )
    with pytest.raises(BelleConfigError) as raised:
        load_config(path)
    assert str(raised.value) == message


def test_content_tool_model_roles(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TASK_API_KEY", "secret")
    path = tmp_path / "belle.yaml"
    path.write_text(
        "platforms: {slack: {enforce_audience: true}}\n"
        "models:\n"
        "  summarizer: {entries: [{id: summary, api: scaledown, api_key: '${TASK_API_KEY}'}]}\n"
        "  extractor: {entries: [{id: fields, api: openrouter, model_name: scaledown/extract}]}\n"
        "  classifier: {entries: [{id: labels, api: scaledown}]}\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert model_entry(config, "summarizer")["api_key"] == "secret"
    assert model_entry(config, "extractor")["model_name"] == "scaledown/extract"
    assert model_entry(config, "classifier")["api"] == "scaledown"


@pytest.mark.parametrize(
    "extra, message",
    [
        ("models: {decision: {entries: [{id: x, api: openrouter}, {id: y, api: openai}]}}\n", "models.decision.default"),
        ("models: {summarizer: {entries: [{id: x, api: chat_completions}, {id: x, api: chat_completions}]}}\n", "unique nonempty ids"),
        ("models: {summarizer: {entries: [{id: x, api: other}]}}\n", "models.summarizer.entries\\[x\\].api"),
        ("models: {extractor: {entries: [{id: x, api: other}]}}\n", "models.extractor.entries\\[x\\].api"),
        ("models: {classifier: {entries: [{id: x, api: openrouter}]}}\n", "models.classifier.entries\\[x\\].api"),
        ("models: {summarizer: {entries: [{id: x, api: chat_completions}]}}\nsecond_brain: {ingest: {summarizer: {model: missing}}}\n", "summarizer.model must name"),
        ("models: {summarizer: {entries: [{id: x, api: chat_completions}]}}\nsecond_brain: {ingest: {summarizer: {api_base: https://legacy}}}\n", "summarizer.api_base is not a setting"),
    ],
)
def test_task_model_config_rejects_ambiguous_or_invalid_entries(tmp_path, extra, message) -> None:
    path = tmp_path / "belle.yaml"
    path.write_text("platforms: {slack: {enforce_audience: true}}\n" + extra, encoding="utf-8")
    with pytest.raises(BelleConfigError, match=message):
        load_config(path)


def test_a_file_with_another_platform_needs_no_slack_section(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("platforms: {loopback: {enabled: true}}\nscopes: []\n", encoding="utf-8")
    assert load_config(path) == {"platforms": {"loopback": {"enabled": True}}, "scopes": []}


@pytest.mark.parametrize(
    "content, message",
    [
        ("platforms: {loopback: {enabled: true}, slack: {}}\n", "slack.enforce_audience"),
        ("platforms: {loopback: {enabled: true}, slack: }\n", "slack must be a mapping"),
        ("platforms: {loopback: {enabled: true}, slack: {enforce_audience: true, search_enabled: 1}}\n", "slack.search_enabled"),
    ],
)
def test_slack_settings_are_checked_beside_another_platform(tmp_path, content: str, message: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BelleConfigError, match=message):
        load_config(path)


def test_slack_settings_are_kept_beside_another_platform(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BELLE_TEST_BOT_TOKEN", "xoxb-test")
    path = tmp_path / "config.yaml"
    path.write_text(
        "platforms: {loopback: {enabled: true}, slack: {enforce_audience: false, bot_token: '${BELLE_TEST_BOT_TOKEN}'}}\n",
        encoding="utf-8",
    )
    assert load_config(path)["platforms"]["slack"] == {"enforce_audience": False, "bot_token": "xoxb-test"}


@pytest.mark.parametrize(
    "content, message",
    [
        *(
            (f"{name}: {{enabled: true}}\n", f"Belle config {name} is not a section. Platform sections go under platforms: platforms.{name}")
            for name in ("slack", "mattermost", "matrix", "loopback")
        ),
        (
            "platform: loopback\n",
            "Belle config platform is not a section. A platform section turns its platform on: platforms.<name>.enabled",
        ),
        (
            "decision: {api: openrouter}\n",
            "Belle config decision is not a section. The decision model goes under models: models.decision.entries",
        ),
        *(
            (
                f"platforms: {{slack: {{enforce_audience: true}}}}\nsecond_brain: {{ingest: {{summarizer: {{{key}: x}}}}}}\n",
                (
                    f"Belle config second_brain.ingest.summarizer.{key} is not a setting."
                    " The summarizer endpoint goes under models: models.summarizer.entries"
                ),
            )
            for key in ("api_base", "api_key", "client_provider")
        ),
    ],
)
def test_a_retired_setting_is_refused_with_its_place_now(tmp_path, content: str, message: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BelleConfigError) as raised:
        load_config(path)
    assert str(raised.value) == message


@pytest.mark.parametrize(
    "content, message",
    [
        ("platforms: [slack]\n", "platforms must be a mapping of platform names to their sections, or null"),
        ("platforms: {slak: {}}\n", "platforms.slak names no platform. The platforms are discord, loopback, matrix, mattermost, slack"),
        ("platforms: {slack: {}}\n", "slack.enforce_audience"),
    ],
)
def test_a_platforms_group_that_does_not_fit_is_refused(tmp_path, content: str, message: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BelleConfigError, match=message):
        load_config(path)


def test_an_empty_platforms_group_holds_no_section(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("platforms:\nscopes: []\n", encoding="utf-8")
    assert load_config(path) == {"platforms": None, "scopes": []}



def test_slack_validation_precedes_shared_validation(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("platforms: {slack: {enforce_audience: wrong}}\nmodels: {decision: wrong}\n")
    with pytest.raises(BelleConfigError, match=r"platforms.slack.enforce_audience must be a boolean"):
        load_config(path)


def test_platform_validator_receives_normalized_sections(tmp_path, monkeypatch):
    from belle_agent.platforms import slack

    seen = []
    monkeypatch.setattr(slack, "validate_config", lambda data, path: seen.append((data.copy(), path)))
    path = tmp_path / "config.yaml"
    path.write_text("platforms: {slack: {enforce_audience: true}}\n")
    load_config(path)
    assert seen == [({"platforms": {"slack": {"enforce_audience": True}}}, path)]


@pytest.mark.parametrize(("tools", "message"), [
    ({"bind": "0.0.0.0"}, "backends.opencode.tools.url must name the address clients use when the listener binds a wildcard address"),
    ({"bind": "::", "url": ""}, "backends.opencode.tools.url must name the address"),
    ({"url": "http://belle:18300/other"}, "backends.opencode.tools.url must be an HTTP URL ending in /mcp"),
    ({"url": "http://belle:notaport/mcp"}, "backends.opencode.tools.url must be an HTTP URL with a valid host and port"),
])
def test_a_tools_listener_that_clients_cannot_reach_is_refused_by_name(tools, message):
    with pytest.raises(BelleConfigError, match=message):
        listener_settings({"tools": tools}, "tools", "backends.opencode")


def test_a_wildcard_tools_listener_with_its_url_is_read():
    found = listener_settings({"tools": {"bind": "0.0.0.0", "url": "http://belle:18300/mcp"}}, "tools", "backends.opencode")
    assert found == {"tools_bind": "0.0.0.0", "tools_port": 0, "tools_url": "http://belle:18300/mcp"}


@pytest.mark.parametrize("key", ["history_scan_timeout_seconds", "history_members_cache_seconds"])
@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf", str(10**1000), str(-(10**1000))])
def test_slack_history_durations_must_be_finite(tmp_path, key: str, value: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"platforms: {{slack: {{enforce_audience: true, {key}: {value}}}}}\n")
    with pytest.raises(BelleConfigError, match=rf"slack\.{key} must be a finite number"):
        load_config(path)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("history_scan_timeout_seconds", "0.0001", "at least 0.001 seconds"),
        ("history_members_cache_seconds", "0.0001", "from 0.001 to 900 seconds"),
        ("history_members_cache_seconds", "900.1", "from 0.001 to 900 seconds"),
    ],
)
def test_slack_history_durations_reject_runtime_clamp_values(
    tmp_path, key: str, value: str, expected: str,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"platforms: {{slack: {{enforce_audience: true, {key}: {value}}}}}\n")

    with pytest.raises(BelleConfigError, match=rf"slack\.{key} must be a finite number {expected}"):
        load_config(path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("history_scan_timeout_seconds", "0.001"),
        ("history_members_cache_seconds", "0.001"),
        ("history_members_cache_seconds", "900"),
    ],
)
def test_slack_history_duration_boundaries_are_accepted(tmp_path, key: str, value: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"platforms: {{slack: {{enforce_audience: true, {key}: {value}}}}}\n")

    assert load_config(path)["platforms"]["slack"][key] == float(value)
