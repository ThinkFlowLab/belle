from belle_agent.platforms.rendering import Renderer
from belle_agent.platforms.slack.commands import escape
from belle_agent.platforms.slack.rendering import SlackMessage, SlackRenderer
from belle_agent.platforms.slack.text import (
    MAX_SLACK_TEXT_LENGTH,
    normalize_slack_mrkdwn,
    preferred_split_index,
    split_text,
)


def _no_blocks(text, requested):
    return None, f"kind-for-{requested}"


def test_a_part_is_the_normalised_fallback_text_and_what_slack_is_sent_for_it():
    renderer: Renderer[SlackMessage] = SlackRenderer(_no_blocks)

    (part,) = renderer.parts("**bold** and [a link](https://example.test)", MAX_SLACK_TEXT_LENGTH)

    assert part.markdown == normalize_slack_mrkdwn("**bold** and [a link](https://example.test)")
    assert part.payload == SlackMessage(part.markdown, None, "kind-for-None")


def test_the_block_markers_go_and_each_chunk_gets_the_blocks_the_marker_asks_for():
    seen = []

    def blocks(text, requested):
        seen.append((text, requested))
        return [{"type": "divider"}], "table"

    text = "<!-- belle:slack-blocks -->\n" + "\n\n".join("word " * 2000 for _ in range(10))
    found = SlackRenderer(blocks).parts(text, MAX_SLACK_TEXT_LENGTH)

    assert [part.markdown for part in found] == split_text(normalize_slack_mrkdwn(text.split("\n", 1)[1].strip()))
    assert seen == [(part.markdown, True) for part in found] and len(found) > 1


def test_the_size_and_the_cut_are_slack_s_own():
    renderer = SlackRenderer(_no_blocks)
    text = "see <https://example.test/a-long-link|label> now"

    assert renderer.measure("é😀") == 2
    assert renderer.cut_index(text, 20) == preferred_split_index(text, 20)


def test_belle_s_own_text_is_escaped_as_slack_commands_escape_it():
    assert SlackRenderer(_no_blocks).escape("a < b & <@U1>") == escape("a < b & <@U1>")
