from belle_agent.platforms.rendering import markdown_cut_index, markdown_parts, measure_of


def _bytes(text: str) -> int:
    return len(text.encode())


def test_parts_fall_between_blocks_and_leave_nothing_out():
    text = "\n\n".join(f"Paragraph {n} " + "word " * 20 for n in range(6))

    found = markdown_parts(text, 300, len)

    assert len(found) > 1 and all(len(part) <= 300 for part in found)
    assert "\n\n".join(found) == text.strip().replace(" \n", "\n")


def test_a_code_block_too_large_for_one_part_is_closed_and_opened_again_in_each():
    text = "```python\n" + "\n".join(f"line_{n} = {n}" for n in range(60)) + "\n```"

    found = markdown_parts(text, 200, len)

    assert len(found) > 1
    assert all(part.startswith("```python\n") and part.endswith("\n```") and len(part) <= 200 for part in found)
    assert [line for part in found for line in part.split("\n")[1:-1]] == text.split("\n")[1:-1]


def test_an_oversized_fence_header_falls_back_to_bounded_plain_parts():
    text = "```" + "x" * 80 + "\nbody\n```"

    found = markdown_parts(text, 40, len)

    assert len(found) > 1
    assert all(len(part) <= 40 for part in found)
    assert "".join(found).replace("\n", "") == text.replace("\n", "")


def test_a_table_too_large_for_one_part_repeats_its_head_in_each():
    head = "| a | b |\n|---|---|"
    text = head + "\n" + "\n".join(f"| {n} | {n * n} |" for n in range(80))

    found = markdown_parts(text, 150, len)

    assert len(found) > 1 and all(part.startswith(head + "\n") and len(part) <= 150 for part in found)


def test_the_budget_is_counted_in_the_unit_the_measure_gives():
    text = "\n\n".join("é" * 40 for _ in range(4))

    assert len(markdown_parts(text, 100, len)) == 2
    assert len(markdown_parts(text, 100, _bytes)) == 4


def test_a_cut_falls_at_a_paragraph_break_outside_a_code_block():
    text = "intro\n\n```\none\n\ntwo\n```\n\nafter"

    cut = markdown_cut_index(text, len(text) - 3, len)

    assert (text[:cut].rstrip(), text[cut:].strip()) == ("intro\n\n```\none\n\ntwo\n```", "after")


def test_a_cut_with_no_paragraph_break_falls_at_a_word_end_that_fits():
    text = "alpha beta gamma delta"

    cut = markdown_cut_index(text, 12, len)

    assert text[:cut] == "alpha beta"


def test_a_measure_counts_characters_or_the_bytes_of_utf_8():
    assert (measure_of("chars")("é😀"), measure_of("bytes")("é😀")) == (2, 6)
