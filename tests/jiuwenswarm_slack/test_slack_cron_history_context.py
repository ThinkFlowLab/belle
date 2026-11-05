"""Keep Slack cron provenance bound to the request that created a job."""

import time
from typing import Any

import pytest

from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.models import CronJob
from jiuwenswarm.gateway.cron.store import CronJobStore
from belle_agent.integrations.jiuwenswarm.slack_routing import slack_history_metadata_for_cron_job

_SLACK_SESSION = "slack_T-TEAM_C-ONE_U-ONE"
_FORGED_SESSION = "slack_T-TEAM_C-PRIVATE_U-ATTACKER"
pytestmark = pytest.mark.usefixtures("cron_hooks")

class _StubScheduler:
    """Scheduler stand-in: reload, plus the project fence create_job consults."""

    def __init__(self) -> None:
        self.reloads = 0

    async def reload(self) -> None:
        self.reloads += 1

    async def project_execution_allowed(self, project_id: str, user_id: str) -> bool:
        return True


def _job(**overrides: Any) -> CronJob:
    defaults: dict[str, Any] = {
        "id": "job-1",
        "name": "digest",
        "enabled": True,
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "description": "summarise the channel",
        "targets": "slack",
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    defaults.update(overrides)
    return CronJob(**defaults)


def _create_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": "digest",
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "description": "summarise the channel",
        "targets": "slack",
    }
    params.update(overrides)
    return params


@pytest.fixture
def controller(tmp_path: Any) -> CronController:
    return CronController(
        store=CronJobStore(path=tmp_path / "cron_jobs.json"),
        scheduler=_StubScheduler(),
    )


@pytest.mark.asyncio
async def test_job_created_on_a_slack_turn_is_recorded_as_trusted(
    controller: CronController,
) -> None:
    created = await controller.create_job(
        _create_params(session_id=_SLACK_SESSION),
        request_channel_id="slack",
        request_session_id=_SLACK_SESSION,
    )

    assert created["session_id"] == _SLACK_SESSION
    assert created["origin_channel_id"] == "slack"


@pytest.mark.asyncio
async def test_hand_written_slack_session_is_created_but_never_trusted(
    controller: CronController,
) -> None:
    """The security case, end to end.

    Anyone who can reach the cron RPC can type a session id naming any channel
    the bot is in. The job is still created and will still deliver there -- that
    is long-standing behaviour and not this change's to alter -- but it is not
    recorded as having a Slack context, so it reads nothing.
    """
    created = await controller.create_job(
        _create_params(session_id=_FORGED_SESSION),
        request_channel_id="web",
        request_session_id="web_42",
    )

    assert created["session_id"] == _FORGED_SESSION
    assert "origin_channel_id" not in created
    assert slack_history_metadata_for_cron_job(_job(**{
        "session_id": _FORGED_SESSION,
        "origin_channel_id": "",
    })) == {}


@pytest.mark.asyncio
async def test_caller_supplied_trust_flag_is_ignored(
    controller: CronController,
) -> None:
    """A create call cannot vouch for itself by setting the flag."""
    created = await controller.create_job(
        _create_params(session_id=_FORGED_SESSION, origin_channel_id="slack"),
        request_channel_id="web",
        request_session_id="web_42",
    )

    assert "origin_channel_id" not in created


@pytest.mark.asyncio
async def test_repointing_the_session_from_elsewhere_revokes_trust(
    controller: CronController,
) -> None:
    created = await controller.create_job(
        _create_params(session_id=_SLACK_SESSION),
        request_channel_id="slack",
        request_session_id=_SLACK_SESSION,
    )

    patched = await controller.update_job(
        created["id"],
        {"session_id": _FORGED_SESSION, "origin_channel_id": "slack"},
        request_channel_id="web",
        request_session_id="web_42",
    )

    assert patched["session_id"] == _FORGED_SESSION
    assert "origin_channel_id" not in patched


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": False},
        # Repeating the same target must not revoke the proven session.
        {"targets": "slack"},
    ],
    ids=["unrelated-field", "targets-only"],
)
async def test_an_unrelated_edit_keeps_a_proven_session_trusted(
    controller: CronController,
    patch: dict[str, Any],
) -> None:
    created = await controller.create_job(
        _create_params(session_id=_SLACK_SESSION),
        request_channel_id="slack",
        request_session_id=_SLACK_SESSION,
    )

    patched = await controller.update_job(created["id"], patch)

    assert patched["session_id"] == _SLACK_SESSION
    assert patched["origin_channel_id"] == "slack"


def test_trust_survives_the_dict_round_trip() -> None:
    job = _job(session_id=_SLACK_SESSION, origin_channel_id="slack")

    restored = CronJob.from_dict(job.to_dict())

    assert restored.origin_channel_id == "slack"
    assert restored.to_dict()["origin_channel_id"] == "slack"


def test_a_record_written_before_this_field_existed_loads_untrusted() -> None:
    legacy = _job(session_id=_SLACK_SESSION).to_dict()
    legacy.pop("origin_channel_id", None)

    restored = CronJob.from_dict(legacy)

    assert restored.origin_channel_id == ""
    assert slack_history_metadata_for_cron_job(restored) == {}
