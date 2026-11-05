"""Register JiuwenSwarm channels with Belle scopes."""

from belle_agent.scopes import ChannelCapabilities, register_channel


def register_host_channels() -> None:
    from belle_agent.integrations.jiuwenswarm import scope_capabilities  # noqa: F401 - registers Slack

    for name in ("web", "__cron__", "__heartbeat__"):
        register_channel(
            ChannelCapabilities(
                channel=name,
                axes=frozenset({"channel"}),
                sections={"delivery": None, "permissions": None} if name == "web" else {"permissions": None},
                attended=name == "web",
            )
        )
