"""Backend command requirements are independent of platform payloads."""

import pytest

from belle_agent.backend import CATALOG_KINDS, SCOPE_FIELDS, BackendCapabilities
from belle_agent.host.commands import HostCommands


@pytest.mark.parametrize(
    ("words", "catalog", "fields", "expected"),
    [
        (("skills", "list"), (), (), "the test backend lists no skills"),
        (("skills", "use"), ("skills",), (), "the test backend cannot require a skill of one turn"),
        (("subagents", "use"), ("subagents",), (), "the test backend cannot require a subagent of one turn"),
        (("experts", "list"), ("experts",), (), "the test backend applies no expert to a turn"),
        (("models", "reset"), ("models",), (), "the test backend cannot change the model of a conversation"),
        (("models", "current"), ("models",), (), ""),
        (("skills", "use"), CATALOG_KINDS, SCOPE_FIELDS, ""),
        (("stop",), (), (), ""),
    ],
)
def test_command_requirements(words, catalog, fields, expected):
    host = HostCommands()
    host._capabilities = BackendCapabilities(
        backend="test",
        catalog=frozenset(catalog),
        scope_fields=frozenset(fields),
    )
    assert host.command_unavailable(words) == expected
