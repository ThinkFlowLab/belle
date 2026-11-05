"""OpenJiuwen tool cards remain available without JiuwenSwarm."""

from importlib import import_module

import pytest

pytest.importorskip("openjiuwen")


@pytest.mark.parametrize(
    ("module", "toolkit", "names"),
    [
        ("history", "SlackHistoryToolkit", ("read_slack_conversation", "download_slack_file", "read_pinned_messages")),
        ("search", "SlackSearchToolkit", ("search_slack_workspace",)),
        ("directory", "SlackDirectoryToolkit", ("find_by_name",)),
        ("bookmarks", "SlackBookmarkToolkit", ("write_bookmark", "read_bookmarks")),
        ("reactions", "SlackReactionToolkit", ("react_to_message",)),
        ("pins", "SlackPinToolkit", ("pin_message",)),
        ("post", "SlackPostToolkit", ("post_message", "edit_message", "delete_message")),
        (
            "lists",
            "SlackListToolkit",
            ("read_slack_list", "write_slack_list", "edit_slack_list", "share_slack_list", "delete_slack_list"),
        ),
        (
            "canvases",
            "SlackCanvasToolkit",
            (
                "read_slack_canvas",
                "write_slack_canvas",
                "write_slack_channel_canvas",
                "edit_slack_canvas",
                "share_slack_canvas",
                "delete_slack_canvas",
            ),
        ),
        ("home_tab", "SlackHomeTabToolkit", ("publish_slack_home_tab", "clear_slack_home_tab")),
    ],
)
def test_standalone_tool_cards(module: str, toolkit: str, names: tuple[str, ...]) -> None:
    cls = getattr(import_module(f"belle_agent.integrations.openjiuwen.tools.{module}"), toolkit)
    assert tuple(tool.card.name for tool in cls().get_tools()) == names
