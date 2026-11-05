"""Shared bounded string extraction works without a platform SDK."""

from belle_agent.structured_text import MAX_TEXT_DEPTH, MAX_TEXT_STRINGS, walk_strings_bounded


def test_nested_allowed_fields_keep_order_and_deduplicate():
    value = {"hidden": "omit", "children": [{"text": " hello "}, {"text": "hello"}, {"text": "world"}]}
    assert walk_strings_bounded(value, frozenset({"text"})) == (["hello", "world"], False)


def test_string_count_and_depth_report_truncation():
    strings, bounded = walk_strings_bounded([str(i) for i in range(MAX_TEXT_STRINGS + 1)], None)
    assert len(strings) == MAX_TEXT_STRINGS
    assert bounded
    nested = "hidden"
    for _ in range(MAX_TEXT_DEPTH + 1):
        nested = [nested]
    assert walk_strings_bounded(nested, None) == ([], True)


def test_empty_content_beyond_depth_does_not_report_truncation():
    nested = []
    for _ in range(MAX_TEXT_DEPTH + 1):
        nested = [nested]
    assert walk_strings_bounded(nested, None) == ([], False)
