from belle_agent.platforms.mattermost.render import RENDERER
from belle_agent.platforms.rendering import Renderer
from belle_agent.text_split import split_index, split_text


def test_each_post_is_the_markdown_of_its_part_split_as_core_splits_text():
    renderer: Renderer[dict[str, str]] = RENDERER
    text = "\n\n".join(f"**Part {n}** " + "mot " * 40 for n in range(5))

    found = renderer.parts(text, 300)

    assert [part.markdown for part in found] == split_text(text, 300, preserve_whitespace=True)
    assert "".join(part.markdown for part in found) == text
    assert all(part.payload == {"message": part.markdown} for part in found)


def test_a_server_that_states_no_size_gets_the_whole_text_to_judge():
    assert [part.markdown for part in RENDERER.parts(" whole text ", 0)] == [" whole text "]


def test_the_size_is_counted_in_characters():
    assert RENDERER.measure("é😀") == 2


def test_the_cut_of_a_streamed_reply_is_a_core_text_cut():
    text = "first line\nsecond line and more words"

    assert RENDERER.cut_index(text, 20) == split_index(text, 20)


def test_belle_s_own_text_is_shown_as_written():
    assert RENDERER.escape("@ada report_*final*.md") == "@ada report_*final*.md"
