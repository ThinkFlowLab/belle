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
