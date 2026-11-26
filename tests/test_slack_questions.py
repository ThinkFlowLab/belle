"""Focused tests for Slack question presentation."""

import json
import logging

import pytest

from belle_agent.platforms.slack.questions import QuestionPresenter, QuestionRenderError, message_text


def _presenter() -> QuestionPresenter:
    return QuestionPresenter(
        action_id_prefix="jiuwenswarm_answer:",
        option_style=lambda intent, value: "danger" if value == "reject" else "",
        logger=logging.getLogger(__name__),
    )


def _posts(question: dict, dropped: list[str] = (), max_messages: int = 10) -> list[tuple[str, list[dict]]]:
    presenter = _presenter()
    declared, options = presenter.answer_choices(question, allow_text_input=True, request_id="r4")
    return presenter.answer_messages(
        request_id="r4", source="", session_id="s4", question=question,
        declared=declared, options=options, dropped=list(dropped), max_messages=max_messages,
    )


def _messages(question: dict, dropped: list[str] = ()) -> list[list[dict]]:
    return [blocks for _, blocks in _posts(question, dropped)]


def _answer(question: dict, dropped: list[str] = ()) -> list[dict]:
    """The blocks of a question that fits in one message."""
    (blocks,) = _messages(question, dropped)
    return blocks


def _approval(text: str) -> dict:
    return {"question": text, "options": [{"label": "Approve", "value": "approve"}, {"label": "Reject", "value": "reject"}]}


def _sections(blocks: list[dict]) -> list[str]:
    return [block["text"]["text"] for block in blocks if block["type"] == "section"]


def _texts(blocks: list[dict]) -> list[str]:
    return [block["text"]["text"] for block in blocks if block["type"] == "section"] + [
        element["text"] for block in blocks if block["type"] == "context" for element in block["elements"]
    ]


def _assert_whole(blocks: list[dict], *pieces: str) -> None:
    texts = _texts(blocks)
    assert all(len(text) <= 3000 for text in texts)
    shown = "".join(texts)
    assert "…" not in shown
    for piece in pieces:
        assert piece in shown


def test_options_keep_values_and_skip_unanswerable_other() -> None:
    presenter = _presenter()
    options = presenter.options(
        {
            "options": [
                {"label": "Approve", "value": "approve", "description": "Run once"},
                {"label": "Reject", "value": "reject"},
                {"label": "Other"},
            ]
        }
    )
    messages = presenter.answer_messages(
        request_id="r1",
        source="permission_interrupt",
        session_id="s1",
        question={"header": "Access", "question": "Proceed?"},
        declared=[],
        options=options,
        dropped=[],
        max_messages=10,
    )

    assert [option.value for option in options] == ["approve", "reject"]
    ((text, blocks),) = messages
    assert text == "Access: Proceed?"
    assert "Run once" in blocks[1]["text"]["text"]
    buttons = blocks[-1]["elements"]
    assert [button["action_id"] for button in buttons] == [
        "jiuwenswarm_answer:0",
        "jiuwenswarm_answer:1",
    ]
    assert "style" not in buttons[0]
    assert buttons[1]["style"] == "danger"
    assert json.loads(buttons[0]["value"])["request_id"] == "r1"


def test_identity_survives_oversized_optional_button_value() -> None:
    encoded = _presenter().encode_button_value(
        request_id="r3", index=0, value="x" * 4000, source="permission_interrupt", session_id="s3"
    )

    assert len(encoded) <= 2000
    assert json.loads(encoded) == {
        "request_id": "r3",
        "index": 0,
        "source": "permission_interrupt",
        "session_id": "s3",
    }


def test_prompt_up_to_the_section_limit_stays_one_whole_section() -> None:
    for size in (2999, 3000):
        assert _sections(_answer(_approval("x" * size))) == ["x" * size]


def test_long_approval_arguments_show_in_full_over_several_sections() -> None:
    command = "\n".join(f"echo line {index} " + "y" * 40 for index in range(120))
    text = f"Run the shell tool?\n```\n{command}\n```"
    assert len(text) > 5000

    sections = _sections(_answer(_approval(text)))

    assert len(sections) > 1
    assert all(len(section) <= 3000 for section in sections)
    assert all(section.startswith("```") and section.endswith("```") for section in sections[1:])
    shown = "\n".join(sections)
    assert all(line in shown for line in command.split("\n"))
    assert "…" not in shown


def test_a_long_option_description_shows_whole() -> None:
    blocks = _answer({"question": "Proceed?", "options": [{"label": "Approve", "value": "approve", "description": "d" * 5000}]})

    assert "".join(_texts(blocks)[1:]).count("d") == 5000
    _assert_whole(blocks)


def test_a_legend_over_the_section_limit_splits_over_sections() -> None:
    descriptions = [f"choice {index} " + "e" * 1000 for index in range(5)]
    options = [{"label": f"L{index}", "value": f"v{index}", "description": text} for index, text in enumerate(descriptions)]

    blocks = _answer({"question": "Pick one", "options": options})

    assert len(_sections(blocks)) > 2
    _assert_whole(blocks, *descriptions)


def test_a_note_over_the_context_limit_splits_over_context_blocks() -> None:
    note = "\n\n".join(f"note {index} " + "n" * 1500 for index in range(4))

    blocks = _answer({"question": "Name?", "note": note})

    assert sum(block["type"] == "context" for block in blocks) > 1
    assert blocks[-1]["type"] == "actions"
    _assert_whole(blocks, *note.split("\n\n"))


def test_a_long_list_of_other_questions_shows_whole_after_the_controls() -> None:
    dropped = [f"question-{index}-" + "q" * 500 for index in range(10)]

    blocks = _answer({"question": "First?", "options": [{"label": "Yes", "value": "yes"}]}, dropped)

    assert blocks[1]["type"] == "actions"
    assert {block["type"] for block in blocks[2:]} == {"context"}
    assert len(blocks) > 3
    _assert_whole(blocks, *dropped)


def _assert_controls_come_last(messages: list[list[dict]]) -> None:
    total = len(messages)
    assert total > 1
    assert all(len(blocks) <= 50 for blocks in messages)
    # Each text holds all of its blocks' text, with room for the notice of a refusal of the blocks.
    assert all(len(message_text(blocks)) <= 37500 for blocks in messages)
    assert all(block["type"] not in ("actions", "input") for blocks in messages[:-1] for block in blocks)
    actions = [block for block in messages[-1] if block["type"] == "actions"]
    assert json.loads(actions[-1]["elements"][0]["value"])["request_id"] == "r4"
    for number, blocks in enumerate(messages[:-1], 1):
        assert blocks[0]["text"]["text"] == f"*Part {number} of {total} of this question.*"
    assert messages[-1][0]["text"]["text"].startswith(
        f"*Part {total} of {total} of this question.* Parts 1 to {total - 1} are above in this thread."
    )


def test_each_part_of_a_split_question_has_the_text_of_its_blocks() -> None:
    posts = _posts(_approval("\n\n".join(f"p{index}-" + "p" * 2900 for index in range(60))))

    assert all(text == message_text(blocks) for text, blocks in posts)


def test_a_question_over_the_text_limit_of_one_message_splits_under_the_block_limit() -> None:
    text = "\n\n".join(f"p{index}-" + "p" * 2900 for index in range(15))

    messages = _messages(_approval(text))

    assert sum(len(blocks) for blocks in messages) < 50
    _assert_controls_come_last(messages)
    sections = [section for blocks in messages for section in _sections(blocks)]
    assert [section for section in sections if section.startswith("p")] == text.split("\n\n")


def test_a_question_over_the_message_limit_is_refused_with_its_size_and_the_setting() -> None:
    text = "\n\n".join(f"p{index}-" + "p" * 2900 for index in range(60))

    with pytest.raises(QuestionRenderError) as refused:
        _posts(_approval(text), max_messages=2)

    assert str(refused.value).startswith("the question has ")
    assert "Slack messages, over the 2 that platforms.slack.max_question_messages allows" in str(refused.value)


def test_a_cut_button_label_shows_in_full_in_the_legend() -> None:
    label = "Run " + "l" * 100

    blocks = _answer({"question": "Which?", "options": [{"label": label, "value": "v"}, {"label": "Short", "value": "s"}]})

    button = blocks[-1]["elements"][0]["text"]["text"]
    assert len(button) == 75 and button.endswith("…")
    assert _sections(blocks)[1] == f"*{label}*"


def test_a_prompt_over_the_block_limit_starts_in_earlier_messages() -> None:
    text = "\n\n".join(f"p{index}-" + "p" * 2900 for index in range(60))

    messages = _messages(_approval(text))

    _assert_controls_come_last(messages)
    sections = [section for blocks in messages for section in _sections(blocks)]
    assert [section for section in sections if section.startswith("p")] == text.split("\n\n")


def test_an_overflowing_approval_shows_what_approve_does_before_its_buttons() -> None:
    text = "\n\n".join("p" * 2900 for _ in range(45))
    descriptions = [f"choice {index} " + "e" * 2900 for index in range(4)]
    descriptions.append("Approve also deletes the repository")
    options = [{"label": f"L{index}", "value": f"v{index}", "description": body} for index, body in enumerate(descriptions)]

    messages = _messages({"question": text, "options": options}, ["later " + "q" * 3000])

    _assert_controls_come_last(messages)
    actions = next(index for index, block in enumerate(messages[-1]) if block["type"] == "actions")
    shown = "".join(text for blocks in messages[:-1] for text in _texts(blocks))
    shown += "".join(_texts(messages[-1][:actions]))
    for piece in [*descriptions, "p" * 2900]:
        assert piece in shown
    assert "…" not in shown
    assert "later " + "q" * 100 in "".join(text for blocks in messages for text in _texts(blocks))


def test_a_note_too_long_to_sit_between_the_inputs_and_submit_moves_before_them() -> None:
    note = "\n\n".join(f"note-{index}-" + "n" * 2900 for index in range(55))

    messages = _messages({"question": "Name?", "note": note})

    _assert_controls_come_last(messages)
    shown = [text for blocks in messages for text in _texts(blocks)]
    assert [text for text in shown if text.startswith("note-")] == note.split("\n\n")
    assert [block["type"] for block in messages[-1][-3:]] == ["divider", "input", "actions"]


def test_a_message_without_room_for_its_controls_is_refused() -> None:
    options = [{"label": f"o{index}", "value": f"v{index}"} for index in range(50 * 25)]

    with pytest.raises(QuestionRenderError, match=r"^its controls take 50 blocks, over the 49 that a Slack message holds"):
        _messages({"question": "Many?", "options": options})


def test_a_header_too_long_for_a_header_block_shows_whole_in_the_section() -> None:
    header = "h" * 200

    blocks = _answer({"header": header, "header_block": True, "question": "Proceed?", "options": [{"label": "Yes", "value": "yes"}]})

    assert all(block["type"] != "header" for block in blocks)
    _assert_whole(blocks, header, "Proceed?")


def test_the_fallback_text_keeps_the_whole_question() -> None:
    assert _presenter().fallback_text({"header": "Run", "question": "x" * 5000}) == "Run: " + "x" * 5000
