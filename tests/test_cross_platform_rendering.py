import re

import pytest

from belle_agent.platforms.discord.render import RENDERER as DISCORD
from belle_agent.platforms.matrix.render import RENDERER as MATRIX
from belle_agent.platforms.mattermost.render import RENDERER as MATTERMOST
from belle_agent.platforms.slack.rendering import SlackRenderer

SLACK = SlackRenderer(lambda text, requested: (None, "none"))


@pytest.mark.parametrize("renderer,budget", [(SLACK, 4000), (MATRIX, 44000), (MATTERMOST, 16383), (DISCORD, 2000)])
@pytest.mark.parametrize("form", ["plain", "unicode", "word", "fence", "markup", "link", "large_link", "empty"])
def test_every_rendered_part_fits_and_preserves_each_source_marker_once(renderer, budget, form):
    markers = [f"segment{n:04d}" for n in range(220)]
    if form == "empty":
        text = ""
        markers = []
    elif form == "large_link":
        markers = markers[:1]
        text = f"[{markers[0]}](https://example.org/{'x' * (budget * 2)})"
    elif form == "word":
        text = "".join(markers)
    elif form == "fence":
        text = "```python\n" + "\n".join(f"{mark} = 'é😀'" for mark in markers) + "\n```"
    elif form == "link":
        text = " ".join(f"[{mark}](https://example.org/{'x' * 30})" for mark in markers)
    elif form == "markup":
        text = " ".join(f"**{mark}**" for mark in markers)
    else:
        text = " ".join(f"{mark} {'é😀' * 12 if form == 'unicode' else 'word ' * 12}" for mark in markers)

    parts = renderer.parts(text, budget)

    assert all(renderer.measure(part.markdown) <= budget for part in parts)
    # Joining without separators also checks markers split across message boundaries.
    output = "".join(part.markdown for part in parts)
    assert re.findall(r"segment\d{4}", output) == markers
    if form == "large_link":
        assert output.count("x") == text.count("x")


@pytest.mark.parametrize("text", ["", "é😀" * 10000, "word" * 20000, "```python\nlong_word\n```", "**strong**", "[label](https://example.org/" + "x" * 10000 + ")"])
async def test_loopback_records_the_original_text_without_a_transport_budget(text):
    from belle_agent.platforms.loopback.platform import LoopbackPlatform

    platform = LoopbackPlatform()
    await platform.post("room", "", text)

    assert [record.text for record in platform.log] == [text]
