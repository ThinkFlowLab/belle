"""One deployment configuration starts a harness on its existing adapter or over ACP: ``BELLE_BACKEND`` alone selects."""

from pathlib import Path

import pytest

from belle_agent.config import load_config
from belle_agent.launcher import create_backend

EXAMPLES = Path(__file__).resolve().parents[2] / "examples/config"
#: What a deployment sets once, for both routes. Only BELLE_BACKEND changes between them.
DEPLOYMENT = {
    "OPENCODE_URL": "http://opencode:4096",
    "OPENCODE_SERVER_PASSWORD": "set-once",
    "DOCKER_AGENT_URL": "http://docker-agent:8080",
    "DOCKER_AGENT_AUTH_TOKEN": "set-once",
    "BELLE_RELAY_TOKEN": "set-once",
    "BELLE_TOOLS_URL": "http://belle:18300/mcp",
    "BELLE_TOOLS_TOKEN": "set-once",
    "BELLE_ACP_SOCKET": "/run/belle-acp/agent.sock",
}


@pytest.mark.parametrize(
    ("example", "harness"),
    [
        ("belle-opencode-loopback.yaml", "opencode"),
        ("belle-docker-agent-loopback.yaml", "docker_agent"),
        ("belle-config.yaml", "opencode"),
        ("belle-config.yaml", "docker_agent"),
    ],
)
def test_one_key_switches_a_harness_between_its_adapter_and_acp(monkeypatch, tmp_path, example, harness):
    for name, value in {**DEPLOYMENT, "BELLE_ACP_PROFILE": harness}.items():
        monkeypatch.setenv(name, value)
    built = {}
    for route in (harness, "acp"):
        monkeypatch.setenv("BELLE_BACKEND", route)
        backend = create_backend(load_config(EXAMPLES / example), tmp_path / route)
        built[route] = backend.capabilities().backend
    assert built == {harness: harness, "acp": "acp"}
