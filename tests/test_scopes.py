"""Generic scope registration and matching."""

from belle_agent.scopes import (
    ChannelCapabilities,
    channel_capabilities,
    compile_scopes,
    compose_section,
    known_channels,
    register_channel,
)
from belle_agent.scopes.capabilities import check_skills, restore_registry, snapshot_registry


def test_host_registers_channels_explicitly():
    previous = snapshot_registry()
    try:
        assert "demo" not in known_channels()
        register_channel(
            ChannelCapabilities(
                channel="demo",
                axes=frozenset({"channel", "chat"}),
                sections={"delivery": frozenset({"reply"})},
            )
        )
        assert channel_capabilities("demo").acts_on("delivery", "reply")
        assert "demo" in known_channels()
    finally:
        restore_registry(previous)


def test_registered_channel_compiles_and_composes():
    previous = snapshot_registry()
    try:
        register_channel(
            ChannelCapabilities(
                channel="demo",
                axes=frozenset({"channel", "chat"}),
                sections={"delivery": frozenset({"reply"})},
            )
        )
        scopes = compile_scopes(
            [
                {"match": {"channel": "demo"}, "delivery": {"reply": "required"}},
                {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"reply": "optional"}},
            ]
        )
        assert compose_section(scopes, channel="demo", chat="C1") == {"reply": "optional"}
        assert compose_section(scopes, channel="demo", chat="C2") == {"reply": "required"}
    finally:
        restore_registry(previous)


def test_skills_unavailable_takes_unsigned_names_and_no_required_skill():
    assert check_skills({"unavailable": ["a", "b"], "required": ["c"]}) is None
    assert check_skills({"unavailable": []}) is None
    assert ".unavailable" in check_skills({"unavailable": ["-a"]}) and "+/- prefixes" in check_skills({"unavailable": ["-a"]})
    assert "available, unavailable, required" in check_skills({"hidden": ["a"]})
    both = check_skills({"required": ["a", "b"], "unavailable": ["b"]})
    assert ".required and .unavailable both name ['b']" in both


def test_skills_unavailable_replaces_what_it_inherits_and_keeps_the_other_skill_keys():
    previous = snapshot_registry()
    try:
        register_channel(
            ChannelCapabilities(
                channel="demo",
                axes=frozenset({"channel", "chat"}),
                sections={"agent": frozenset({"skills"})},
                validators={"agent.skills": check_skills},
            )
        )
        scopes = compile_scopes(
            [
                {"match": {"channel": "demo"}, "agent": {"skills": {"unavailable": ["a", "b"], "required": ["r"]}}},
                {"match": {"channel": "demo", "chat": "C1"}, "agent": {"skills": {"unavailable": ["c"]}}},
                {"match": {"channel": "demo", "chat": "C2"}, "agent": {"skills": {"available": ["a", "x"]}}},
            ]
        )
        skills = {chat: compose_section(scopes, channel="demo", chat=chat, section="agent")["skills"] for chat in ("C1", "C2", "C3")}
        assert skills["C1"] == {"unavailable": ["c"], "required": ["r"]}
        assert skills["C2"] == {"unavailable": ["a", "b"], "required": ["r"], "available": ["a", "x"]}
        assert skills["C3"] == {"unavailable": ["a", "b"], "required": ["r"]}
    finally:
        restore_registry(previous)
