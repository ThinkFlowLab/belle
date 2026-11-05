"""``platforms.mattermost.servers``: several servers in one section, each with its own address, token and listener."""

import logging

import pytest

from belle_agent.config import BelleConfigError
from belle_agent.platforms.base import enabled_platforms
from belle_agent.platforms.mattermost.settings import MattermostSettings, read_servers

ONE = {"name": "one", "url": "${MM_URL_ONE}", "bot_token_env": "MM_TOKEN_ONE", "command_token_env": "MM_COMMAND_ONE",
       "actions": {"bind": "0.0.0.0", "port": 18401, "url": "http://belle:18401"}}
TWO = {"name": "two", "url": "https://two.example.com/", "bot_token_env": "MM_TOKEN_TWO",
       "actions": {"bind": "0.0.0.0", "port": 18402, "url": "http://belle:18402"}}


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    for name in ("MATTERMOST_URL", "MATTERMOST_BOT_TOKEN", "MATTERMOST_ACTIONS_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MM_URL_ONE", "http://one.example.com")
    monkeypatch.setenv("MM_TOKEN_ONE", "synthetic-token-one")
    monkeypatch.setenv("MM_COMMAND_ONE", "synthetic-command-one")
    monkeypatch.setenv("MM_TOKEN_TWO", "synthetic-token-two")


def _section(*servers, **keys):
    return {"platforms": {"mattermost": {"servers": list(servers), **keys}}}


def test_each_server_has_its_address_its_tokens_and_its_listener_and_shares_the_rest():
    one, two = read_servers(_section(ONE, TWO, failed_emoji="warning"))

    assert (one.server, one.url, one.token, one.token_env, one.command_token) == (
        "one", "http://one.example.com", "synthetic-token-one", "MM_TOKEN_ONE", "synthetic-command-one"
    )
    assert (one.actions_url, one.actions_port, two.actions_url, two.actions_port) == (
        "http://belle:18401", 18401, "http://belle:18402", 18402
    )
    assert (two.server, two.url, two.token, two.command_token) == ("two", "https://two.example.com", "synthetic-token-two", "")
    assert one.emoji["failed_emoji"] == two.emoji["failed_emoji"] == "warning"
    assert "synthetic-token" not in repr(one) + repr(two)


@pytest.mark.parametrize(
    ("config", "named"),
    [
        (_section(ONE, servers="x"), "mattermost.servers must be a list of mappings"),
        ({"platforms": {"mattermost": {"servers": [ONE, "two"]}}}, "mattermost.servers must be a list of mappings"),
        (_section({**TWO, "colour": "red"}), r"servers\[0\] has unknown keys: colour. A server takes actions,"),
        (_section({**TWO, "bot_token": "x"}), r"servers\[0\] holds bot_token: a secret is never written"),
        (_section({**TWO, "name": "Two"}), r"servers\[0\]\.name must be 1 to 32 lower-case letters"),
        (_section({**TWO, "name": "a:b"}), r"servers\[0\]\.name must be"),
        (_section({key: value for key, value in TWO.items() if key != "name"}), r"servers\[0\]\.name must be"),
        (_section({key: value for key, value in TWO.items() if key != "bot_token_env"}), r"servers\[0\]\.bot_token_env must be the name of an environment variable"),
        (_section({**TWO, "bot_token_env": "${MM_TOKEN_TWO}"}), r"servers\[0\]\.bot_token_env must be the name"),
        (_section({**TWO, "command_token_env": 3}), r"servers\[0\]\.command_token_env must be the name"),
        (_section({**TWO, "enabled": "yes"}), r"servers\[0\]\.enabled must be true, false or empty"),
        (_section({**TWO, "url": "two.example.com"}), r"servers\[0\]\.url must be the http or https address"),
        (_section({**TWO, "actions": {"url": "belle"}}), r"servers\[0\]\.actions\.url must be the http"),
        (_section(ONE, {**TWO, "name": "one"}), r"servers\[1\]\.name is the name of platforms.mattermost.servers\[0\] as well"),
        (_section(ONE, {**TWO, "bot_token_env": "MM_TOKEN_ONE"}), r"servers\[1\]\.bot_token_env is the bot_token_env of"),
        (_section(ONE, {**TWO, "url": "HTTP://one.example.com/"}), r"servers\[1\]\.url is the url of"),
        (_section(ONE, {**TWO, "actions": {"port": 18401}}), r"servers\[1\]\.actions\.port is the actions\.port of"),
        (_section(ONE, {**TWO, "actions": {"url": "http://belle:18401"}}), r"servers\[1\]\.actions\.url is the actions\.url of"),
        (_section(ONE, url="http://one.example.com"), r"mattermost\.url is set beside platforms\.mattermost\.servers"),
        (_section(ONE, actions={"url": "http://belle:18400"}), r"mattermost\.actions\.url is set beside"),
    ],
)
def test_a_list_that_does_not_fit_is_refused_with_the_entry_and_the_key_to_correct(config, named):
    with pytest.raises(BelleConfigError, match=named):
        read_servers(config)


def test_the_section_keys_the_example_writes_for_one_server_may_stay_empty_beside_the_list():
    section = _section(ONE, url="${MATTERMOST_URL}", actions={"bind": "0.0.0.0", "port": 18400, "url": "${MATTERMOST_ACTIONS_URL}"})

    assert [settings.server for settings in read_servers(section)] == ["one"]


# ── which servers are on, and the platform with them ─────────────────────


def test_a_server_with_neither_its_url_nor_its_token_is_off_and_the_others_are_on(monkeypatch, caplog):
    monkeypatch.delenv("MM_URL_ONE")
    monkeypatch.delenv("MM_TOKEN_ONE")

    with caplog.at_level(logging.INFO, logger="belle_agent.platforms.mattermost.settings"):
        assert [settings.server for settings in read_servers(_section(ONE, TWO))] == ["two"]

    assert "servers[0] (one) is off and is not connected: MM_URL_ONE and MM_TOKEN_ONE are not set" in caplog.text
    assert enabled_platforms(_section(ONE, TWO)) == ["mattermost"]


def test_a_disabled_server_is_off_whatever_it_holds():
    assert [settings.server for settings in read_servers(_section({**ONE, "enabled": False}, TWO))] == ["two"]


@pytest.mark.parametrize(
    ("unset", "entry", "named"),
    [
        ("MM_TOKEN_ONE", ONE, r"servers\[0\] cannot start: one of its url and its bot token is set, and MM_TOKEN_ONE is not set"),
        ("MM_URL_ONE", ONE, r"servers\[0\] cannot start: one of its url and its bot token is set, and MM_URL_ONE is not set"),
        ("MM_TOKEN_TWO", {**TWO, "enabled": True}, r"servers\[0\] cannot start: enabled is true, and MM_TOKEN_TWO is not set"),
    ],
)
def test_a_server_that_is_on_in_part_stops_the_start_and_names_what_is_missing(monkeypatch, unset, entry, named):
    monkeypatch.delenv(unset)

    for check in (read_servers, enabled_platforms):
        with pytest.raises(BelleConfigError, match=named):
            check(_section(entry))


def test_with_no_server_on_the_platform_is_off_and_says_why(monkeypatch):
    with pytest.raises(BelleConfigError, match=r"turns on no platform: platforms.mattermost \(a server of platforms.mattermost.servers whose url and bot token resolve is not set\)"):
        enabled_platforms(_section({**ONE, "enabled": False}))
    with pytest.raises(BelleConfigError, match="mattermost cannot start: enabled is true"):
        enabled_platforms(_section({**ONE, "enabled": False}, enabled=True))


def test_an_empty_list_keeps_the_section_s_one_server(monkeypatch):
    monkeypatch.setenv("MATTERMOST_URL", "http://chat.example.com")
    monkeypatch.setenv("MATTERMOST_BOT_TOKEN", "synthetic-bot-token")

    assert enabled_platforms({"platforms": {"mattermost": {"url": "${MATTERMOST_URL}", "servers": []}}}) == ["mattermost"]
    assert MattermostSettings("http://chat.example.com", "x").server == ""
