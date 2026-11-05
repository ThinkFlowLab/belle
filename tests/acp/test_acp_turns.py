"""The chunk rules: what each ACP update gives a Belle turn, and what its final text is."""

from acp import update_agent_message_text, update_agent_thought_text
from acp.schema import ImageContentBlock, ToolCallProgress, ToolCallStart

from belle_agent.backend import EVENT_STEP, EVENT_TEXT_DELTA, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT
from belle_agent.integrations.acp.turns import TurnText, translate


def said(text: str, message: str | None = None):
    update = update_agent_message_text(text)
    return update.model_copy(update={"message_id": message}) if message else update


def started(call: str, status: str = "pending", **fields):
    return ToolCallStart(session_update="tool_call", tool_call_id=call, title=f"run {call}", status=status, **fields)


def progressed(call: str, status: str | None, text: str = "", title: str | None = None):
    content = [{"type": "content", "content": {"type": "text", "text": text}}] if text else None
    return ToolCallProgress.model_validate(
        {"sessionUpdate": "tool_call_update", "toolCallId": call, "status": status, "content": content, "title": title}
    )


def play(*updates) -> tuple[list, TurnText]:
    turn = TurnText()
    return [event for update in updates for event in translate(update, turn)], turn


def test_the_chunks_of_one_message_are_its_text_in_order():
    events, turn = play(said("Hel", "m1"), said("lo", "m1"), said(" there"))
    assert [(event.kind, event.text) for event in events] == [(EVENT_TEXT_DELTA, t) for t in ("Hel", "lo", " there")]
    assert turn.final == "Hello there"


def test_a_new_message_id_starts_the_reply_again():
    events, turn = play(said("draft", "m1"), said("answer", "m2"), said(" more", "m2"))
    assert [event.kind for event in events] == [EVENT_TEXT_DELTA, EVENT_STEP, EVENT_TEXT_DELTA, EVENT_TEXT_DELTA]
    assert turn.final == "answer more"


def test_text_before_a_tool_call_is_no_part_of_the_final():
    events, turn = play(
        said("Let me look."), started("t1"), progressed("t1", "completed", "found it"), said("It is 4.")
    )
    assert [event.kind for event in events] == [
        EVENT_TEXT_DELTA,
        EVENT_STEP,
        EVENT_TOOL_CALLED,
        EVENT_TOOL_RESULT,
        EVENT_STEP,
        EVENT_TEXT_DELTA,
    ]
    assert turn.final == "It is 4."
    assert events[3].text == "found it" and events[3].data == {"name": "run t1", "call_id": "t1"}


def test_a_tool_call_update_replaces_the_fields_it_names_and_reports_one_result():
    events, _ = play(
        started("t1", raw_input={"cmd": "ls"}),
        progressed("t1", "in_progress", title="list files"),
        progressed("t1", "failed", "denied"),
        progressed("t1", "failed", "denied again"),
    )
    assert [event.kind for event in events] == [EVENT_TOOL_CALLED, EVENT_TOOL_RESULT]
    assert events[0].data == {"name": "run t1", "call_id": "t1", "arguments": {"cmd": "ls"}}
    assert events[1].data == {"name": "list files", "call_id": "t1", "failed": True}


def test_a_call_that_starts_ended_reports_its_result_at_once():
    events, _ = play(started("t1", status="completed"))
    assert [event.kind for event in events] == [EVENT_TOOL_CALLED, EVENT_TOOL_RESULT]


def test_thought_is_peek_text_and_never_part_of_the_final():
    events, turn = play(update_agent_thought_text("hmm"), said("yes"))
    assert events[0].data == {"thinking": True} and events[0].text == "hmm"
    assert turn.final == "yes"


def test_a_chunk_that_is_not_text_adds_nothing():
    image = update_agent_message_text("x").model_copy(
        update={"content": ImageContentBlock(type="image", data="AA==", mime_type="image/png")}
    )
    assert play(image) == ([], TurnText())
