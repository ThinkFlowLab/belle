"""The Mattermost package imports the core, and no integration, host library or other platform."""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "conformance"))

from test_import_guard import HOST_LIBRARIES, SOURCE, _imports

from belle_agent.platforms.base import platform_names

PACKAGE = "belle_agent.platforms.mattermost"
#: The other platform packages. A module of ``belle_agent.platforms`` that is no package is core.
OTHERS = tuple(f"belle_agent.platforms.{name}" for name in platform_names() if name != "mattermost")


def test_the_mattermost_platform_imports_no_host_library_no_integration_and_no_other_platform():
    found = {}
    for path in sorted((SOURCE / "platforms/mattermost").rglob("*.py")):
        foreign = sorted(
            name
            for name in _imports(path)
            if name.split(".")[0] in HOST_LIBRARIES
            or name.startswith("belle_agent.integrations")
            or name.startswith(OTHERS)
        )
        if foreign:
            found[path.name] = foreign
    assert not found, f"the Mattermost platform imports the core only: {found}"


def test_nothing_outside_the_package_and_the_integrations_imports_it():
    found = {}
    for path in sorted(SOURCE.rglob("*.py")):
        # A host integration builds the platform where its host runs one.
        if path.is_relative_to(SOURCE / "platforms/mattermost") or path.is_relative_to(SOURCE / "integrations"):
            continue
        hits = sorted(name for name in _imports(path) if name.startswith(PACKAGE))
        if hits:
            found[path.relative_to(SOURCE).as_posix()] = hits
    assert not found, f"the core selects the Mattermost platform by configuration, and never imports it: {found}"
