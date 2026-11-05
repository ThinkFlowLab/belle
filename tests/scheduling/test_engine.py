"""Scheduler engine ownership follows backend capabilities, not backend names."""

import pytest

from belle_agent.backend import BackendCapabilities
from belle_agent.config import BelleConfigError
from belle_agent.scheduling.engine import resolve_engine


@pytest.mark.parametrize("name", ["opencode", "docker_agent", "future_harness"])
def test_auto_selects_belle_without_a_backend_scheduler(name: str) -> None:
    assert resolve_engine("auto", BackendCapabilities(name)) == "belle"


def test_auto_selects_host_with_one() -> None:
    assert resolve_engine("auto", BackendCapabilities("future_harness", scheduler="native")) == "host"


def test_belle_with_a_backend_scheduler_is_refused() -> None:
    with pytest.raises(BelleConfigError, match="keeps its own scheduler and tools"):
        resolve_engine("belle", BackendCapabilities("future_harness", scheduler="native"))


def test_host_without_one_is_refused() -> None:
    with pytest.raises(BelleConfigError, match="has no scheduler"):
        resolve_engine("host", BackendCapabilities("future_harness"))


def test_disabled_does_not_need_a_backend_scheduler() -> None:
    assert resolve_engine("disabled", BackendCapabilities("future_harness")) == "disabled"
