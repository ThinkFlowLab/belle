"""The core imports no integration, and the seam loads with no host library."""

import ast
import json
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src/belle_agent"
SUITE = Path(__file__).resolve().parent
HOST_LIBRARIES = ("jiuwenswarm", "openjiuwen", "acp")
COMPOSITION_ROOT = "launcher.py"
SEAM = (
    "belle_agent.launcher",
    "belle_agent.backend",
    "belle_agent.tools.host",
    "belle_agent.tools.server",
    "belle_agent.host",
    "belle_agent.platforms.base",
    "belle_agent.platforms.loopback",
    "belle_agent.second_brain.hosted",
    "belle_agent.second_brain.lexical",
    "conformance_kit",
    "scripted_backend",
)


def _imports(path: Path) -> set[str]:
    """Name every module a file imports, with relative imports made absolute."""
    package = ".".join(("belle_agent", *path.relative_to(SOURCE).parent.parts))
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package.rsplit(".", node.level - 1)[0] if node.level else ""
            module = ".".join(part for part in (base, node.module or "") if part)
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def _importers(prefix: str, *, outside: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        if relative.startswith((outside, "skills/")):
            continue
        hits = sorted(name for name in _imports(path) if name == prefix or name.startswith(f"{prefix}."))
        if hits:
            found[relative] = hits
    return found


def test_nothing_outside_integrations_imports_an_integration():
    found = _importers("belle_agent.integrations", outside="integrations/")
    assert not found, f"the core and the platforms must not import an integration: {found}"


def test_nothing_imports_the_composition_root():
    found = _importers("belle_agent.launcher", outside=COMPOSITION_ROOT)
    assert not found, f"the launcher is started, never imported: a module that imports it names every backend: {found}"


def test_nothing_outside_integrations_imports_a_host_library():
    found = {
        library: importers
        for library in HOST_LIBRARIES
        if (importers := _importers(library, outside="integrations/"))
    }
    assert not found, f"only an integration may import a host library: {found}"


def test_nothing_outside_the_loopback_package_imports_it():
    found = _importers("belle_agent.platforms.loopback", outside="platforms/loopback/")
    assert not found, f"the loopback platform is selected by configuration, never imported: {found}"


def test_the_seam_and_the_suite_load_with_no_host_library_importable():
    script = f"""
import importlib, sys

class Refuse:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {HOST_LIBRARIES!r}:
            raise ImportError(f"{{name}} is not importable in this check")

sys.meta_path.insert(0, Refuse())
sys.path.insert(0, {str(SUITE)!r})
for name in {SEAM!r}:
    importlib.import_module(name)
from belle_agent.launcher import second_brain_backend
assert second_brain_backend({{"second_brain": {{"enabled": True}}}}) is not None
loaded = sorted(name for name in sys.modules if name.split(".")[0] in {HOST_LIBRARIES!r})
assert not loaded, loaded
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_the_opencode_integration_imports_no_host_library_and_no_other_integration():
    own = "belle_agent.integrations.opencode"
    found = {}
    for path in sorted((SOURCE / "integrations/opencode").rglob("*.py")):
        foreign = sorted(
            name
            for name in _imports(path)
            if name.split(".")[0] in HOST_LIBRARIES
            or (name.startswith("belle_agent.integrations.") and not name.startswith(own))
        )
        if foreign:
            found[path.name] = foreign
    assert not found, f"the OpenCode integration imports the core and nothing of another backend: {found}"


def test_the_slack_platform_imports_no_host_library_and_no_integration():
    found = {}
    for path in sorted((SOURCE / "platforms/slack").rglob("*.py")):
        foreign = sorted(
            name
            for name in _imports(path)
            if name.split(".")[0] in HOST_LIBRARIES or name.startswith("belle_agent.integrations")
        )
        if foreign:
            found[path.name] = foreign
    assert not found, f"the Slack platform is what every harness shares, and imports none of them: {found}"


def test_the_slack_connector_loads_and_builds_with_no_host_library_importable():
    script = f"""
import os, sys

class Refuse:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {HOST_LIBRARIES!r}:
            raise ImportError(f"{{name}} is not importable in this check")

sys.meta_path.insert(0, Refuse())
os.environ.update(SLACK_BOT_TOKEN="xoxb-11-22-abcdef", SLACK_APP_TOKEN="xapp-1-A11-22-abcdef")
from belle_agent.platforms.base import create_platform
platform = create_platform({{"platforms": {{"slack": {{"enforce_audience": True}}}}}}, "slack")
assert type(platform).__module__ == "belle_agent.platforms.slack.connector"
assert platform.toolkits(dict)
loaded = sorted(name for name in sys.modules if name.split(".")[0] in {HOST_LIBRARIES!r} or name.startswith("belle_agent.integrations"))
assert not loaded, loaded
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def _host_on_opencode(second_brain: str) -> dict:
    """Build a host on the OpenCode adapter in a fresh interpreter, and report what that loaded."""
    script = f"""
import json, sys, tempfile
from pathlib import Path
from belle_agent.host import host_from_config
from belle_agent.integrations.opencode import OpenCodeBackend
{second_brain}
directory = tempfile.mkdtemp()
config = {{"platforms": {{"loopback": {{"enabled": True}}}}, "second_brain": {{"enabled": True, "corpus_dir": directory + "/corpus"}}}}
host = host_from_config(config, OpenCodeBackend("http://opencode.invalid", "none"), state_dir=Path(directory), second_brain=backend)
print(json.dumps({{
    "tools": [tool["name"] for tool in host.tools.definitions("")],
    "belle": sorted(name for name in sys.modules if name.split(".")[0] == "belle_agent"),
    "library": sorted(name for name in sys.modules if name.split(".")[0] in {HOST_LIBRARIES!r}),
}}))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_the_shared_second_brain_backend_loads_without_a_host_library():
    loaded = _host_on_opencode("from belle_agent.second_brain.default_backend import SECOND_BRAIN_BACKEND as backend")

    assert loaded["library"] == []
    assert {"brain_search", "brain_read", "brain_list"} <= set(loaded["tools"])
    assert {"belle_agent.integrations.opencode.adapter", "belle_agent.host", "belle_agent.tools.server"} <= set(loaded["belle"])
    assert "belle_agent.platforms.loopback.platform" in loaded["belle"]


def test_with_no_second_brain_backend_the_library_is_not_imported():
    loaded = _host_on_opencode("backend = None")

    assert loaded["library"] == []
    # The second brain is enabled all the same, and its tools are served to say that nobody looked.
    assert {"brain_search", "brain_read", "brain_list"} <= set(loaded["tools"])
