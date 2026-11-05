"""Scheduler configuration boundary checks."""

from dataclasses import fields

import pytest

from belle_agent.config import BelleConfigError, load_config
from belle_agent.scheduling.settings import SchedulingSettings, scheduling_settings


def test_each_key_has_its_default() -> None:
    assert scheduling_settings({}) == SchedulingSettings()


@pytest.mark.parametrize(("key", "value"), [
    ("engine", False), ("max_jobs", True), ("min_interval_seconds", 0),
    ("max_concurrent_runs", 0), ("job_visibility", "private"),
    ("operator_role", 2), ("default_zone", "Nowhere/Unknown"),
    ("delivery", {}), ("kept_runs_per_job", 0),
    ("disable_after_failures", -1),
    ("question_timeout_seconds", 0),
])
def test_each_bad_value_names_its_key(key: str, value: object) -> None:
    with pytest.raises(BelleConfigError, match=f"scheduling.{key}"):
        scheduling_settings({"scheduling": {key: value}})


def test_unquoted_off_engine_names_the_fix(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "config.yaml"
    path.write_text("scheduling:\n  engine: off\n", encoding="utf-8")
    with pytest.raises(BelleConfigError, match="write disabled"):
        load_config(path)


def test_operator_role_must_exist() -> None:
    with pytest.raises(BelleConfigError, match="Roles: admin"):
        scheduling_settings({"roles": {"admin": {}}, "scheduling": {"operator_role": "missing"}})


def test_example_lists_every_key() -> None:
    from pathlib import Path

    example = (Path(__file__).parents[2] / "examples/config/belle-config.yaml").read_text(encoding="utf-8")
    section = example.split("scheduling:\n", 1)[1].split("\nbackends:", 1)[0]
    assert all(f"  {item.name}:" in section for item in fields(SchedulingSettings))


def test_deleted_job_history_days_is_refused() -> None:
    with pytest.raises(BelleConfigError, match="deleted_job_history_days.*session_retention_hours"):
        scheduling_settings({"scheduling": {"deleted_job_history_days": 30}})


def test_delivery_rule_checks_its_field() -> None:
    with pytest.raises(BelleConfigError, match=r"delivery\[0\]\.to\.same"):
        scheduling_settings({"scheduling": {"delivery": [{"to": {"same": ["thread"]}}]}})
