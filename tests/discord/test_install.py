import json
import urllib.error
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from belle_agent.config import BelleConfigError
from belle_agent.platforms.discord import install
from belle_agent.platforms.discord.connector import registered_toolkits
from belle_agent.platforms.discord.settings import MARK_EMOJI, TOKEN_ENV, read_settings


def _settings(**values):
    return read_settings(values, {TOKEN_ENV: "validation-only"})


@pytest.fixture(autouse=True)
def selected_config(monkeypatch, tmp_path):
    path = tmp_path / "belle.yaml"
    path.write_text("platforms: {discord: {}}\n", encoding="utf-8")
    monkeypatch.setattr(install, "selected_config_file", lambda: path)


def test_invite_url_encodes_scopes_and_uses_calculated_permissions():
    permissions = install.requested_permissions(_settings(), install._registered_tools(_settings()))
    query = parse_qs(urlparse(install.invite_url("123456789012345678", permissions)).query)
    assert query == {
        "client_id": ["123456789012345678"],
        "scope": ["bot applications.commands"],
        "permissions": [str(permissions)],
    }
    assert permissions == sum(1 << bit for bit in (6, 10, 11, 14, 15, 16, 18, 20, 34, 35, 36, 37, 38, 49, 51))
    assert permissions & (1 << 3) == 0  # Administrator is never requested.


@pytest.mark.parametrize("application_id", ["", "abc", "1" * 25])
def test_invite_refuses_invalid_application_id(application_id):
    with pytest.raises(ValueError, match="application_id"):
        install.invite_url(application_id, 0)


def test_post_tool_retains_its_permissions_with_cards_disabled():
    settings = _settings(activity_card=False, **{key: "disabled" for key in MARK_EMOJI})
    permissions = install.requested_permissions(settings, install._registered_tools(settings))
    assert permissions & (1 << 14)  # Rich embeds remain available through post_message.
    assert permissions & (1 << 6)  # react_to_message remains available without configured emoji.
    assert permissions & (1 << 15)  # post_message can upload inline attachments.
    assert permissions & (1 << 16)  # post_message verifies reply targets against history.
    assert permissions & (1 << 18)  # post_message and captions can use external custom emoji.
    assert permissions & (1 << 37)  # post_message accepts external sticker IDs.
    assert permissions & (1 << 49)  # post_message can send polls.
    reader_permissions = install.requested_permissions(settings, {"read_discord_conversation"})
    assert reader_permissions == (1 << 10) | (1 << 16) | (1 << 20)
    assert permissions & (1 << 10)
    assert permissions & (1 << 16)
    assert permissions & (1 << 20)  # read_discord_conversation supports voice channels.


def test_read_tools_include_channel_and_history_permissions_individually():
    names = {
        "read_discord_conversation", "find_by_name", "download_discord_attachment",
        "search_discord_guild", "read_discord_reactions", "read_pinned_messages",
        "list_discord_thread_members", "read_discord_poll",
    }
    for name in names:
        expected = (1 << 10) | (1 << 16) | ((1 << 20) if name in {
            "read_discord_conversation", "find_by_name", "download_discord_attachment",
            "search_discord_guild", "read_discord_poll",
        } else 0)
        assert install.requested_permissions(_settings(), {name}) == expected


@pytest.mark.parametrize(("name", "bits"), [
    ("react_to_message", (6, 10, 16, 18)),
    ("pin_message", (10, 16, 51)),
    ("send_discord_file", (10, 11, 15, 16, 18, 35, 38)),
    ("edit_message", (10, 16, 18)),
    ("delete_message", (10, 16)),
    ("list_discord_threads", (10, 16, 34)),
    ("end_discord_poll", (10, 16)),
    ("create_discord_thread", (10, 16, 35, 36, 38)),
    ("update_discord_thread", (10, 16, 34, 38)),
    ("change_discord_thread_member", (10, 16, 34, 38)),
])
def test_write_and_thread_tools_request_their_preflight_permissions_individually(name, bits):
    assert install.requested_permissions(_settings(), {name}) == sum(1 << bit for bit in bits)


def test_privileged_post_options_add_invite_permissions():
    settings = _settings(allow_everyone_mentions=True, allow_tts=True)
    permissions = install.requested_permissions(settings, install._registered_tools(settings))
    assert permissions & (1 << 17)
    assert permissions & (1 << 12)


def test_unmapped_registered_capability_refuses_to_hide_a_permission():
    with pytest.raises(ValueError, match="no Discord invite permission map"):
        install.requested_permissions(_settings(), {"future_tool"})


def test_registered_tools_cover_all_discord_toolkits():
    names = install._registered_tools(_settings())
    platform = SimpleNamespace(_settings=_settings())
    offered = [card["name"] for kit in registered_toolkits(platform, dict) for card in kit.get_tool_definitions()]
    assert len(offered) == len(set(offered))
    assert {"post_message", "send_discord_file", "edit_message", "delete_message",
            "react_to_message", "pin_message", "create_discord_thread",
            "update_discord_thread", "change_discord_thread_member"} <= names


def test_token_verification_returns_identity_and_never_includes_token(monkeypatch, capsys):
    secret = "synthetic-token-do-not-print"

    def response(request, timeout):
        assert request.get_header("Authorization") == f"Bot {secret}"
        assert request.get_header("User-agent") == install.USER_AGENT
        return BytesIO(json.dumps({"id": "42", "username": "Belle", "bot": True}).encode())

    monkeypatch.setattr(install.urllib.request, "urlopen", response)
    monkeypatch.setenv(TOKEN_ENV, secret)
    assert install.main(["--application-id", "123", "--verify-token"]) == 0
    output = capsys.readouterr().out
    assert "Verified bot: Belle (bot id 42)" in output
    assert secret not in output


def test_missing_and_denied_token_are_safe(monkeypatch, capsys):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    assert install.main(["--application-id", "123", "--verify-token"]) == 1
    assert TOKEN_ENV in capsys.readouterr().err

    def denied(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, BytesIO(b"secret response"))

    monkeypatch.setattr(install.urllib.request, "urlopen", denied)
    with pytest.raises(ValueError, match="HTTP 401") as error:
        install._verify_token("bad-token")
    assert "secret response" not in str(error.value)
    assert "bad-token" not in str(error.value)


def test_portal_intents_follow_config(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    assert install.main(["--application-id", "123"]) == 0
    output = capsys.readouterr().out
    assert "Privileged Gateway Intents: Message Content, Server Members (REST audience checks; Gateway member events are off)" in output
    config = """platforms:\n  discord:\n    message_content: false\n    guild_members: true\n"""
    path = tmp_path / "belle.yaml"
    path.write_text(config)
    try:
        assert install.main(["--application-id", "123", "--config", str(path)]) == 0
        output = capsys.readouterr().out
        assert "Privileged Gateway Intents: Server Members (REST audience checks and Gateway member events)" in output
        assert "Message Content, Server Members" not in output
    finally:
        path.unlink(missing_ok=True)


def test_named_bot_invite_requires_an_explicit_bot_and_no_credentials():
    config = {"platforms": {"discord": {"bots": [{"name": "east", "token_env": "EAST_TOKEN"},
                                                 {"name": "west", "token_env": "WEST_TOKEN"}]}}}
    with pytest.raises(BelleConfigError, match="--bot"):
        install._settings(config)
    assert install._settings(config, "west").message_content
