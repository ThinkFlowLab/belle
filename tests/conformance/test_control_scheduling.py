"""Verify scheduler metadata reaches the host's local status provider."""

from belle_agent.control.protocol import utc_now


async def test_host_registers_cached_scheduler_provider(world):
    host = world.host
    original = host.scheduler
    sample = {"id": "scheduling", "state": "ready", "reason": None,
              "observed_at": utc_now(), "metrics": {}, "rows": [
        {"job_id": "job-1234567890", "name": "Daily digest", "state": "active",
         "when": "every seconds=300", "next_due": None}]}

    class CachedScheduler:
        def status(self):
            return sample

    try:
        host.scheduler = CachedScheduler()
        snapshot = await host._control.collector.collect()
        assert snapshot["scheduling"] == sample
        assert snapshot["scheduling"] is not sample
    finally:
        host.scheduler = original


async def test_host_bridges_operator_toggle_and_scheduler_refusal(world):
    from belle_agent.control.protocol import ControlError
    from belle_agent.scheduling.refusals import Refused

    original = world.host.scheduler
    calls = []

    class Scheduler:
        async def toggle_job(self, job_id, state):
            calls.append((job_id, state))
            if state == 'paused':
                raise Refused('job_ended', 'Ended jobs need a new schedule')

    try:
        world.host.scheduler = Scheduler()
        await world.host.toggle_schedule('job-1234567890', 'active')
        assert calls == [('job-1234567890', 'active')]
        import pytest
        with pytest.raises(ControlError) as error:
            await world.host.toggle_schedule('job-1234567890', 'paused')
        assert error.value.code == 'job_ended'
        world.host.scheduler = None
        with pytest.raises(ControlError) as error:
            await world.host.toggle_schedule('job-1234567890', 'active')
        assert error.value.code == 'unsupported'
    finally:
        world.host.scheduler = original
