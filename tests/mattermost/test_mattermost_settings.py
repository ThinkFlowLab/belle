"""The ``mattermost`` section, and the bot token from the environment only."""

import logging
from pathlib import Path

import pytest

from belle_agent.config import BelleConfigError, load_config
from belle_agent.platforms.mattermost import settings as mattermost_settings
from belle_agent.platforms.mattermost.settings import TOKEN_ENV, URL_ENV, read_settings

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/config/belle-config.yaml"

TOKEN = "synthetic-bot-token-0123456789"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.delenv(URL_ENV, raising=False)


def test_the_url_comes_from_its_variable_and_the_token_from_the_environment(monkeypatch):
    monkeypatch.setenv(URL_ENV, "https://chat.example.com/")
    settings = read_settings({"platforms": {"mattermost": {"url": "${MATTERMOST_URL}"}}})

    assert (settings.url, settings.token) == ("https://chat.example.com", TOKEN)
    assert TOKEN not in repr(settings)


@pytest.mark.parametrize("config", [{}, {"platforms": {"mattermost": None}}, {"platforms": {"mattermost": {"url": "${MATTERMOST_URL}"}}}])
def test_a_missing_url_names_the_key_and_the_variable(config):
    with pytest.raises(BelleConfigError, match=rf"mattermost\.url is empty.*{URL_ENV}"):
        read_settings(config)


def test_a_missing_token_names_its_variable(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV)
    with pytest.raises(BelleConfigError, match=TOKEN_ENV):
        read_settings({"platforms": {"mattermost": {"url": "http://mattermost:8065"}}})


@pytest.mark.parametrize(
    ("section", "named"),
    [
        ({"url": "http://mattermost:8065", "token": "x"}, "token"),
        ({"url": "http://mattermost:8065", "colour": "red"}, "unknown keys: colour"),
        ({"url": "mattermost:8065"}, "http or https address"),
        ({"url": "http://bot:secret@mattermost:8065"}, "holds credentials"),
        ("http://mattermost:8065", "must be a mapping"),
        ({"url": "http://mattermost:8065", "failed_emoji": ":x:"}, r"mattermost\.failed_emoji must be an emoji name"),
        ({"url": "http://mattermost:8065", "queued_emoji": 3}, r"mattermost\.queued_emoji must be an emoji name"),
        ({"url": "http://mattermost:8065", "actions": {"url": "belle:18400"}}, r"mattermost\.actions\.url must be the http"),
        ({"url": "http://mattermost:8065", "actions": {"port": "many"}}, r"mattermost\.actions\.port must be a port"),
        ({"url": "http://mattermost:8065", "actions": {"path": "/x"}}, r"mattermost\.actions is a mapping of bind"),
        ({"url": "http://mattermost:8065", "history_max_total_chars": 0}, r"history_max_total_chars must be a positive integer"),
        ({"url": "http://mattermost:8065", "history_max_total_chars": "lots"}, r"history_max_total_chars must be a positive integer"),
        ({"url": "http://mattermost:8065", "directory_scan_max_pages": 0}, r"directory_scan_max_pages must be a positive integer"),
        ({"url": "http://mattermost:8065", "audience_member_max_pages": 0}, r"audience_member_max_pages must be a positive integer"),
        ({"url": "http://mattermost:8065", "gap_reaction_scan_max_pages": 0}, r"gap_reaction_scan_max_pages must be a positive integer"),
        ({"url": "http://mattermost:8065", "gap_reaction_scan_max_pages": True}, r"gap_reaction_scan_max_pages must be a positive integer"),
        ({"url": "http://mattermost:8065", "file_download_max_bytes": 0}, r"file_download_max_bytes must be a positive integer"),
        ({"url": "http://mattermost:8065", "file_download_max_bytes": True}, r"file_download_max_bytes must be a positive integer"),
        ({"url": "http://mattermost:8065", "file_upload_max_bytes": 0}, r"file_upload_max_bytes must be a positive integer"),
    ],
)
def test_a_section_that_does_not_fit_is_refused_with_what_to_correct(section, named):
    with pytest.raises(BelleConfigError, match=named):
        read_settings({"platforms": {"mattermost": section}})


def test_directory_scan_budget_is_read_from_config():
    settings = read_settings({"platforms": {"mattermost": {
        "url": "http://mattermost:8065", "directory_scan_max_pages": 26,
    }}})
    assert settings.directory_scan_max_pages == 26


def test_text_budget_has_no_extra_belle_ceiling():
    settings = read_settings({"platforms": {"mattermost": {
        "url": "http://mattermost:8065", "history_max_total_chars": 600_000,
    }}})
    assert settings.history_max_total_chars == 600_000


def test_audience_member_budget_is_read_from_config():
    settings = read_settings({"platforms": {"mattermost": {
        "url": "http://mattermost:8065", "audience_member_max_pages": 31,
    }}})
    assert settings.audience_member_max_pages == 31


def test_the_pages_a_gap_s_reactions_are_read_from_are_read_from_config():
    assert read_settings({"platforms": {"mattermost": {"url": "http://mattermost:8065"}}}).gap_reaction_scan_max_pages == 1
    settings = read_settings({"platforms": {"mattermost": {
        "url": "http://mattermost:8065", "gap_reaction_scan_max_pages": 3,
    }}})
    assert settings.gap_reaction_scan_max_pages == 3


def test_file_download_budget_is_read_from_config():
    settings = read_settings({"platforms": {"mattermost": {
        "url": "http://mattermost:8065", "file_download_max_bytes": 41,
    }}})
    assert settings.file_download_max_bytes == 41


def test_no_refusal_and_no_log_line_holds_the_token(caplog):
    with caplog.at_level(logging.DEBUG, logger="belle_agent"), pytest.raises(BelleConfigError) as refused:
        read_settings({"platforms": {"mattermost": {"url": "ftp://mattermost"}}})

    assert TOKEN not in str(refused.value)
    assert TOKEN not in caplog.text


def test_the_example_configuration_lists_every_key_of_the_section():
    assert set(load_config(EXAMPLE)["platforms"]["mattermost"]) == mattermost_settings._KEYS


def test_the_listener_address_comes_from_its_variable_and_is_off_without_it(monkeypatch):
    section = {"url": "http://mattermost:8065", "actions": {"bind": "0.0.0.0", "port": 18400, "url": "${MATTERMOST_ACTIONS_URL}"}}
    monkeypatch.delenv("MATTERMOST_ACTIONS_URL", raising=False)
    assert read_settings({"platforms": {"mattermost": section}}).actions_url == ""

    monkeypatch.setenv("MATTERMOST_ACTIONS_URL", "http://belle:18400/")
    settings = read_settings({"platforms": {"mattermost": section}})
    assert (settings.actions_url, settings.actions_bind, settings.actions_port) == ("http://belle:18400", "0.0.0.0", 18400)
