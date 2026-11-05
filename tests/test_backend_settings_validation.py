"""Backend settings reject malformed relay sections before reading their contents."""

import importlib

import pytest

from belle_agent.config import BelleConfigError


@pytest.mark.parametrize("backend", ["opencode", "docker_agent"])
@pytest.mark.parametrize("relay", [None, 1, "port", ["port"]])
def test_relay_requires_a_mapping_before_secret_checks(backend, relay, tmp_path):
    settings = importlib.import_module(f"belle_agent.integrations.{backend}.settings")
    config = {"backends": {backend: {"url": "http://backend.invalid", "relay": relay}}}

    with pytest.raises(BelleConfigError, match=rf"backends\.{backend}\.relay must be a mapping of bind, port"):
        settings.create_backend(config, tmp_path)
