from belle_agent.integrations.jiuwenswarm.scopes import register_host_channels
from belle_agent.scopes import channel_capabilities


def test_cron_is_declared_unattended_and_reads_permissions(registry):
    register_host_channels()
    cron = channel_capabilities("__cron__")

    assert cron is not None
    assert cron.attended is False
    assert cron.reads("permissions")
    assert not cron.reads("delivery")
