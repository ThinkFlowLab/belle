"""The Discord package before it is a platform: its settings, its ids, and that nothing can start it."""

import pytest

from belle_agent.config import BelleConfigError
from belle_agent.platforms.base import enabled_platforms
from belle_agent.platforms.discord.capabilities import CHANNEL_TYPES, DISCORD_CAPABILITIES
from belle_agent.platforms.discord.ids import discord_id_error
from belle_agent.platforms.discord.settings import TOKEN_ENV, read_settings

TOKEN = "synthetic-discord-token"
GUILD = "1000000000000000116"


def test_the_token_comes_from_the_environment_and_never_shows():
    settings = read_settings({}, {TOKEN_ENV: TOKEN})

    assert (settings.token, settings.message_content, settings.guild_members) == (TOKEN, True, False)
    assert TOKEN not in repr(settings)
    assert settings.pending_events_max == 1024


def test_single_bot_token_rejects_surrounding_whitespace():
    with pytest.raises(BelleConfigError, match=f"{TOKEN_ENV} must have no surrounding whitespace"):
        read_settings({}, {TOKEN_ENV: f" {TOKEN} "})


@pytest.mark.parametrize("channels", [
    [], {"general": "1000000000000000116"}, {"1000000000000000116": "general"},
    {1000000000000000116: "1000000000000000117"}, {"1000000000000000116": 1000000000000000117},
])
def test_member_event_channels_must_map_snowflake_guilds_to_channel_ids(channels):
    with pytest.raises(BelleConfigError, match="member_event_channels"):
        read_settings({"member_event_channels": channels}, {TOKEN_ENV: TOKEN})


def test_member_event_channels_are_read():
    channels = {"1000000000000000116": "1000000000000000117"}
    settings = read_settings({"member_event_channels": channels}, {TOKEN_ENV: TOKEN})
    channels["1000000000000000116"] = "1000000000000000118"

    assert settings.member_event_channels == {"1000000000000000116": "1000000000000000117"}


@pytest.mark.parametrize("key", ["acknowledgement_emoji", "brain_react_success"])
def test_custom_emoji_settings_require_a_discord_name_and_id(key):
    value = "Beacon_2:1000000000000000117"
    settings = read_settings({key: value}, {TOKEN_ENV: TOKEN})
    assert settings.emoji[key] == value

    for invalid in ("belle:123", "x:1000000000000000117", "bad-name:1000000000000000117"):
        with pytest.raises(BelleConfigError, match=f"platforms.discord.{key} custom emoji"):
            read_settings({key: invalid}, {TOKEN_ENV: TOKEN})


def test_unicode_and_disabled_emoji_settings_keep_their_behavior():
    settings = read_settings({"acknowledgement_emoji": "✨", "completed_emoji": "disabled"}, {TOKEN_ENV: TOKEN})

    assert settings.emoji["acknowledgement_emoji"] == "✨"
    assert settings.emoji["completed_emoji"] == ""


@pytest.mark.parametrize(
    ("section", "environ", "named"),
    [
        ({"token": TOKEN}, {TOKEN_ENV: TOKEN}, "platforms.discord.token is refused"),
        ({"intents": []}, {TOKEN_ENV: TOKEN}, "platforms.discord.intents is not a setting"),
        ({"message_content": "yes"}, {TOKEN_ENV: TOKEN}, "platforms.discord.message_content must be true or false"),
        ({}, {}, TOKEN_ENV),
    ],
)
def test_a_setting_that_does_not_fit_is_refused_by_name(section, environ, named):
    with pytest.raises(BelleConfigError, match=named):
        read_settings(section, environ)


def test_an_id_argument_is_checked_as_a_snowflake():
    assert discord_id_error({"chat_id": GUILD, "message_id": ""}) is None
    code, detail = discord_id_error({"chat_id": "general"})
    assert "chat_id" in code + detail and "17 to 20 digits" in detail


def test_every_channel_type_but_a_group_dm_has_a_word_scopes_match():
    assert 3 not in CHANNEL_TYPES
    assert set(CHANNEL_TYPES.values()) == set(DISCORD_CAPABILITIES.axis_values["chat_type"])


def test_the_discord_section_turns_on_only_where_the_bot_token_is_set(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(BelleConfigError, match=f"platforms.discord \\({TOKEN_ENV} is not set\\)"):
        enabled_platforms({"platforms": {"discord": {}}})
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    assert enabled_platforms({"platforms": {"discord": {}}}) == ["discord"]


def test_the_surfaces_of_a_running_turn_are_read_with_their_defaults_and_their_forms():
    environ = {TOKEN_ENV: "t"}

    found = read_settings({"enable_streaming": True, "activity_card_delay_seconds": 0}, environ)
    default = read_settings({}, environ)

    assert (found.enable_streaming, found.activity_card, found.activity_card_delay_seconds) == (True, True, 0.0)
    assert (default.enable_streaming, default.activity_card_delay_seconds, default.activity_card_min_edit_seconds) == (
        False, 5.0, 10.0)
    with pytest.raises(BelleConfigError, match="activity_card_min_edit_seconds must be a number of seconds, 0 or more"):
        read_settings({"activity_card_min_edit_seconds": -1}, environ)
    with pytest.raises(BelleConfigError, match="enable_streaming must be true or false"):
        read_settings({"enable_streaming": "yes"}, environ)


def test_question_lifetime_limits_are_read_and_invalid_limits_name_the_setting():
    settings = read_settings({"pending_questions_max": 12, "question_expiry_seconds": 60}, {TOKEN_ENV: "t"})
    assert (settings.pending_questions_max, settings.question_expiry_seconds) == (12, 60)
    with pytest.raises(BelleConfigError, match="pending_questions_max must be a whole number, 1 or more"):
        read_settings({"pending_questions_max": 0}, {TOKEN_ENV: "t"})


@pytest.mark.parametrize("bots", [[], [{}], [{"name": "", "token_env": "FIRST"}],
    [{"name": "a:b", "token_env": "FIRST"}], [{"name": "a", "token": TOKEN}],
    [{"name": "a", "token_env": "FIRST"}, {"name": "a", "token_env": "SECOND"}],
    [{"name": "a", "token_env": "FIRST"}, {"name": "b", "token_env": "FIRST"}]])
def test_named_bot_entries_refuse_ambiguous_or_secret_configuration(bots):
    from belle_agent.platforms.discord.settings import read_bots

    with pytest.raises(BelleConfigError) as caught:
        read_bots({"bots": bots}, {"FIRST": TOKEN, "SECOND": "other"})
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("environ", [{}, {"FIRST": ""}, {"FIRST": " t "}, {"FIRST": TOKEN, "SECOND": TOKEN}])
def test_named_bots_require_distinct_nonempty_tokens(environ):
    from belle_agent.platforms.discord.settings import read_bots

    with pytest.raises(BelleConfigError) as caught:
        read_bots({"bots": [{"name": "a", "token_env": "FIRST"}, {"name": "b", "token_env": "SECOND"}]}, environ)
    assert TOKEN not in str(caught.value)


def test_named_bots_resolve_only_their_explicit_environment_reference(monkeypatch):
    from pathlib import Path

    from belle_agent.platforms.discord import validate_config
    from belle_agent.platforms.discord.connector import create_platform

    section = {"bots": [{"name": "a", "token_env": "FIRST"}, {"name": "b", "token_env": "SECOND"}]}
    validate_config({"platforms": {"discord": section}}, Path("unused"))
    monkeypatch.setenv("FIRST", "secret:first:private")
    monkeypatch.setenv("SECOND", "secret:second:private")
    port = create_platform({"platforms": {"discord": section}})
    assert list(port.members) == ["a", "b"]
    assert [member._settings.token for member in port.members.values()] == ["secret:first:private", "secret:second:private"]
    assert "secret:first:private" not in repr(port.members["a"]._settings)


def test_config_load_rejects_duplicate_resolved_tokens_without_disclosing_them(monkeypatch):
    from pathlib import Path

    from belle_agent.platforms.discord import validate_config

    monkeypatch.setenv("FIRST", TOKEN)
    monkeypatch.setenv("SECOND", TOKEN)
    section = {"bots": [{"name": "a", "token_env": "FIRST"}, {"name": "b", "token_env": "SECOND"}]}
    with pytest.raises(BelleConfigError, match="duplicate bot tokens") as caught:
        validate_config({"platforms": {"discord": section}}, Path("unused"))
    assert TOKEN not in str(caught.value)


def test_pending_event_limit_is_read():
    settings = read_settings({"pending_events_max": 2}, {TOKEN_ENV: TOKEN})
    assert settings.pending_events_max == 2


def test_file_download_limit_is_read():
    settings = read_settings({"file_download_max_bytes": 8}, {TOKEN_ENV: TOKEN})
    assert settings.file_download_max_bytes == 8


@pytest.mark.parametrize("value", [0, True, "8"])
def test_file_download_limit_refuses_invalid_values(value):
    with pytest.raises(BelleConfigError, match="platforms.discord.file_download_max_bytes must be a whole number, 1 or more"):
        read_settings({"file_download_max_bytes": value}, {TOKEN_ENV: TOKEN})


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "2", [], {}])
def test_pending_event_limit_refuses_invalid_values(value):
    with pytest.raises(BelleConfigError, match="platforms.discord.pending_events_max must be a whole number, 1 or more"):
        read_settings({"pending_events_max": value}, {TOKEN_ENV: TOKEN})
