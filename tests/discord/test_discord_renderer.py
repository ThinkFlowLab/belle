"""Discord renders through ``DiscordRenderer``: Discord's Markdown, in parts of at most 2000 code points."""

from belle_agent.platforms.discord.affordances import MESSAGE_LIMIT
from belle_agent.platforms.discord.render import ALLOWED_MENTIONS, RENDERER, discord_markdown
from belle_agent.platforms.rendering import Renderer


def test_headings_discord_lacks_become_bold_and_setext_ones_become_hashes():
    assert discord_markdown("#### Deep\n\n### Kept") == "**Deep**\n\n### Kept"
    assert discord_markdown("Title\n=====\n\nSub\n---") == "# Title\n\n## Sub"


def test_double_underscores_are_strong_and_images_are_links_but_code_is_left_alone():
    text = "Some __strong__ and `__code__` and ![chart](https://example.test/c.png)\n\n```\n__kept__ ![x](y)\n```"

    assert discord_markdown(text) == (
        "Some **strong** and `__code__` and [chart](https://example.test/c.png)\n\n```\n__kept__ ![x](y)\n```"
    )


def test_a_table_becomes_a_fenced_block_with_aligned_columns_of_plain_cells():
    text = "Results:\n\n| Name | Score |\n|:---|---:|\n| **Ada** | 3 |\n| Lin | 15 |\n\nDone."

    assert discord_markdown(text) == (
        "Results:\n\n```\nName | Score\n-----+------\nAda  |     3\nLin  |    15\n```\n\nDone."
    )


def test_each_part_fits_the_budget_in_code_points_and_notifies_only_users():
    renderer: Renderer[dict] = RENDERER
    text = "\n\n".join("😀" * 30 + " paragraph" for _ in range(80))

    found = renderer.parts(text, MESSAGE_LIMIT)

    assert len(found) > 1 and all(renderer.measure(part.markdown) <= MESSAGE_LIMIT for part in found)
    assert all(part.payload == {"content": part.markdown, "allowed_mentions": ALLOWED_MENTIONS} for part in found)
    assert ALLOWED_MENTIONS == {"parse": ["users"], "replied_user": False}


def test_a_long_code_block_is_closed_and_opened_again_in_each_part():
    text = "```python\n" + "\n".join(f"value_{n} = {n}" for n in range(400)) + "\n```"

    found = RENDERER.parts(text, MESSAGE_LIMIT)

    assert len(found) > 1 and all(part.markdown.startswith("```python\n") and part.markdown.endswith("\n```") for part in found)


def test_the_renderer_counts_unicode_code_points():
    assert RENDERER.measure("é😀") == 2


def test_belle_s_own_text_has_discord_s_markdown_characters_escaped():
    assert RENDERER.escape("report_*final*.md (v2) #1") == r"report\_\*final\*.md \(v2\) \#1"
