import pytest

from belle_agent.platforms.slack import commands
from belle_agent.platforms.slack import command_ui


@pytest.mark.parametrize(
    ("text", "subcommand", "arguments"),
    [
        ("", commands.HELP, ""),
        ("SKILLS   List", commands.SKILLS_LIST, ""),
        ("skills use research  two  spaces", commands.SKILLS_USE, "research  two  spaces"),
        ("subagents use code_agent inspect this", commands.SUBAGENTS_USE, "code_agent inspect this"),
    ],
)
def test_resolve(text: str, subcommand: commands.Subcommand, arguments: str) -> None:
    assert commands.resolve(text) == (subcommand, arguments)
    assert commands.resolve("skillslist")[0] is None


def test_help_lists_commands_without_tui_commands() -> None:
    text = commands.help_text("/jiuwen", bot_name="JiuwenSwarm")
    assert all(f"`{commands.usage(sub, '/jiuwen')}`" in text for sub in commands.SUBCOMMANDS)
    assert all(command not in text for command in ("/status", "/model", "/exit"))


def test_picker_keeps_cancel_id_and_supports_static_and_external_options() -> None:
    static = command_ui.select_blocks(
        prompt="Choose",
        action_id="test",
        placeholder="Find",
        options=[("First", "one", "A preview")],
    )
    assert static[1]["elements"][0]["options"][0]["description"]["text"] == "A preview"
    assert static[1]["elements"][1]["action_id"] == "belle_slash_cancel_select"
    dynamic = command_ui.select_blocks(prompt="Choose", action_id="test", placeholder="Find")
    assert dynamic[1]["elements"][0]["type"] == "external_select"
    with pytest.raises(ValueError, match="at most 100"):
        command_ui.select_blocks(
            prompt="Choose",
            action_id="test",
            placeholder="Find",
            options=[("same", str(index)) for index in range(101)],
        )


def test_message_link_requires_a_matching_slack_host() -> None:
    url = "https://acme.slack.com/archives/C123/p1790000000000001"
    assert command_ui.message_link_target(url, host="acme.slack.com") == (
        "C123",
        "1790000000.000001",
    )
    assert command_ui.message_link_target(url, host="other.slack.com") is None
    assert command_ui.message_link_target(url.replace("https:", "http:"), host="acme.slack.com") is None


def test_status_reports_only_public_facts() -> None:
    text = commands.status_text(
        commands.StatusFacts(agent_answered=True, agent_seconds=0.42, chat_type="channel"),
        "/jiuwen",
        bot_name="JiuwenSwarm",
    )
    assert [line.split("*")[1] for line in text.splitlines()[1:]] == [
        "Agent",
        "Model",
        "Right now",
        "`/jiuwen ask` here would use",
    ]
    assert "(status check took 0.4 s)" in text


def test_catalog_text_escapes_slack_markup() -> None:
    text = commands.experts_text(
        [{"id": "bad<!channel>", "installed": True, "connection_state": "connected"}],
        "missing<&>",
    )
    assert "`missing&lt;&amp;&gt;` (not installed)" in text
    assert "`bad&lt;!channel&gt;`" in text
    assert "<!channel>" not in text


def test_private_question_anchors_escape_mass_mentions() -> None:
    anchor = commands.anchor_text("hi <!channel> &amp; you\nsecond line", "/jiuwen ask")
    assert "<!channel>" not in anchor
    assert ">hi &lt;!channel&gt; &amp; you" in anchor
    assert ">second line" in anchor
    side = commands.side_anchor_text(
        "<!here> ok?",
        "C123",
        commands.CONTEXT_THREAD,
        "https://acme.slack.com/archives/C123/p1790000000000001",
        command="/jiuwen ask",
    )
    assert "<!here>" not in side
    assert "&lt;!here&gt; ok?" in side


def test_skill_and_subagent_lists_escape_names_and_bound_length() -> None:
    names = [f"agent-{index}" for index in range(130)] + ["<unsafe>"]
    text = commands.subagents_text(names)
    assert "agent-129" in text and "&lt;unsafe&gt;" in text
    assert "<unsafe>" not in text
    many = [
        {"name": f"s{index:03d}", "description": "Does one thing. And more besides."}
        for index in range(commands.MAX_DESCRIBED_SKILLS + commands.MAX_NAMED_SKILLS + 7)
    ]
    text = commands.skills_text(many, required=["s021"])
    assert text.count("\n• ") == commands.MAX_DESCRIBED_SKILLS
    assert "And more besides" not in text
    assert text.splitlines()[-1].endswith(", and 7 more.")



def test_bot_name_is_used_in_private_notices() -> None:
    for notice in (
        commands.foreign_workspace_notice("Belle"),
        commands.not_permitted_notice("Belle"),
        commands.ask_answering_in_dm_notice("Belle"),
        commands.help_text("/jiuwen", bot_name="Belle"),
    ):
        assert "Belle" in notice
        assert "JiuwenSwarm" not in notice
