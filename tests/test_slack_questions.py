"""Focused tests for Slack question presentation."""

import json
import logging

from belle_agent.platforms.slack.questions import QuestionPresenter


def _presenter() -> QuestionPresenter:
    return QuestionPresenter(
        action_id_prefix="jiuwenswarm_answer:",
        option_style=lambda intent, value: "danger" if value == "reject" else "",
        logger=logging.getLogger(__name__),
    )


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
    blocks = presenter.option_blocks(
        request_id="r1",
        source="permission_interrupt",
        session_id="s1",
        question={"header": "Access", "question": "Proceed?"},
        options=options,
    )

    assert [option.value for option in options] == ["approve", "reject"]
    assert blocks is not None
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


def _approval_blocks(text: str) -> list[dict]:
    presenter = _presenter()
    question = {"question": text, "options": [{"label": "Approve", "value": "approve"}, {"label": "Reject", "value": "reject"}]}
    blocks = presenter.answer_blocks(
        request_id="r9", source="permission_interrupt", session_id="s9", question=question,
        declared=[], options=presenter.options(question), dropped=[],
    )
    assert blocks is not None
    assert blocks[-1]["type"] == "actions"
    assert json.loads(blocks[-1]["elements"][0]["value"])["request_id"] == "r9"
    return blocks


def _sections(blocks: list[dict]) -> list[str]:
    return [block["text"]["text"] for block in blocks if block["type"] == "section"]


def test_prompt_up_to_the_section_limit_stays_one_whole_section() -> None:
    for size in (2999, 3000):
        assert _sections(_approval_blocks("x" * size)) == ["x" * size]


def test_long_approval_arguments_show_in_full_over_several_sections() -> None:
    command = "\n".join(f"echo line {index} " + "y" * 40 for index in range(120))
    text = f"Run the shell tool?\n```\n{command}\n```"
    assert len(text) > 5000

    sections = _sections(_approval_blocks(text))

    assert len(sections) > 1
    assert all(len(section) <= 3000 for section in sections)
    assert all(section.startswith("```") and section.endswith("```") for section in sections[1:])
    shown = "\n".join(sections)
    assert all(line in shown for line in command.split("\n"))
    assert "…" not in shown


def test_prompt_over_the_block_limit_says_it_is_cut_before_the_controls() -> None:
    text = "\n\n".join("p" * 2900 for _ in range(60))

    blocks = _approval_blocks(text)

    assert len(blocks) == 50
    assert _sections(blocks)[:48] == ["p" * 2900] * 48
    notice = blocks[-2]["text"]["text"]
    assert "Not all of this question is shown" in notice
    assert f"{len(text)} characters in 60 parts" in notice
    assert "first 48 parts" in notice
