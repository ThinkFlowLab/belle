"""Which platforms a configuration turns on: each section by its credentials, or the deprecated platform setting."""

from pathlib import Path

import pytest

from belle_agent.config import BelleConfigError, load_config
from belle_agent.platforms.base import enabled_platforms

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/config/belle-config.yaml"
VARIABLES = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "MATTERMOST_URL", "MATTERMOST_BOT_TOKEN")
SLACK = {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1"}
MATTERMOST = {"MATTERMOST_URL": "http://chat.example.com", "MATTERMOST_BOT_TOKEN": "synthetic"}
SECTIONS = "platforms:\n  slack: {enforce_audience: true}\n  mattermost: {url: '${MATTERMOST_URL}'}\n"


@pytest.fixture(autouse=True)
def _unset(monkeypatch) -> None:
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _load(tmp_path, text: str) -> dict:
    path = tmp_path / "belle.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


@pytest.mark.parametrize(
    "variables, enabled",
    [({**SLACK}, ["slack"]), ({**MATTERMOST}, ["mattermost"]), ({**SLACK, **MATTERMOST}, ["slack", "mattermost"])],
)
def test_the_example_turns_on_each_platform_whose_credentials_are_set(monkeypatch, variables, enabled) -> None:
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    assert enabled_platforms(load_config(EXAMPLE)) == enabled


def test_a_configuration_with_no_credentials_names_each_section_and_what_it_lacks() -> None:
    with pytest.raises(BelleConfigError) as raised:
        enabled_platforms(load_config(EXAMPLE))
    assert str(raised.value) == (
        "Belle config turns on no platform: platforms.slack (SLACK_BOT_TOKEN, SLACK_APP_TOKEN are not set);"
        " platforms.mattermost (MATTERMOST_URL, MATTERMOST_BOT_TOKEN are not set);"
        " platforms.matrix (MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_STORE_PASSPHRASE are not set);"
        " platforms.discord (DISCORD_BOT_TOKEN is not set)"
    )


def test_a_section_with_some_of_its_credentials_stops_the_start_naming_what_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MATTERMOST_URL", MATTERMOST["MATTERMOST_URL"])
    with pytest.raises(BelleConfigError, match="platforms.mattermost cannot start: some of its credentials are set, and MATTERMOST_BOT_TOKEN is not set"):
        enabled_platforms(_load(tmp_path, SECTIONS))


@pytest.mark.parametrize(
    "text, enabled",
    [
        # false turns a configured section off.
        ("platforms: {slack: {enforce_audience: true, enabled: false}, mattermost: {url: '${MATTERMOST_URL}'}}\n", ["mattermost"]),
        # A section with no credentials, such as loopback, is on only with true.
        ("platforms: {loopback: {}, mattermost: {url: '${MATTERMOST_URL}'}}\n", ["mattermost"]),
        (
            "platforms: {loopback: {enabled: true}, mattermost: {url: '${MATTERMOST_URL}', enabled: true}}\n",
            ["loopback", "mattermost"],
        ),
        ("platforms:\n  loopback:\n    enabled: true\n", ["loopback"]),
    ],
)
def test_enabled_overrides_what_the_credentials_say(tmp_path, monkeypatch, text, enabled) -> None:
    for name, value in {**SLACK, **MATTERMOST}.items():
        monkeypatch.setenv(name, value)
    assert enabled_platforms(_load(tmp_path, text)) == enabled


@pytest.mark.parametrize(
    "text, message",
    [
        ("platforms: {mattermost: {url: '${MATTERMOST_URL}', enabled: true}}\n", "enabled is true, and MATTERMOST_URL, MATTERMOST_BOT_TOKEN are not set"),
        ("platforms: {mattermost: {enabled: yes please}}\n", "mattermost.enabled must be true, false or empty"),
        ("platforms: {mattermost: []}\n", "mattermost must be a mapping"),
        ("platforms: {loopback: {}}\n", r"no platform: platforms.loopback \(it has no credentials, so only enabled: true turns it on\)"),
        ("scopes: []\n", "it has no platform section, such as platforms.discord or platforms.loopback or platforms.matrix or platforms.mattermost or platforms.slack"),
    ],
)
def test_a_configuration_that_turns_on_nothing_usable_is_refused(tmp_path, text, message) -> None:
    with pytest.raises(BelleConfigError, match=message):
        enabled_platforms(_load(tmp_path, text))


@pytest.mark.parametrize("example", ["belle-opencode-loopback.yaml", "belle-docker-agent-loopback.yaml"])
def test_each_loopback_example_turns_on_loopback(example) -> None:
    config = load_config(EXAMPLE.parent / example)
    assert enabled_platforms(config) == ["loopback"]
    assert config["platforms"]["loopback"] == {"enabled": True, "control": "${BELLE_LOOPBACK_CONTROL}"}

