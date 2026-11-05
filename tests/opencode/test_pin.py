"""The pin: the version in the image recipe is the version the server reports, and the relay is loaded."""

import re
from pathlib import Path

import pytest

from belle_agent.integrations.opencode.pin import OPENCODE_VERSION

CONTAINERFILE = Path(__file__).parents[2] / "containers" / "opencode" / "Containerfile"


def pinned() -> dict[str, str]:
    """The ARG defaults of the image recipe, which is where the pin is recorded."""
    return dict(re.findall(r"^ARG (OPENCODE_[A-Z0-9]+)=(\S+)$", CONTAINERFILE.read_text(), re.MULTILINE))


def test_the_adapter_and_the_recipe_pin_the_same_version():
    assert OPENCODE_VERSION == pinned()["OPENCODE_VERSION"]


def test_the_recipe_pins_a_version_a_target_and_a_checksum():
    pin = pinned()
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin["OPENCODE_VERSION"])
    assert pin["OPENCODE_TARGET"]
    assert re.fullmatch(r"[0-9a-f]{128}", pin["OPENCODE_SHA512"])


@pytest.mark.integration
def test_the_server_reports_the_pinned_version(client):
    status, info = client.call("GET", "/api/info")
    assert status == 200
    assert info["version"] == pinned()["OPENCODE_VERSION"]


@pytest.mark.integration
def test_the_relay_plugin_is_loaded(client):
    # The same call loads a project plugin, which loads with its location.
    sources = {plugin["id"]: plugin["source"]["type"] for plugin in client.api("GET", "/api/plugin")}
    assert sources.get("belle.relay") not in (None, "builtin"), sources
