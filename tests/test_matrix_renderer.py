from belle_agent.platforms.matrix import render
from belle_agent.platforms.rendering import Renderer


def test_each_part_carries_the_text_fields_of_its_own_markdown():
    renderer: Renderer[dict[str, str]] = render.RENDERER
    text = "\n\n".join(f"**Part {n}** " + "word " * 40 for n in range(5))

    found = renderer.parts(text, 600)

    assert len(found) > 1
    assert [part.markdown for part in found] == render.parts(text, 600)
    assert all(part.payload == render.content(part.markdown) and renderer.measure(part.markdown) <= 600 for part in found)


def test_the_size_is_counted_in_bytes():
    assert render.RENDERER.measure("é") == render.size("é") > len("é") * 2


def test_the_cut_of_a_streamed_reply_is_the_one_render_gives():
    text = "intro\n\n```\none\n\ntwo\n```\n\n" + "after " * 30

    assert render.RENDERER.cut_index(text, 60) == render.cut_index(text, 60)


def test_belle_s_own_text_is_shown_as_written():
    assert render.RENDERER.escape("<b>report_*final*.md</b>") == "<b>report_*final*.md</b>"
