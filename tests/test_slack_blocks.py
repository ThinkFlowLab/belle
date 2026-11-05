"""Slack Block Kit renderer tests."""

import json
import logging

import pytest

from belle_agent.platforms.slack import blocks as slack_blocks

TABLE = "| Name | Count |\n| --- | --- |\n| alpha | 1 |\n| beta | 2 |"


def _tables(blocks):
    """Return plain and data tables."""
    return [block for block in blocks if block["type"] in ("table", "data_table")]


def _basic(content, **kwargs):
    """Render content as a plain table."""
    return slack_blocks.render_blocks(content, render_tables=slack_blocks.RENDER_TABLES_BASIC, **kwargs)


def _sections(blocks):
    return [block for block in blocks if block["type"] == "section"]


def _cell_text(cell):
    """What one cell displays, whichever type it is."""
    if cell["type"] == "raw_text":
        return cell["text"]
    parts = []
    for section in cell["elements"]:
        for element in section["elements"]:
            if element["type"] == "link":
                parts.append(element.get("text") or element["url"])
            else:
                parts.append(element["text"])
    return "".join(parts)


def _cells(block):
    return [[_cell_text(cell) for cell in row] for row in block["rows"]]


def _urls(block):
    """Every link destination in the block, row by row."""
    return [
        [
            [
                element["url"]
                for section in cell.get("elements", [])
                for element in section["elements"]
                if element["type"] == "link"
            ]
            for cell in row
        ]
        for row in block["rows"]
    ]


def test_prose_without_a_table_renders_no_blocks() -> None:
    content = "*Summary*\n\n• one\n• two\n\nSee <https://example.invalid|the report>."
    assert slack_blocks.render_blocks(content) is None


def test_table_becomes_a_table_block_with_its_rows_in_order() -> None:
    blocks = _basic(TABLE)
    assert [block["type"] for block in blocks] == ["table"]
    assert _cells(blocks[0]) == [
        ["Name", "Count"],
        ["alpha", "1"],
        ["beta", "2"],
    ]
    assert all(cell["type"] == "raw_text" for row in blocks[0]["rows"] for cell in row)


def test_prose_around_a_table_stays_mrkdwn_in_section_blocks() -> None:
    content = f"*Totals*\n\n{TABLE}\n\nSee <https://example.invalid|the source>."
    blocks = _basic(content)
    assert [block["type"] for block in blocks] == ["section", "table", "section"]
    assert blocks[0]["text"] == {"type": "mrkdwn", "text": "*Totals*"}
    assert blocks[2]["text"]["text"] == "See <https://example.invalid|the source>."


def test_alignment_row_becomes_column_settings() -> None:
    content = "| L | C | R |\n| :-- | :-: | --: |\n| a | b | c |"
    blocks = _basic(content)
    assert blocks[0]["column_settings"] == [
        {"align": "left"},
        {"align": "center"},
        {"align": "right"},
    ]


def test_all_left_aligned_table_omits_column_settings() -> None:
    blocks = slack_blocks.render_blocks(TABLE)
    assert "column_settings" not in blocks[0]


def test_cell_markup_is_reduced_to_what_raw_text_can_show() -> None:
    content = "| Item | Detail |\n| --- | --- |\n| *bold* | `code` |\n| <https://example.invalid|label> | a \\| b |"
    blocks = slack_blocks.render_blocks(content)
    assert _cells(blocks[0])[1:] == [
        ["bold", "code"],
        ["label", "a | b"],
    ]


def test_bare_link_without_a_label_keeps_the_url() -> None:
    content = "| Where |\n| --- |\n| <https://example.invalid/x> |"
    blocks = slack_blocks.render_blocks(content)
    assert _cells(blocks[0])[1] == ["https://example.invalid/x"]


def test_a_cell_with_a_link_keeps_the_destination() -> None:
    content = "| Pull request | Owner |\n| --- | --- |\n| <https://example.invalid/pr/1426|jiuwenswarm#1426> | alice |"
    blocks = slack_blocks.render_blocks(content)
    row = blocks[0]["rows"][1]
    assert row[0] == {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [
                    {
                        "type": "link",
                        "url": "https://example.invalid/pr/1426",
                        "text": "jiuwenswarm#1426",
                    }
                ],
            }
        ],
    }
    # A cell with no link is untouched, and still the cheaper type.
    assert row[1] == {"type": "raw_text", "text": "alice"}


def test_a_bare_link_carries_no_redundant_text() -> None:
    content = "| Where |\n| --- |\n| <https://example.invalid/x> |"
    blocks = slack_blocks.render_blocks(content)
    (cell,) = blocks[0]["rows"][1]
    (element,) = cell["elements"][0]["elements"]
    assert element == {"type": "link", "url": "https://example.invalid/x"}


def test_text_around_a_link_is_kept_in_order_and_spaced() -> None:
    content = "| Note |\n| --- |\n| see <https://example.invalid/a|the report> for detail |"
    blocks = slack_blocks.render_blocks(content)
    (cell,) = blocks[0]["rows"][1]
    assert cell["elements"][0]["elements"] == [
        {"type": "text", "text": "see "},
        {"type": "link", "url": "https://example.invalid/a", "text": "the report"},
        {"type": "text", "text": " for detail"},
    ]
    assert _cell_text(cell) == "see the report for detail"


def test_two_links_in_one_cell_both_survive() -> None:
    content = "| Links |\n| --- |\n| <https://example.invalid/a|a> and <https://example.invalid/b|b> |"
    blocks = slack_blocks.render_blocks(content)
    assert _urls(blocks[0])[1] == [["https://example.invalid/a", "https://example.invalid/b"]]
    assert _cells(blocks[0])[1] == ["a and b"]


def test_a_mention_is_not_turned_into_a_link() -> None:
    content = "| Who | Where |\n| --- | --- |\n| <@U01ABCDEF> | <#C01ABCDEF|general> |"
    blocks = slack_blocks.render_blocks(content)
    assert [cell["type"] for cell in blocks[0]["rows"][1]] == [
        "raw_text",
        "raw_text",
    ]
    assert _cells(blocks[0])[1] == ["@U01ABCDEF", "general"]


def test_markup_inside_a_linked_cell_is_still_reduced() -> None:
    content = "| Item |\n| --- |\n| *bold* <https://example.invalid/a|`code`> |"
    blocks = slack_blocks.render_blocks(content)
    (cell,) = blocks[0]["rows"][1]
    assert cell["elements"][0]["elements"] == [
        {"type": "text", "text": "bold "},
        {"type": "link", "url": "https://example.invalid/a", "text": "code"},
    ]


def test_a_linked_cell_costs_the_character_budget_only_what_it_shows() -> None:
    long_url = "https://example.invalid/" + "u" * 400
    rows = "\n".join(f"| <{long_url}|x> |" for _ in range(20))
    blocks = slack_blocks.render_blocks(f"| Link |\n| --- |\n{rows}")
    assert _urls(blocks[0])[1] == [[long_url]]


def test_a_link_in_a_cell_does_not_add_a_phantom_column() -> None:
    content = "| Report | Owner |\n| --- | --- |\n| <https://example.invalid/a|first> | <https://example.invalid/b|second> |\n| plain | `a | b` |"
    blocks = slack_blocks.render_blocks(content)
    assert [len(row) for row in blocks[0]["rows"]] == [2, 2, 2]
    assert _cells(blocks[0])[1] == ["first", "second"]
    assert _cells(blocks[0])[2] == ["plain", "a | b"]


def test_outer_pipes_are_optional() -> None:
    content = "Name | Count\n--- | ---\nalpha | 1"
    blocks = _basic(content)
    assert _cells(blocks[0]) == [["Name", "Count"], ["alpha", "1"]]


def test_two_tables_in_one_message_each_get_a_block() -> None:
    content = f"{TABLE}\n\nand then\n\n{TABLE}"
    blocks = _basic(content)
    assert [block["type"] for block in blocks] == ["table", "section", "table"]


# --- degradation ---------------------------------------------------------


def test_a_sentence_with_a_pipe_is_not_a_table() -> None:
    content = "Run `a | b` and read the output."
    assert slack_blocks.render_blocks(content) is None


def test_header_without_a_delimiter_row_stays_prose() -> None:
    content = "| Name | Count |\n| alpha | 1 |"
    assert slack_blocks.render_blocks(content) is None


def test_delimiter_of_the_wrong_width_stays_prose() -> None:
    content = "| Name | Count |\n| --- |\n| alpha | 1 |"
    assert slack_blocks.render_blocks(content) is None


def test_ragged_rows_are_padded_and_truncated_rather_than_raising() -> None:
    content = "| Name | Count | Note |\n| --- | --- | --- |\n| alpha |\n| beta | 2 | ok | extra |"
    blocks = slack_blocks.render_blocks(content)
    assert _cells(blocks[0]) == [
        ["Name", "Count", "Note"],
        ["alpha", "", ""],
        ["beta", "2", "ok"],
    ]


def test_a_table_inside_a_code_fence_is_left_alone() -> None:
    content = f"Example:\n\n```\n{TABLE}\n```\n"
    assert slack_blocks.render_blocks(content) is None


def test_a_table_after_a_closed_fence_is_still_rendered() -> None:
    content = f"```\nnot a table\n```\n\n{TABLE}"
    blocks = slack_blocks.render_blocks(content)
    assert len(_tables(blocks)) == 1


# --- limits --------------------------------------------------------------


def test_long_prose_is_split_into_sections_within_the_character_limit() -> None:
    paragraph = "\n".join(["x" * 200] * 40)
    blocks = slack_blocks.render_blocks(f"{paragraph}\n\n{TABLE}")
    sections = _sections(blocks)
    assert len(sections) > 1
    assert all(len(section["text"]["text"]) <= slack_blocks.MAX_SECTION_TEXT_LENGTH for section in sections)


def test_a_single_line_over_the_section_limit_is_hard_split() -> None:
    blocks = slack_blocks.render_blocks("y" * 7000 + f"\n\n{TABLE}")
    assert all(len(section["text"]["text"]) <= slack_blocks.MAX_SECTION_TEXT_LENGTH for section in _sections(blocks))


def test_more_blocks_than_slack_accepts_falls_back_to_text() -> None:
    paragraph = "\n\n".join(["z" * 2900] * (slack_blocks.MAX_BLOCKS_PER_MESSAGE + 2))
    assert slack_blocks.render_blocks(f"{paragraph}\n\n{TABLE}") is None


def test_a_table_over_the_row_limit_falls_back_to_text() -> None:
    rows = "\n".join(f"| r{index} | {index} |" for index in range(slack_blocks.MAX_TABLE_ROWS + 5))
    content = f"| Name | Count |\n| --- | --- |\n{rows}"
    assert slack_blocks.render_blocks(content) is None


def test_a_table_over_the_column_limit_falls_back_to_text() -> None:
    width = slack_blocks.MAX_TABLE_COLUMNS + 1
    header = "| " + " | ".join(f"c{index}" for index in range(width)) + " |"
    delimiter = "| " + " | ".join(["---"] * width) + " |"
    body = "| " + " | ".join(["v"] * width) + " |"
    assert slack_blocks.render_blocks(f"{header}\n{delimiter}\n{body}") is None


def test_a_table_over_the_character_budget_falls_back_to_text() -> None:
    rows = "\n".join(f"| {'w' * 200} | {index} |" for index in range(110))
    content = f"| Name | Count |\n| --- | --- |\n{rows}"
    assert slack_blocks.render_blocks(content) is None


def test_a_table_just_inside_the_limits_still_renders() -> None:
    rows = "\n".join(f"| r{index} | {index} |" for index in range(90))
    blocks = slack_blocks.render_blocks(f"| Name | Count |\n| --- | --- |\n{rows}")
    assert len(blocks[0]["rows"]) == 91
    assert len(blocks) <= slack_blocks.MAX_BLOCKS_PER_MESSAGE


def test_the_section_limit_still_matches_the_slack_sdk_block_model() -> None:
    from slack_sdk.models.blocks import SectionBlock

    assert slack_blocks.MAX_SECTION_TEXT_LENGTH == SectionBlock.text_max_length
    assert slack_blocks.MAX_BLOCKS_PER_MESSAGE == 50
    assert slack_blocks.MAX_TABLE_COLUMNS == 20
    assert slack_blocks.MAX_PLAIN_TABLE_ROWS == 100
    assert slack_blocks.MAX_PLAIN_TABLE_CHARACTERS_PER_MESSAGE == 10000


def test_rendered_blocks_are_accepted_by_the_sdk_block_parser() -> None:
    from slack_sdk.models.blocks import Block

    # The plain block, because the SDK does not model ``data_table`` at all --
    # see the module docstring on why its limits had to be probed by posting.
    blocks = _basic(f"*Totals*\n\n{TABLE}")
    parsed = Block.parse_all(blocks)
    assert [block.type for block in parsed] == ["section", "table"]
    for block in parsed:
        block.validate_json()


def test_a_table_with_a_linked_cell_is_accepted_by_the_sdk_parser() -> None:
    from slack_sdk.models.blocks import Block

    content = "| Pull request |\n| --- |\n| <https://example.invalid/pr/1|jiuwenswarm#1> |"
    blocks = _basic(content)
    for block in Block.parse_all(blocks):
        block.validate_json()


# --- which table block a table becomes ------------------------------------


def _long_table(rows: int = 25, tag: str = "r") -> str:
    body = "\n".join(f"| {tag}{index} | {index} |" for index in range(rows))
    return f"| Name | Count |\n| --- | --- |\n{body}"


def test_the_default_is_the_interactive_block() -> None:
    for rows in (1, 2, 5, 25):
        blocks = slack_blocks.render_blocks(_long_table(rows))
        assert blocks is not None
        assert blocks[0]["type"] == "data_table"
        assert blocks[0]["page_size"] <= rows


def test_basic_keeps_every_table_a_plain_table() -> None:
    for rows in (1, 2, 5, 25):
        blocks = slack_blocks.render_blocks(_long_table(rows), render_tables=slack_blocks.RENDER_TABLES_BASIC)
        assert blocks is not None
        assert blocks[0]["type"] == "table"
        assert "caption" not in blocks[0]
        assert "page_size" not in blocks[0]
        assert len(blocks[0]["rows"]) == rows + 1


def test_off_leaves_a_table_as_the_text_it_was_written_as() -> None:
    assert slack_blocks.render_blocks(_long_table(), render_tables=slack_blocks.RENDER_TABLES_OFF) is None


def test_off_is_about_tables_and_not_about_blocks() -> None:
    content = f"{_mermaid(PIE_SOURCE)}\n\n{_long_table(3)}"
    blocks = slack_blocks.render_blocks(content, render_tables=slack_blocks.RENDER_TABLES_OFF)
    assert _tables(blocks) == []
    assert [block["type"] for block in blocks] == ["data_visualization", "section"]
    assert "| r0 | 0 |" in blocks[-1]["text"]["text"]


def test_an_unknown_rendering_is_read_as_the_default() -> None:
    blocks = slack_blocks.render_blocks(_long_table(3), render_tables="fancy")
    assert blocks[0]["type"] == slack_blocks.RENDER_TABLES_DEFAULT


def test_the_marker_forces_a_data_table_out_of_a_basic_channel() -> None:
    blocks = slack_blocks.render_blocks(TABLE, requested=True, render_tables=slack_blocks.RENDER_TABLES_BASIC)
    assert blocks[0]["type"] == "data_table"
    # Two rows and a page that holds both: the pager never appears.
    assert blocks[0]["page_size"] == 2


def test_off_outranks_the_marker() -> None:
    assert slack_blocks.render_blocks(TABLE, requested=True, render_tables=slack_blocks.RENDER_TABLES_OFF) is None


def test_a_page_is_never_slacks_default_of_five() -> None:
    blocks = slack_blocks.render_blocks(_long_table(12))
    assert blocks[0]["page_size"] == 12

    blocks = slack_blocks.render_blocks(_long_table(50))
    assert blocks[0]["page_size"] == slack_blocks.DATA_TABLE_PAGE_SIZE


def test_a_page_size_stays_inside_the_bounds_slack_accepts() -> None:
    for rows in (1, 2, 300):
        blocks = slack_blocks.render_blocks(_long_table(rows))
        if blocks is None:  # over the row ceiling, which is a separate refusal
            continue
        page_size = blocks[0]["page_size"]
        assert 1 <= page_size <= slack_blocks.DATA_TABLE_PAGE_SIZE


def test_a_table_too_long_for_basic_declines_rather_than_losing_rows() -> None:
    content = _long_table(slack_blocks.MAX_PLAIN_TABLE_ROWS + 5)
    assert slack_blocks.render_blocks(content, render_tables=slack_blocks.RENDER_TABLES_BASIC) is None
    blocks = slack_blocks.render_blocks(content)
    assert blocks[0]["type"] == "data_table"


def test_a_table_too_long_for_either_block_declines_under_both() -> None:
    content = _long_table(slack_blocks.MAX_TABLE_ROWS + 5)
    for mode in slack_blocks.RENDER_TABLES_MODES:
        assert slack_blocks.render_blocks(content, render_tables=mode) is None


# --- the caption ----------------------------------------------------------


def test_the_caption_is_the_bold_line_the_table_sits_under() -> None:
    blocks = slack_blocks.render_blocks(f"*Newly tracked*\n\n{_long_table()}")
    assert blocks[-1]["caption"] == "Newly tracked"


def test_the_caption_is_taken_from_a_markdown_heading_too() -> None:
    blocks = slack_blocks.render_blocks(f"## Roster (1/3)\n\n{_long_table()}")
    assert blocks[-1]["caption"] == "Roster (1/3)"


def test_a_sentence_above_a_table_is_not_its_caption() -> None:
    blocks = slack_blocks.render_blocks(f"Here is what the run found today.\n\n{_long_table()}")
    assert blocks[-1]["caption"] == slack_blocks.DEFAULT_TABLE_CAPTION


def test_a_table_that_opens_the_message_gets_the_generic_caption() -> None:
    blocks = slack_blocks.render_blocks(_long_table())
    assert blocks[0]["caption"] == slack_blocks.DEFAULT_TABLE_CAPTION


def test_the_second_of_two_adjacent_tables_does_not_inherit_a_caption() -> None:
    content = f"*Changed*\n\n{_long_table(tag='a')}\n\n{_long_table(tag='b')}"
    blocks = slack_blocks.render_blocks(content)
    captions = [block["caption"] for block in blocks if block["type"] == "data_table"]
    assert captions == ["Changed", slack_blocks.DEFAULT_TABLE_CAPTION]


def test_a_long_heading_is_clamped_rather_than_sent_whole() -> None:
    blocks = slack_blocks.render_blocks(f"*{'w' * 400}*\n\n{_long_table()}")
    caption = blocks[-1]["caption"]
    assert len(caption) == slack_blocks.MAX_TABLE_CAPTION_LENGTH
    assert caption.endswith("…")


def test_a_caption_keeps_the_words_and_loses_the_markup() -> None:
    blocks = slack_blocks.render_blocks(f"*`Roster`:*\n\n{_long_table()}")
    assert blocks[-1]["caption"] == "Roster"


# --- numeric columns ------------------------------------------------------


def test_a_column_of_numbers_is_emitted_as_raw_number() -> None:
    blocks = slack_blocks.render_blocks(_long_table())
    body = blocks[0]["rows"][1:]
    assert all(row[1]["type"] == "raw_number" for row in body)
    assert all(row[0]["type"] == "raw_text" for row in body)


def test_every_numeric_cell_carries_the_text_slack_calls_optional() -> None:
    blocks = slack_blocks.render_blocks(_long_table())
    for row in blocks[0]["rows"][1:]:
        assert set(row[1]) == {"type", "value", "text"}
        assert row[1]["text"]


def test_thousands_separators_sort_by_value_and_read_as_written() -> None:
    body = "\n".join(f"| r{index} | {index * 1000:,} |" for index in range(25))
    blocks = slack_blocks.render_blocks(f"| Name | Count |\n| --- | --- |\n{body}")
    cell = blocks[0]["rows"][3][1]
    assert cell == {"type": "raw_number", "value": 2000, "text": "2,000"}


def test_a_decimal_column_keeps_its_fractions() -> None:
    body = "\n".join(f"| r{index} | {index}.5 |" for index in range(25))
    blocks = slack_blocks.render_blocks(f"| Name | Rate |\n| --- | --- |\n{body}")
    assert blocks[0]["rows"][1][1] == {"type": "raw_number", "value": 0.5, "text": "0.5"}


def test_one_non_number_leaves_the_whole_column_textual() -> None:
    body = "\n".join(f"| r{index} | {index} |" for index in range(24))
    content = f"| Name | Count |\n| --- | --- |\n{body}\n| r24 | n/a |"
    blocks = slack_blocks.render_blocks(content)
    assert all(cell["type"] == "raw_text" for row in blocks[0]["rows"] for cell in row)


def test_a_percentage_is_not_a_number() -> None:
    body = "\n".join(f"| r{index} | {index}% |" for index in range(25))
    blocks = slack_blocks.render_blocks(f"| Name | Share |\n| --- | --- |\n{body}")
    assert all(row[1]["type"] == "raw_text" for row in blocks[0]["rows"][1:])


def test_a_linked_number_keeps_the_destination_rather_than_the_sort() -> None:
    body = "\n".join(f"| r{index} | <https://example.invalid/{index}|{index}> |" for index in range(25))
    blocks = slack_blocks.render_blocks(f"| Name | Count |\n| --- | --- |\n{body}")
    assert blocks[0]["rows"][1][1]["type"] == "rich_text"


def test_a_numeric_header_is_still_a_header() -> None:
    body = "\n".join(f"| r{index} | {index} |" for index in range(25))
    blocks = slack_blocks.render_blocks(f"| 1 | 2 |\n| --- | --- |\n{body}")
    assert [cell["type"] for cell in blocks[0]["rows"][0]] == ["raw_text", "raw_text"]


def test_a_linked_header_cell_is_never_rich_text_in_a_data_table() -> None:
    body = "\n".join(f"| r{index} | {index} |" for index in range(25))
    content = f"| <https://example.invalid|Name> | Count |\n| --- | --- |\n{body}"
    blocks = slack_blocks.render_blocks(content)
    assert blocks[0]["rows"][0][0] == {"type": "raw_text", "text": "Name"}


# --- what could not be probed --------------------------------------------


def test_a_data_table_omits_column_settings_that_a_plain_table_keeps() -> None:
    header = "| L | R |\n| :-- | --: |\n"
    short = _basic(header + "| a | b |")
    assert short is not None and short[0]["type"] == "table"
    assert short[0]["column_settings"] == [{"align": "left"}, {"align": "right"}]

    long = slack_blocks.render_blocks(header + "| a | b |")
    assert long is not None and long[0]["type"] == "data_table"
    assert "column_settings" not in long[0]


def test_a_data_table_leaves_the_row_header_column_at_the_default() -> None:
    blocks = slack_blocks.render_blocks(_long_table())
    assert "row_header_column_index" not in blocks[0]


# --- one budget per block type -------------------------------------------


def test_short_tables_are_still_held_to_the_plain_table_budget() -> None:
    one = "| Name | Note |\n| --- | --- |\n" + "\n".join(f"| r{index} | {'w' * 40} |" for index in range(20))
    content = "\n\n".join([one] * 12)
    assert 10_000 < sum(len(line) for line in content.splitlines()) < 40_000
    assert _basic(content) is None


def test_a_long_table_may_use_the_budget_a_short_one_may_not() -> None:
    body = "\n".join(f"| r{index} | {'w' * 40} |" for index in range(190))
    content = f"| Name | Note |\n| --- | --- |\n{body}"
    blocks = slack_blocks.render_blocks(content)
    assert blocks[0]["type"] == "data_table"


def test_a_data_table_over_the_row_ceiling_still_falls_back_to_text() -> None:
    body = "\n".join(f"| r{index} | {index} |" for index in range(slack_blocks.MAX_TABLE_ROWS))
    assert slack_blocks.render_blocks(f"| Name | Count |\n| --- | --- |\n{body}") is None
    body = "\n".join(f"| r{index} | {index} |" for index in range(slack_blocks.MAX_TABLE_ROWS - 1))
    assert slack_blocks.render_blocks(f"| Name | Count |\n| --- | --- |\n{body}") is not None


# --- the advanced tier: Block Kit an author wrote by hand -----------------


PIE = {
    "type": "data_visualization",
    "title": "Reviews",
    "chart": {
        "type": "pie",
        "segments": [{"label": "merged", "value": 7}, {"label": "open", "value": 3}],
    },
}


def _fence(payload, info: str = "blockkit") -> str:
    import json

    return f"```{info}\n{json.dumps(payload)}\n```"


def test_a_blockkit_fence_becomes_the_blocks_it_holds() -> None:
    blocks = slack_blocks.render_blocks(f"*Reviews*\n\n{_fence(PIE)}")
    assert [block["type"] for block in blocks] == ["section", "data_visualization"]
    assert blocks[1] == PIE
    # The source is gone: it was the instruction, not the content.
    assert "data_visualization" not in blocks[0]["text"]["text"]


@pytest.mark.parametrize("info", ["", "json", "slack-raw", "text"])
def test_every_fence_that_is_not_blockkit_stays_source(info: str) -> None:
    content = _fence(PIE, info)
    assert slack_blocks.render_blocks(content) is None


def test_a_word_after_the_language_is_ignored_rather_than_read() -> None:
    blocks = slack_blocks.render_blocks(_fence(PIE, "blockkit slack-blocks"))
    assert [block["type"] for block in blocks] == ["data_visualization"]


def test_the_language_is_matched_case_insensitively() -> None:
    assert slack_blocks.render_blocks(_fence(PIE, "BlockKit")) is not None


def test_a_fence_of_several_blocks_forwards_them_in_order() -> None:
    table = {
        "type": "data_table",
        "caption": "Roster",
        "rows": [[{"type": "raw_text", "text": "Name"}]],
    }
    blocks = slack_blocks.render_blocks(_fence([table, PIE]))
    assert [block["type"] for block in blocks] == ["data_table", "data_visualization"]


def test_the_block_kit_builder_export_shape_is_accepted() -> None:
    blocks = slack_blocks.render_blocks(_fence({"blocks": [PIE]}))
    assert [block["type"] for block in blocks] == ["data_visualization"]


# --- model-written Block Kit ----------------------------------------------


@pytest.mark.parametrize(
    "block",
    [
        {"type": "card", "text": "hello"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "hi"}},
        {"type": "task_card", "status": "complete"},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "hi"}]},
        {"type": "divider"},
    ],
)
def test_a_type_nothing_restricts_is_forwarded(block) -> None:
    assert slack_blocks.render_blocks(_fence(block)) == [block]


def test_a_block_without_a_type_is_still_not_a_block() -> None:
    assert slack_blocks.render_blocks(_fence({"text": "hello"})) is None
    assert slack_blocks.render_blocks(_fence({"type": "  "})) is None


def test_one_refused_block_declines_the_whole_fence() -> None:
    assert slack_blocks.render_blocks(_fence([PIE, {"type": "button"}])) is None


@pytest.mark.parametrize(
    "block",
    [
        {"type": "data_table", "action_id": "jiuwenswarm_answer:0"},
        {
            "type": "data_visualization",
            "chart": {"accessory": {"type": "button", "action_id": "approve"}},
        },
        {"type": "data_table", "rows": [[{"elements": [{"action_id": "x"}]}]]},
        {"type": "data_table", "caption": "ok", "ACTION_ID": "x"},
    ],
)
def test_a_block_carrying_an_action_id_anywhere_is_refused(block) -> None:
    assert slack_blocks.render_blocks(_fence(block)) is None


def test_a_button_without_an_action_id_is_refused_on_its_type() -> None:
    bare_button = {
        "type": "actions",
        "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Approve"}}],
    }
    assert slack_blocks.render_blocks(_fence(bare_button)) is None


@pytest.mark.parametrize("kind", ["actions", "input", "overflow", "static_select", "workflow_button"])
def test_an_interactive_type_is_refused_wherever_it_sits(kind: str) -> None:
    assert slack_blocks.render_blocks(_fence({"type": kind})) is None
    assert slack_blocks.render_blocks(_fence({"type": "section", "accessory": {"type": kind}})) is None


def test_interactive_blocks_are_refused() -> None:
    button = {"type": "actions", "elements": [{"type": "button", "action_id": "a"}]}
    assert slack_blocks.render_blocks(_fence(button)) is None


# --- what a bad fence does -----------------------------------------------


@pytest.mark.parametrize("body", ["{not json at all", "[]", "null", '"a string"', "[1, 2, 3]", ""])
def test_a_fence_that_is_not_blocks_stays_on_screen_as_source(body: str) -> None:
    content = f"```blockkit\n{body}\n```"
    assert slack_blocks.render_blocks(content) is None


def test_a_bad_fence_beside_a_table_costs_only_itself() -> None:
    content = f"```blockkit\n{{oops\n```\n\n{TABLE}"
    blocks = slack_blocks.render_blocks(content)
    assert [block["type"] for block in blocks] == ["section", "data_table"]
    assert "oops" in blocks[0]["text"]["text"]


def test_an_unclosed_fence_is_still_verbatim_prose() -> None:
    content = f'```blockkit\n{{"type": "data_table"}}\n\n{TABLE}'
    assert slack_blocks.render_blocks(content) is None


# --- the two-chart ceiling ------------------------------------------------


def test_two_charts_in_one_message_render() -> None:
    content = f"{_fence(PIE)}\n\n{_fence(PIE)}"
    blocks = slack_blocks.render_blocks(content)
    assert len(blocks) == 2


def test_a_third_chart_declines_the_message_rather_than_being_refused() -> None:
    content = "\n\n".join([_fence(PIE)] * 3)
    assert slack_blocks.render_blocks(content) is None


# --- the basic tier: a mermaid pie chart ---------------------------------


def _mermaid(body: str, info: str = "mermaid") -> str:
    return f"```{info}\n{body}\n```"


PIE_SOURCE = 'pie showData\n    title Reviews this week\n    "merged" : 7\n    "open" : 3'


def test_a_mermaid_pie_becomes_a_chart() -> None:
    blocks = slack_blocks.render_blocks(_mermaid(PIE_SOURCE))
    assert blocks == [
        {
            "type": "data_visualization",
            "title": "Reviews this week",
            "chart": {
                "type": "pie",
                "segments": [
                    {"label": "merged", "value": 7},
                    {"label": "open", "value": 3},
                ],
            },
        }
    ]


@pytest.mark.parametrize("info", ["slack-raw", ""])
def test_a_diagram_is_shown_as_source_by_asking_for_source(info: str) -> None:
    assert slack_blocks.render_blocks(_mermaid(PIE_SOURCE, info)) is None


def test_a_chart_with_no_title_takes_the_heading_it_sits_under() -> None:
    source = 'pie\n    "merged" : 7'
    blocks = slack_blocks.render_blocks(f"*Review outcomes*\n\n{_mermaid(source)}")
    assert blocks[-1]["title"] == "Review outcomes"


def test_a_chart_with_no_title_and_no_heading_still_has_one() -> None:
    blocks = slack_blocks.render_blocks(_mermaid('pie\n    "merged" : 7'))
    assert blocks[0]["title"] == slack_blocks.DEFAULT_CHART_TITLE


def test_comments_and_accessibility_directives_are_not_data() -> None:
    source = '%% written by the report script\npie\n    accTitle: Reviews\n    "merged" : 7'
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert blocks[0]["chart"]["segments"] == [{"label": "merged", "value": 7}]


def test_unquoted_labels_and_decimal_values_are_read() -> None:
    source = "pie\n    Calcium : 42.96\n    Potassium : 50"
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert blocks[0]["chart"]["segments"] == [
        {"label": "Calcium", "value": 42.96},
        {"label": "Potassium", "value": 50},
    ]


# --- pie and xychart-beta, and no other diagram --------------------------


@pytest.mark.parametrize(
    "source",
    [
        "graph TD\n    A --> B",
        "flowchart LR\n    A --> B",
        "sequenceDiagram\n    A ->> B: hi",
        "gantt\n    title A\n    section S\n    task :a1, 2024-01-01, 30d",
        "erDiagram\n    A ||--o{ B : has",
        "stateDiagram-v2\n    [*] --> Still",
    ],
)
def test_a_diagram_that_is_not_a_chart_is_not_translated(source: str) -> None:
    assert slack_blocks.render_blocks(_mermaid(source)) is None


def test_a_line_the_parser_does_not_recognise_declines_the_whole_chart() -> None:
    source = 'pie\n    "merged" : 7\n    this line is not a segment'
    assert slack_blocks.render_blocks(_mermaid(source)) is None


# --- Slack's own bounds on a chart ---------------------------------------


def test_a_value_at_or_below_zero_declines_the_chart() -> None:
    for value in ("0", "-3"):
        source = f'pie\n    "merged" : 7\n    "open" : {value}'
        assert slack_blocks.render_blocks(_mermaid(source)) is None


def test_twelve_segments_render_and_thirteen_do_not() -> None:
    def chart(count: int) -> str:
        rows = "\n".join(f'    "s{index}" : {index + 1}' for index in range(count))
        return _mermaid(f"pie\n{rows}")

    assert slack_blocks.render_blocks(chart(slack_blocks.MAX_CHART_SEGMENTS)) is not None
    assert slack_blocks.render_blocks(chart(slack_blocks.MAX_CHART_SEGMENTS + 1)) is None


def test_an_empty_pie_is_not_a_chart() -> None:
    assert slack_blocks.render_blocks(_mermaid("pie\n    title Nothing")) is None


def test_a_long_label_is_clamped_rather_than_declined() -> None:
    source = f'pie\n    "{"w" * 60}" : 7'
    blocks = slack_blocks.render_blocks(_mermaid(source))
    label = blocks[0]["chart"]["segments"][0]["label"]
    assert len(label) == slack_blocks.MAX_CHART_LABEL_LENGTH


def test_a_long_title_is_clamped_too() -> None:
    source = f'pie\n    title {"w" * 200}\n    "merged" : 7'
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert len(blocks[0]["title"]) == slack_blocks.MAX_CHART_TITLE_LENGTH


# --- charts among everything else ----------------------------------------


def test_a_chart_and_a_table_share_one_message() -> None:
    content = f"*Reviews*\n\n{_mermaid(PIE_SOURCE)}\n\n*Roster*\n\n{TABLE}"
    blocks = slack_blocks.render_blocks(content)
    assert [block["type"] for block in blocks] == [
        "section",
        "data_visualization",
        "section",
        "data_table",
    ]


def test_the_two_chart_ceiling_counts_both_tiers_together() -> None:
    content = "\n\n".join([_mermaid(PIE_SOURCE), _mermaid(PIE_SOURCE), _fence(PIE)])
    assert slack_blocks.render_blocks(content) is None


# --- the basic tier: a mermaid xychart-beta bar or line chart -------------


XYCHART_SOURCE = 'xychart-beta\n    title "Sales Revenue"\n    x-axis "Month" [jan, feb, mar]\n    y-axis "Revenue (USD)" 4000 --> 11000\n    bar [5000, 6000, 7500]'


def test_a_mermaid_xychart_becomes_a_bar_chart() -> None:
    blocks = slack_blocks.render_blocks(_mermaid(XYCHART_SOURCE))
    assert blocks == [
        {
            "type": "data_visualization",
            "title": "Sales Revenue",
            "chart": {
                "type": "bar",
                "series": [
                    {
                        "name": "Revenue (USD)",
                        "data": [
                            {"label": "jan", "value": 5000},
                            {"label": "feb", "value": 6000},
                            {"label": "mar", "value": 7500},
                        ],
                    }
                ],
                "axis_config": {
                    "categories": ["jan", "feb", "mar"],
                    "x_label": "Month",
                    "y_label": "Revenue (USD)",
                },
            },
        }
    ]


def test_a_line_plot_becomes_a_line_chart() -> None:
    source = "xychart-beta\n    x-axis [a, b]\n    line [1, 2]"
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert blocks[0]["chart"]["type"] == slack_blocks.LINE_CHART


def test_the_axis_titles_go_inside_axis_config() -> None:
    blocks = slack_blocks.render_blocks(_mermaid(XYCHART_SOURCE))
    chart = blocks[0]["chart"]
    assert chart["axis_config"]["x_label"] == "Month"
    assert chart["axis_config"]["y_label"] == "Revenue (USD)"
    assert "x_label" not in chart
    assert "y_label" not in chart


def test_the_bare_xychart_keyword_is_read_too() -> None:
    source = "xychart\n    x-axis [a, b]\n    bar [1, 2]"
    assert slack_blocks.render_blocks(_mermaid(source)) is not None


def test_a_named_plot_names_its_series() -> None:
    source = 'xychart-beta\n    x-axis [a, b]\n    line "P50" [1, 2]\n    line "P99" [3, 4]'
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert [series["name"] for series in blocks[0]["chart"]["series"]] == ["P50", "P99"]


def test_a_lone_unnamed_plot_is_named_for_the_y_axis() -> None:
    source = 'xychart-beta\n    y-axis "Latency"\n    x-axis [a, b]\n    bar [1, 2]'
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert blocks[0]["chart"]["series"][0]["name"] == "Latency"


def test_a_lone_unnamed_plot_with_no_y_axis_is_named_for_its_kind() -> None:
    source = "xychart-beta\n    x-axis [a, b]\n    bar [1, 2]"
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert blocks[0]["chart"]["series"][0]["name"] == "Bar"


def test_several_unnamed_plots_are_numbered() -> None:
    source = "xychart-beta\n    x-axis [a, b]\n    bar [1, 2]\n    bar [3, 4]"
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert [series["name"] for series in blocks[0]["chart"]["series"]] == [
        "Series 1",
        "Series 2",
    ]


def test_vertical_is_accepted_and_horizontal_is_not() -> None:
    body = "\n    x-axis [a, b]\n    bar [1, 2]"
    assert slack_blocks.render_blocks(_mermaid(f"xychart-beta vertical{body}")) is not None
    assert slack_blocks.render_blocks(_mermaid(f"xychart-beta horizontal{body}")) is None


def test_a_y_axis_range_is_dropped_rather_than_declined() -> None:
    source = 'xychart-beta\n    y-axis "ms" 0 --> 100\n    x-axis [a]\n    bar [1]'
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert blocks[0]["chart"]["axis_config"]["y_label"] == "ms"


def test_quoted_categories_may_hold_a_comma() -> None:
    source = 'xychart-beta\n    x-axis ["a, b", c]\n    bar [1, 2]'
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert blocks[0]["chart"]["axis_config"]["categories"] == ["a, b", "c"]


def test_an_xychart_takes_the_heading_it_sits_under_when_it_has_no_title() -> None:
    source = "xychart-beta\n    x-axis [a, b]\n    bar [1, 2]"
    blocks = slack_blocks.render_blocks(f"*Weekly runs*\n\n{_mermaid(source)}")
    assert blocks[-1]["title"] == "Weekly runs"


def test_comments_are_not_data_in_an_xychart_either() -> None:
    source = "%% emitted by the report script\nxychart-beta\n    accTitle: Runs\n    x-axis [a, b]\n    bar [1, 2]"
    assert slack_blocks.render_blocks(_mermaid(source)) is not None


# --- what an xychart declines --------------------------------------------


@pytest.mark.parametrize(
    ("source", "why"),
    [
        (
            "xychart-beta\n    bar [1, 2]",
            "no x-axis, so there are no categories to plot against",
        ),
        (
            'xychart-beta\n    x-axis "Month" 0 --> 100\n    bar [1, 2]',
            "a numeric x-axis range: Slack's x-axis is a category list, not a scale",
        ),
        (
            "xychart-beta\n    x-axis [a, b]",
            "no plot at all",
        ),
        (
            "xychart-beta\n    x-axis [a, b]\n    bar [1, 2]\n    line [3, 4]",
            "mermaid draws a combination chart; Slack picks one type per block",
        ),
        (
            "xychart-beta\n    x-axis [a, b, c]\n    bar [1, 2]",
            "fewer values than categories, and a zero will not be invented",
        ),
        (
            "xychart-beta\n    x-axis [a, b]\n    bar [1, 2, 3]",
            "more values than categories",
        ),
        (
            "xychart-beta\n    x-axis [a, b]\n    bar []",
            "an empty plot",
        ),
        (
            "xychart-beta\n    x-axis []\n    bar [1]",
            "an empty category list",
        ),
        (
            "xychart-beta\n    x-axis [a, b]\n    bar [one, two]",
            "values that are not numbers",
        ),
        (
            'xychart-beta\n    x-axis [a, b]\n    line [540 "PaLM", 65 "LLaMA"]',
            "mermaid 11.16 per-point labels, which Slack has nowhere to put",
        ),
        (
            "xychart-beta\n    x-axis [a, b]\n    scatter [1, 2]",
            "a plot keyword mermaid does not have and this parser must not invent",
        ),
        (
            "xychart-beta\n    x-axis [a]\n    y-axis [b]\n    bar [1]",
            "a y-axis with categories, which mermaid's grammar does not allow",
        ),
        (
            'xychart-beta\n    title "A"\n    title "B"\n    x-axis [a]\n    bar [1]',
            "two titles",
        ),
        (
            "xychart-beta\n    x-axis [a]\n    x-axis [b]\n    bar [1]",
            "two x-axes",
        ),
        (
            "xychart-beta\n    x-axis [a]\n    bar [1]\n    suddenly prose",
            "a line the parser does not recognise",
        ),
    ],
)
def test_an_xychart_declines_rather_than_guessing(source: str, why: str) -> None:
    assert slack_blocks.render_blocks(_mermaid(source)) is None, why


# --- Slack's bounds on a series chart ------------------------------------


def _xychart(categories: int, plots: int = 1) -> str:
    labels = ", ".join(f"c{index}" for index in range(categories))
    values = ", ".join(["1"] * categories)
    rows = "\n".join(f'    bar "s{index}" [{values}]' for index in range(plots))
    return _mermaid(f"xychart-beta\n    x-axis [{labels}]\n{rows}")


def test_twenty_categories_render_and_twenty_one_do_not() -> None:
    limit = slack_blocks.MAX_CHART_CATEGORIES
    assert slack_blocks.render_blocks(_xychart(limit)) is not None
    assert slack_blocks.render_blocks(_xychart(limit + 1)) is None


def test_twelve_series_render_and_thirteen_do_not() -> None:
    limit = slack_blocks.MAX_CHART_SERIES
    assert slack_blocks.render_blocks(_xychart(2, plots=limit)) is not None
    assert slack_blocks.render_blocks(_xychart(2, plots=limit + 1)) is None


def test_a_long_category_label_is_clamped() -> None:
    source = f'xychart-beta\n    x-axis ["{"w" * 60}"]\n    bar [1]'
    blocks = slack_blocks.render_blocks(_mermaid(source))
    label = blocks[0]["chart"]["axis_config"]["categories"][0]
    assert len(label) == slack_blocks.MAX_CHART_LABEL_LENGTH
    # A data point finds its category by label, so the two must still agree
    # after clamping or Slack refuses the chart.
    assert blocks[0]["chart"]["series"][0]["data"][0]["label"] == label


def test_two_categories_that_clamp_to_the_same_label_decline() -> None:
    stem = "w" * 20
    source = f'xychart-beta\n    x-axis ["{stem}a", "{stem}b"]\n    bar [1, 2]'
    assert slack_blocks.render_blocks(_mermaid(source)) is None


def test_two_series_that_clamp_to_the_same_name_decline() -> None:
    stem = "w" * 20
    source = "\n".join(
        [
            "xychart-beta",
            "    x-axis [a]",
            f'    bar "{stem}1" [1]',
            f'    bar "{stem}2" [2]',
        ]
    )
    assert slack_blocks.render_blocks(_mermaid(source)) is None


def test_a_long_axis_title_is_clamped_to_the_axis_ceiling() -> None:
    source = f'xychart-beta\n    x-axis "{"w" * 200}" [a]\n    bar [1]'
    blocks = slack_blocks.render_blocks(_mermaid(source))
    label = blocks[0]["chart"]["axis_config"]["x_label"]
    assert len(label) == slack_blocks.MAX_AXIS_LABEL_LENGTH


def test_an_xychart_title_is_clamped_to_the_title_ceiling() -> None:
    source = f'xychart-beta\n    title "{"w" * 200}"\n    x-axis [a]\n    bar [1]'
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert len(blocks[0]["title"]) == slack_blocks.MAX_CHART_TITLE_LENGTH


def test_a_negative_value_is_allowed_on_a_series_chart() -> None:
    source = "xychart-beta\n    x-axis [a, b]\n    bar [-3, 4]"
    blocks = slack_blocks.render_blocks(_mermaid(source))
    assert blocks[0]["chart"]["series"][0]["data"][0]["value"] == -3


# --- the basic tier: a Vega-Lite specification ---------------------------


def _vega(spec: dict, info: str = "vega-lite") -> str:
    return f"```{info}\n{json.dumps(spec)}\n```"


VEGA_BAR = {
    "mark": "bar",
    "title": "Weekly runs",
    "data": {
        "values": [
            {"day": "Mon", "runs": 3},
            {"day": "Tue", "runs": 7},
            {"day": "Wed", "runs": 5},
        ]
    },
    "encoding": {
        "x": {"field": "day", "type": "nominal", "title": "Day"},
        "y": {"field": "runs", "type": "quantitative", "title": "Runs"},
    },
}

VEGA_ARC = {
    "mark": "arc",
    "data": {"values": [{"state": "merged", "n": 7}, {"state": "open", "n": 3}]},
    "encoding": {
        "theta": {"field": "n", "type": "quantitative"},
        "color": {"field": "state", "type": "nominal"},
    },
}


def test_a_vega_lite_spec_becomes_a_bar_chart() -> None:
    assert slack_blocks.render_blocks(_vega(VEGA_BAR)) == [
        {
            "type": "data_visualization",
            "title": "Weekly runs",
            "chart": {
                "type": "bar",
                "series": [
                    {
                        "name": "Runs",
                        "data": [
                            {"label": "Mon", "value": 3},
                            {"label": "Tue", "value": 7},
                            {"label": "Wed", "value": 5},
                        ],
                    }
                ],
                "axis_config": {
                    "categories": ["Mon", "Tue", "Wed"],
                    "x_label": "Day",
                    "y_label": "Runs",
                },
            },
        }
    ]


@pytest.mark.parametrize(
    ("mark", "chart"),
    [
        ("bar", slack_blocks.BAR_CHART),
        ("area", slack_blocks.AREA_CHART),
        ("line", slack_blocks.LINE_CHART),
    ],
)
def test_each_supported_cartesian_mark_maps_to_its_chart(mark: str, chart: str) -> None:
    blocks = slack_blocks.render_blocks(_vega({**VEGA_BAR, "mark": mark}))
    assert blocks[0]["chart"]["type"] == chart


def test_an_arc_mark_becomes_a_pie() -> None:
    blocks = slack_blocks.render_blocks(_vega(VEGA_ARC))
    assert blocks[0]["chart"] == {
        "type": "pie",
        "segments": [
            {"label": "merged", "value": 7},
            {"label": "open", "value": 3},
        ],
    }


def test_a_mark_object_is_read_the_same_as_a_mark_string() -> None:
    spec = {**VEGA_BAR, "mark": {"type": "area", "opacity": 0.5}}
    blocks = slack_blocks.render_blocks(_vega(spec))
    assert blocks[0]["chart"]["type"] == slack_blocks.AREA_CHART


def test_a_colour_channel_splits_the_rows_into_series() -> None:
    spec = {
        "mark": "line",
        "data": {
            "values": [
                {"d": "Mon", "v": 1, "tier": "free"},
                {"d": "Tue", "v": 2, "tier": "free"},
                {"d": "Mon", "v": 5, "tier": "paid"},
                {"d": "Tue", "v": 6, "tier": "paid"},
            ]
        },
        "encoding": {
            "x": {"field": "d", "type": "ordinal"},
            "y": {"field": "v", "type": "quantitative"},
            "color": {"field": "tier", "type": "nominal"},
        },
    }
    blocks = slack_blocks.render_blocks(_vega(spec))
    chart = blocks[0]["chart"]
    assert [series["name"] for series in chart["series"]] == ["free", "paid"]
    assert chart["axis_config"]["categories"] == ["Mon", "Tue"]
    assert chart["series"][1]["data"] == [
        {"label": "Mon", "value": 5},
        {"label": "Tue", "value": 6},
    ]


def test_the_axis_titles_default_to_the_field_names() -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    del spec["encoding"]["x"]["title"]
    del spec["encoding"]["y"]["title"]
    blocks = slack_blocks.render_blocks(_vega(spec))
    assert blocks[0]["chart"]["axis_config"]["x_label"] == "day"
    assert blocks[0]["chart"]["axis_config"]["y_label"] == "runs"


def test_an_explicit_null_title_hides_the_axis_label() -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"]["x"]["title"] = None
    blocks = slack_blocks.render_blocks(_vega(spec))
    assert "x_label" not in blocks[0]["chart"]["axis_config"]


def test_a_title_object_is_read_as_well_as_a_title_string() -> None:
    spec = {**VEGA_BAR, "title": {"text": "From an object"}}
    blocks = slack_blocks.render_blocks(_vega(spec))
    assert blocks[0]["title"] == "From an object"


def test_a_multi_line_title_is_joined_rather_than_truncated() -> None:
    spec = {**VEGA_BAR, "title": {"text": ["Weekly", "runs"]}}
    blocks = slack_blocks.render_blocks(_vega(spec))
    assert blocks[0]["title"] == "Weekly runs"


def test_a_spec_with_no_title_takes_the_heading_it_sits_under() -> None:
    spec = {key: value for key, value in VEGA_BAR.items() if key != "title"}
    blocks = slack_blocks.render_blocks(f"*Review outcomes*\n\n{_vega(spec)}")
    assert blocks[-1]["title"] == "Review outcomes"


def test_a_numeric_category_is_rendered_as_written() -> None:
    spec = {
        "mark": "bar",
        "data": {"values": [{"q": 1, "v": 4}, {"q": 2, "v": 9}]},
        "encoding": {
            "x": {"field": "q", "type": "ordinal"},
            "y": {"field": "v", "type": "quantitative"},
        },
    }
    blocks = slack_blocks.render_blocks(_vega(spec))
    assert blocks[0]["chart"]["axis_config"]["categories"] == ["1", "2"]


# --- what a Vega-Lite spec declines --------------------------------------


@pytest.mark.parametrize(
    "mark",
    [
        "point",
        "circle",
        "square",
        "tick",
        "rect",
        "rule",
        "text",
        "trail",
        "geoshape",
        "image",
        "boxplot",
        "errorbar",
        "errorband",
        "invented",
    ],
)
def test_an_unsupported_mark_declines_rather_than_being_approximated(mark: str) -> None:
    assert slack_blocks.render_blocks(_vega({**VEGA_BAR, "mark": mark})) is None


def test_an_external_data_url_declines() -> None:
    spec = {**VEGA_BAR, "data": {"url": "https://example.com/runs.json"}}
    assert slack_blocks.render_blocks(_vega(spec)) is None


@pytest.mark.parametrize(
    "data",
    [
        {"name": "runs"},
        {"sequence": {"start": 0, "stop": 10}},
        {"values": [], "format": {"type": "json"}},
        {"values": []},
        {"values": [[1, 2], [3, 4]]},
        {"values": "a,b\n1,2"},
        {},
    ],
)
def test_data_this_module_cannot_read_declines(data: object) -> None:
    assert slack_blocks.render_blocks(_vega({**VEGA_BAR, "data": data})) is None


@pytest.mark.parametrize("key", ["layer", "facet", "repeat", "concat", "hconcat", "vconcat", "spec"])
def test_a_spec_that_is_more_than_one_chart_declines(key: str) -> None:
    assert slack_blocks.render_blocks(_vega({**VEGA_BAR, key: [VEGA_BAR]})) is None


@pytest.mark.parametrize("channel", ["row", "column", "facet"])
def test_a_faceting_channel_declines(channel: str) -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"][channel] = {"field": "day", "type": "nominal"}
    assert slack_blocks.render_blocks(_vega(spec)) is None


def test_a_transform_declines() -> None:
    spec = {**VEGA_BAR, "transform": [{"filter": "datum.runs > 3"}]}
    assert slack_blocks.render_blocks(_vega(spec)) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("aggregate", "mean"),
        ("bin", True),
        ("timeUnit", "month"),
        ("stack", "normalize"),
    ],
)
def test_a_derived_encoding_declines(key: str, value: object) -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"]["y"][key] = value
    assert slack_blocks.render_blocks(_vega(spec)) is None


def test_an_explicit_sort_on_x_orders_the_categories() -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"]["x"]["sort"] = ["Wed", "Mon", "Tue"]
    blocks = slack_blocks.render_blocks(_vega(spec))
    chart = blocks[0]["chart"]
    assert chart["axis_config"]["categories"] == ["Wed", "Mon", "Tue"]
    assert [point["value"] for point in chart["series"][0]["data"]] == [5, 3, 7]


def test_a_sort_naming_a_category_the_data_lacks_ignores_it() -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"]["x"]["sort"] = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    blocks = slack_blocks.render_blocks(_vega(spec))
    assert blocks[0]["chart"]["axis_config"]["categories"] == ["Mon", "Tue", "Wed"]


def test_a_null_sort_is_the_order_the_rows_arrived_in() -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"]["x"]["sort"] = None
    assert slack_blocks.render_blocks(_vega(spec)) == slack_blocks.render_blocks(_vega(VEGA_BAR))


def test_a_sort_that_leaves_a_category_out_declines() -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"]["x"]["sort"] = ["Wed", "Mon"]
    assert slack_blocks.render_blocks(_vega(spec)) is None


@pytest.mark.parametrize("sort", ["-y", "ascending", {"field": "runs"}, 3])
def test_a_computed_sort_on_x_declines(sort: object) -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"]["x"]["sort"] = sort
    assert slack_blocks.render_blocks(_vega(spec)) is None


@pytest.mark.parametrize("channel", ["y", "color"])
def test_a_sort_on_any_channel_but_x_declines(channel: str) -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"].setdefault(channel, {"field": "runs", "type": "quantitative"})
    spec["encoding"][channel]["sort"] = ["Mon", "Tue", "Wed"]
    assert slack_blocks.render_blocks(_vega(spec)) is None


@pytest.mark.parametrize("channel", ["size", "shape", "opacity", "detail", "xOffset"])
def test_an_encoding_channel_that_is_neither_read_nor_inert_declines(
    channel: str,
) -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"][channel] = {"field": "runs", "type": "quantitative"}
    assert slack_blocks.render_blocks(_vega(spec)) is None


@pytest.mark.parametrize("channel", ["tooltip", "description", "href", "key"])
def test_an_inert_channel_is_ignored_rather_than_declined(channel: str) -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"][channel] = {"field": "runs", "type": "quantitative"}
    assert slack_blocks.render_blocks(_vega(spec)) is not None


def test_a_quantitative_x_declines() -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["encoding"]["x"]["type"] = "quantitative"
    assert slack_blocks.render_blocks(_vega(spec)) is None


def test_an_undeclared_numeric_x_declines_as_the_quantitative_one_it_would_be() -> None:
    spec = {
        "mark": "line",
        "data": {"values": [{"x": 1, "y": 4}, {"x": 100, "y": 9}]},
        "encoding": {"x": {"field": "x"}, "y": {"field": "y"}},
    }
    assert slack_blocks.render_blocks(_vega(spec)) is None


def test_a_non_quantitative_y_declines_as_a_horizontal_bar_chart() -> None:
    spec = {
        "mark": "bar",
        "data": {"values": [{"day": "Mon", "runs": 3}]},
        "encoding": {
            "x": {"field": "runs", "type": "quantitative"},
            "y": {"field": "day", "type": "nominal"},
        },
    }
    assert slack_blocks.render_blocks(_vega(spec)) is None


def test_a_series_with_a_gap_declines_rather_than_being_filled_with_a_zero() -> None:
    spec = {
        "mark": "bar",
        "data": {
            "values": [
                {"d": "Mon", "v": 1, "s": "a"},
                {"d": "Tue", "v": 2, "s": "a"},
                {"d": "Mon", "v": 5, "s": "b"},
            ]
        },
        "encoding": {
            "x": {"field": "d", "type": "ordinal"},
            "y": {"field": "v", "type": "quantitative"},
            "color": {"field": "s", "type": "nominal"},
        },
    }
    assert slack_blocks.render_blocks(_vega(spec)) is None


VEGA_GAPPED = {
    "mark": "bar",
    "data": {
        "values": [
            {"d": "Mon", "v": 1, "s": "a"},
            {"d": "Tue", "v": 2, "s": "a"},
            {"d": "Mon", "v": 5, "s": "b"},
        ]
    },
    "encoding": {
        "x": {"field": "d", "type": "ordinal"},
        "y": {"field": "v", "type": "quantitative"},
        "color": {"field": "s", "type": "nominal"},
    },
}


def test_a_gapped_colour_grid_costs_only_its_own_fence() -> None:
    message = f"Here is the split.\n\n{_vega(VEGA_GAPPED)}\n\n{TABLE}"
    blocks = slack_blocks.render_blocks(message)
    assert len(_tables(blocks)) == 1
    assert not [block for block in blocks if block["type"] == "data_visualization"]
    # And the spec itself is on screen as the source the author can see is undrawn.
    prose = "".join(block["text"]["text"] for block in _sections(blocks))
    assert "```vega-lite" in prose
    assert '"mark": "bar"' in prose


VEGA_TWO_SERIES = {
    "mark": "bar",
    "data": {
        "values": [
            {"d": "Mon", "v": 1, "s": "a"},
            {"d": "Tue", "v": 2, "s": "a"},
            {"d": "Mon", "v": 5, "s": "b"},
            {"d": "Tue", "v": 6, "s": "b"},
        ]
    },
    "encoding": {
        "x": {"field": "d", "type": "ordinal"},
        "y": {"field": "v", "type": "quantitative"},
        "color": {"field": "s", "type": "nominal"},
    },
}


def test_a_fence_that_named_no_rendering_language_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=slack_blocks.logger.name):
        slack_blocks.render_blocks(f"{TABLE}\n\n```python\nprint(1)\n```")
    assert "outside what this connector can render" not in caplog.text


def test_the_streaming_probe_does_not_report_a_decline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = _vega(VEGA_GAPPED)

    with caplog.at_level(logging.INFO, logger=slack_blocks.logger.name):
        for _ in range(5):
            slack_blocks.streamable_prose_prefix(message)

    assert "outside what this connector can render" not in caplog.text


def test_the_compose_time_call_still_reports_it_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = _vega(VEGA_GAPPED)

    with caplog.at_level(logging.INFO, logger=slack_blocks.logger.name):
        for _ in range(5):
            slack_blocks.streamable_prose_prefix(message)
        assert slack_blocks.render_blocks(message) is None

    declines = [record for record in caplog.records if "outside what this connector can render" in record.getMessage()]
    assert len(declines) == 1


def test_a_colour_repeating_the_x_field_is_dropped_rather_than_declining() -> None:
    spec = {
        "mark": "bar",
        "data": {
            "values": [
                {"day": "Mon", "count": 3},
                {"day": "Tue", "count": 5},
                {"day": "Wed", "count": 2},
                {"day": "Thu", "count": 7},
                {"day": "Fri", "count": 4},
            ]
        },
        "encoding": {
            "x": {"field": "day", "type": "nominal"},
            "y": {"field": "count", "type": "quantitative", "title": "Runs"},
            "color": {"field": "day", "type": "nominal", "legend": None},
        },
    }
    blocks = slack_blocks.render_blocks(_vega(spec))
    chart = blocks[0]["chart"]
    assert [series["name"] for series in chart["series"]] == ["Runs"]
    assert chart["axis_config"]["categories"] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert chart["series"][0]["data"][3] == {"label": "Thu", "value": 7}


def test_a_colour_on_its_own_field_is_still_a_series_dimension() -> None:
    blocks = slack_blocks.render_blocks(_vega(VEGA_TWO_SERIES))
    assert [series["name"] for series in blocks[0]["chart"]["series"]] == ["a", "b"]


def test_a_pie_is_untouched_by_the_repeated_colour_rule() -> None:
    blocks = slack_blocks.render_blocks(_vega(VEGA_ARC))
    assert [segment["label"] for segment in blocks[0]["chart"]["segments"]] == [
        "merged",
        "open",
    ]


def test_two_rows_for_one_series_and_category_decline() -> None:
    spec = {
        "mark": "bar",
        "data": {"values": [{"d": "Mon", "v": 1}, {"d": "Mon", "v": 2}]},
        "encoding": {
            "x": {"field": "d", "type": "ordinal"},
            "y": {"field": "v", "type": "quantitative"},
        },
    }
    assert slack_blocks.render_blocks(_vega(spec)) is None


@pytest.mark.parametrize(
    "spec",
    [
        {"mark": "bar", "data": {"values": [{"a": 1}]}},
        {"data": {"values": [{"a": 1}]}, "encoding": {}},
        {"mark": "bar", "encoding": {"x": {"field": "a"}}},
        {"mark": "bar", "data": {"values": [{"a": 1}]}, "encoding": {}},
    ],
)
def test_a_spec_missing_a_required_part_declines(spec: dict) -> None:
    assert slack_blocks.render_blocks(_vega(spec)) is None


def test_a_cartesian_spec_without_both_axes_declines() -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    del spec["encoding"]["y"]
    assert slack_blocks.render_blocks(_vega(spec)) is None


def test_an_arc_without_a_colour_channel_declines() -> None:
    spec = json.loads(json.dumps(VEGA_ARC))
    del spec["encoding"]["color"]
    assert slack_blocks.render_blocks(_vega(spec)) is None


def test_a_row_missing_a_field_declines() -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["data"]["values"].append({"day": "Thu"})
    assert slack_blocks.render_blocks(_vega(spec)) is None


@pytest.mark.parametrize("value", ["seven", None, True, [1], {"a": 1}])
def test_a_value_that_is_not_a_number_declines(value: object) -> None:
    spec = json.loads(json.dumps(VEGA_BAR))
    spec["data"]["values"][0]["runs"] = value
    assert slack_blocks.render_blocks(_vega(spec)) is None


def test_a_pie_segment_at_or_below_zero_declines() -> None:
    for value in (0, -3):
        spec = json.loads(json.dumps(VEGA_ARC))
        spec["data"]["values"][0]["n"] = value
        assert slack_blocks.render_blocks(_vega(spec)) is None


def test_a_malformed_spec_declines_to_source() -> None:
    assert slack_blocks.render_blocks("```vega-lite\n{not json at all\n```") is None
    assert slack_blocks.render_blocks("```vega-lite\n[1, 2, 3]\n```") is None
    assert slack_blocks.render_blocks("```vega-lite\n\n```") is None


# --- Slack's bounds, reached from a Vega-Lite spec ------------------------


def _vega_rows(categories: int, series: int = 1) -> dict:
    return {
        "mark": "bar",
        "data": {
            "values": [
                {"d": f"c{index}", "v": 1, "s": f"s{name}"} for name in range(series) for index in range(categories)
            ]
        },
        "encoding": {
            "x": {"field": "d", "type": "ordinal"},
            "y": {"field": "v", "type": "quantitative"},
            "color": {"field": "s", "type": "nominal"},
        },
    }


# --- the three chart sources share one message and one ceiling -----------


def test_a_vega_lite_chart_and_an_xychart_share_one_message() -> None:
    content = f"{_vega(VEGA_BAR)}\n\n{_mermaid(XYCHART_SOURCE)}"
    blocks = slack_blocks.render_blocks(content)
    assert [block["type"] for block in blocks] == [
        "data_visualization",
        "data_visualization",
    ]


def test_the_two_chart_ceiling_counts_every_source_together() -> None:
    content = "\n\n".join([_vega(VEGA_BAR), _mermaid(XYCHART_SOURCE), _mermaid(PIE_SOURCE)])
    assert slack_blocks.render_blocks(content) is None


def test_a_declined_chart_fence_costs_only_itself() -> None:
    content = f"```vega-lite\n{{not json\n```\n\n{TABLE}"
    blocks = slack_blocks.render_blocks(content)
    assert [block["type"] for block in blocks] == ["section", "data_table"]


def test_data_fallback_formats_table_and_chart_rows_consistently() -> None:
    blocks = [
        {
            "type": "data_table",
            "caption": "Counts",
            "rows": [
                [{"type": "raw_text", "text": "Name"}, {"type": "raw_text", "text": "Count"}],
                [{"type": "raw_text", "text": "alpha"}, {"type": "raw_number", "text": "1"}],
            ],
        },
        {
            "type": "data_visualization",
            "title": "Shares",
            "chart": {"type": "pie", "segments": [{"label": "alpha", "value": 1}]},
        },
        {
            "type": "data_visualization",
            "title": "Trend",
            "chart": {
                "type": "line",
                "axis_config": {"categories": ["Mon"]},
                "series": [{"name": "Count", "data": [{"label": "Mon", "value": 2}]}],
            },
        },
    ]
    assert slack_blocks.data_fallback_text(blocks) == (
        "*Counts*\n| Name | Count |\n| --- | --- |\n| alpha | 1 |\n\n"
        "*Shares*\n| Label | Value |\n| --- | --- |\n| alpha | 1 |\n\n"
        "*Trend*\n|  | Count |\n| --- | --- |\n| Mon | 2 |"
    )
