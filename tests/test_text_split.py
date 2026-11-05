"""Long text splits at the most natural boundary that fits."""

from belle_agent.text_split import split_text


def test_text_splits_at_a_paragraph_then_a_line_then_a_word():
    assert split_text("one two\n\nthree four", 12) == ["one two", "three four"]
    assert split_text("one two\nthree four", 12) == ["one two", "three four"]
    assert split_text("one two three four", 12) == ["one two", "three four"]


def test_text_with_no_boundary_is_cut_at_the_limit_and_the_first_part_may_be_shorter():
    assert split_text("abcdefghij", 4) == ["abcd", "efgh", "ij"]
    assert split_text("abcdefghij", 4, first_limit=2) == ["ab", "cdef", "ghij"]
    assert split_text("short", 10) == ["short"]


def test_text_can_split_without_discarding_whitespace():
    content = "  one two\n\nthree four  "
    parts = split_text(content, 12, preserve_whitespace=True)
    assert "".join(parts) == content
    assert all(len(part) <= 12 for part in parts)


def test_text_does_not_make_a_blank_part_when_a_later_cut_can_keep_the_spaces():
    content = "A" + " " * 8 + "B"
    parts = split_text(content, 5, preserve_whitespace=True)
    assert "".join(parts) == content
    assert all(part.strip() for part in parts)
