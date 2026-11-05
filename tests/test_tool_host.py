"""The tool host binds each call to its conversation's turn and nothing else."""

import asyncio
import json

import pytest

from belle_agent.backend import VERDICT_ALLOW, VERDICT_ASK, VERDICT_DENY, ToolCall
from belle_agent.scopes import LEVEL_ASK
from belle_agent.tools.host import BoundTurn, ToolHost, request_metadata


class Toolkit:
    def __init__(self) -> None:
        self.seen: list[tuple[str, dict]] = []
        self.gate = asyncio.Event()

    def get_tool_definitions(self):
        return [
            {"name": name, "description": name, "input_params": {"type": "object", "properties": {}}}
            for name in ("post", "read", "wait", "explode")
        ]

    async def post(self, text: str, fail: bool = False) -> str:
        self.seen.append((text, request_metadata()))
        return json.dumps(
            {"ok": False, "error": "post_failed"} if fail else
            {"ok": True, "chat_id": "room", "message_id": "m1"}
        )

    async def read(self) -> str:
        return "plain text result"

    async def wait(self, text: str) -> str:
        await self.gate.wait()
        self.seen.append((text, request_metadata()))
        return json.dumps({"ok": True})

    async def explode(self) -> str:
        raise RuntimeError("secret detail")


def _host() -> tuple[ToolHost, Toolkit]:
    toolkit = Toolkit()
    return ToolHost([toolkit], posting_tool="post"), toolkit


def _call(conversation: str, name: str, **arguments) -> ToolCall:
    return ToolCall(conversation=conversation, name=name, arguments=arguments)


async def test_a_call_reads_its_own_turns_metadata_and_none_remains_after():
    host, toolkit = _host()
    host.bind("a", BoundTurn("r1", metadata={"where": "a"}))

    result = await host.call(_call("a", "post", text="hello"))

    assert json.loads(result) == {"ok": True, "chat_id": "room", "message_id": "m1"}
    assert toolkit.seen == [("hello", {"where": "a"})]
    assert request_metadata() == {}


async def test_overlapping_calls_keep_their_own_metadata():
    host, toolkit = _host()
    host.bind("a", BoundTurn("r1", metadata={"where": "a"}))
    host.bind("b", BoundTurn("r2", metadata={"where": "b"}))

    first = asyncio.create_task(host.call(_call("a", "wait", text="from a")))
    second = asyncio.create_task(host.call(_call("b", "wait", text="from b")))
    await asyncio.sleep(0)
    toolkit.gate.set()
    await asyncio.gather(first, second)

    assert sorted(toolkit.seen) == [("from a", {"where": "a"}), ("from b", {"where": "b"})]


async def test_metadata_is_cleared_after_a_tool_raises_and_the_error_is_bounded():
    host, _ = _host()
    host.bind("a", BoundTurn("r1", metadata={"where": "a"}))

    result = json.loads(await host.call(_call("a", "explode")))

    assert result == {"ok": False, "error": "tool_failed", "detail": "RuntimeError"}
    assert request_metadata() == {}


async def test_a_call_outside_a_running_turn_runs_nothing():
    host, toolkit = _host()
    host.bind("a", BoundTurn("r1"))

    assert (await host.authorize(_call("b", "post", text="x"))).action == VERDICT_DENY
    assert json.loads(await host.call(_call("b", "post", text="x")))["error"] == "tool_denied"
    host.release("a", "r1")
    assert json.loads(await host.call(_call("a", "post", text="x")))["error"] == "tool_denied"
    assert toolkit.seen == []


async def test_a_confirmed_destructive_call_only_runs_during_its_own_turn():
    class DestructiveToolkit(Toolkit):
        active_turn_only = frozenset({"post"})

    toolkit = DestructiveToolkit()
    host = ToolHost([toolkit])
    first = BoundTurn("r1")
    call = _call("a", "post", text="change")
    host.bind("a", first)

    assert json.loads(await host.run_confirmed(call, first))["ok"] is True
    host.release("a", "r1")
    assert json.loads(await host.run_confirmed(call, first))["error"] == "turn_ended"
    host.bind("a", BoundTurn("r2"))
    assert json.loads(await host.run_confirmed(call, first))["error"] == "turn_ended"
    assert [text for text, _ in toolkit.seen] == ["change"]


async def test_a_turn_being_stopped_makes_no_further_call():
    host, toolkit = _host()
    turn = BoundTurn("r1")
    host.bind("a", turn)
    assert json.loads(await host.call(_call("a", "post", text="while it runs"))) == {
        "ok": True, "chat_id": "room", "message_id": "m1",
    }

    # What the turn was running may not have noticed the stop yet.
    turn.stopped = True

    refused = json.loads(await host.call(_call("a", "post", text="after the stop")))
    assert (refused["ok"], refused["error"]) == (False, "tool_denied")
    assert [text for text, _ in toolkit.seen] == ["while it runs"]


async def test_a_later_turn_is_not_released_by_the_turn_it_replaced():
    host, toolkit = _host()
    host.bind("a", BoundTurn("r1"))
    host.bind("a", BoundTurn("r2", metadata={"turn": 2}))

    host.release("a", "r1")

    await host.call(_call("a", "post", text="x"))
    assert toolkit.seen == [("x", {"turn": 2})]


async def test_scope_levels_gate_the_call_and_shape_the_roster():
    host, toolkit = _host()
    host.bind("a", BoundTurn("r1", tools={"post": "deny", "read": "ask"}))

    assert (await host.authorize(_call("a", "post", text="x"))).action == VERDICT_DENY
    asked = await host.authorize(_call("a", "read"))
    assert (asked.action, asked.question.tool, asked.question.kind) == (VERDICT_ASK, "read", "approval")
    # A tool the host does not serve is the backend's own: only the scope speaks for it.
    assert (await host.authorize(_call("a", "bash"))).action == VERDICT_ALLOW
    assert json.loads(await host.call(_call("a", "post", text="x")))["error"] == "tool_denied"
    # Nothing approves a call yet, so a call that must ask does not run.
    assert json.loads(await host.call(_call("a", "read")))["error"] == "approval_required"
    assert toolkit.seen == []
    assert [definition["name"] for definition in host.definitions("a")] == ["read", "wait", "explode"]
    assert len(host.definitions("no turn")) == 4


async def test_unknown_tools_and_wrong_arguments_are_refused_before_anything_runs():
    host, toolkit = _host()
    host.bind("a", BoundTurn("r1"))

    assert json.loads(await host.call(_call("a", "bash")))["error"] == "unknown_tool"
    assert json.loads(await host.call(_call("a", "post", wrong="x")))["error"] == "invalid_arguments"
    assert toolkit.seen == []


async def test_only_a_posting_call_that_succeeded_counts_as_delivered():
    host, _ = _host()
    turn = BoundTurn("r1")
    host.bind("a", turn)

    assert await host.call(_call("a", "read")) == "plain text result"
    await host.call(_call("a", "post", text="x", fail=True))
    assert turn.delivered is False

    await host.call(_call("a", "post", text="x"))
    assert turn.delivered is True


async def test_post_without_confirmed_message_ids_does_not_count_as_delivered():
    class BrokenToolkit(Toolkit):
        async def post(self, text: str) -> str:
            return json.dumps({"ok": True, "chat_id": "room"})

    host = ToolHost([BrokenToolkit()], posting_tool="post")
    turn = BoundTurn("r1")
    host.bind("a", turn)

    await host.call(_call("a", "post", text="x"))
    assert not turn.delivered and not turn.posted


async def test_malformed_post_part_records_do_not_suppress_the_final_reply():
    class BrokenToolkit(Toolkit):
        async def post(self, text: str) -> str:
            return json.dumps(self.payload)

    toolkit = BrokenToolkit()
    host = ToolHost([toolkit], posting_tool="post")
    turn = BoundTurn("r1")
    host.bind("a", turn)

    for payload in (
        {"ok": True, "chat_id": "room", "message_id": "m1", "part_message_ids": ["m1", "m2"], "parts": 1},
        {"ok": True, "chat_id": "room", "message_id": "m1", "parts": 2},
        {"ok": True, "chat_id": "room", "message_id": "m1", "thread_id": 7},
    ):
        toolkit.payload = payload
        await host.call(_call("a", "post", text="x"))
        assert not turn.delivered and not turn.posted


async def test_posted_messages_include_each_part_and_its_thread():
    class PartsToolkit(Toolkit):
        async def post(self, text: str, reply_to_message_id: str = "") -> str:
            return json.dumps({
                "ok": True,
                "chat_id": "room",
                "message_id": "m1",
                "part_message_ids": ["m1", "m2"],
            })

    host = ToolHost([PartsToolkit()], posting_tool="post")
    turn = BoundTurn("r1", metadata={"belle_place": {"thread": "origin"}})
    host.bind("a", turn)

    await host.call(_call("a", "post", text="long", reply_to_message_id="chosen"))

    assert turn.posted == [
        {"conversation": "room", "message_id": "m1", "thread": "chosen"},
        {"conversation": "room", "message_id": "m2", "thread": "chosen"},
    ]


async def test_partial_post_records_sent_parts_and_keeps_the_failure():
    class PartialToolkit(Toolkit):
        async def post(self, text: str) -> str:
            return json.dumps({
                "ok": False, "error": "post_failed", "chat_id": "room", "message_id": "m1",
                "part_message_ids": ["m1", "m2"], "parts": 2, "thread_id": "root",
            })

    host = ToolHost([PartialToolkit()], posting_tool="post")
    turn = BoundTurn("r1")
    host.bind("a", turn)

    result = json.loads(await host.call(_call("a", "post", text="long")))

    assert result["ok"] is False and result["error"] == "post_failed"
    assert turn.delivered
    assert turn.posted == [
        {"conversation": "room", "message_id": "m1", "thread": "root"},
        {"conversation": "room", "message_id": "m2", "thread": "root"},
    ]


async def test_post_result_thread_id_overrides_the_requested_reply_id():
    class ResolvedToolkit(Toolkit):
        async def post(self, text: str, reply_to_message_id: str) -> str:
            return json.dumps({
                "ok": True, "chat_id": "room", "message_id": "m1",
                "part_message_ids": ["m1", "m2"], "thread_id": "",
            })

    host = ToolHost([ResolvedToolkit()], posting_tool="post")
    turn = BoundTurn("r1", metadata={"belle_place": {"thread": "origin"}})
    host.bind("a", turn)

    await host.call(_call("a", "post", text="long", reply_to_message_id="child"))

    assert turn.posted == [
        {"conversation": "room", "message_id": "m1", "thread": ""},
        {"conversation": "room", "message_id": "m2", "thread": "m1"},
    ]


async def test_post_result_uses_confirmed_placement_for_each_part():
    class PlacedToolkit(Toolkit):
        async def post(self, text: str) -> str:
            return json.dumps({
                "ok": False, "error": "post_failed", "chat_id": "room", "message_id": "m1", "parts": 2,
                "part_message_ids": ["m1", "m2"],
                "part_placements": [
                    {"chat_id": "room", "message_id": "m1", "thread_id": ""},
                    {"chat_id": "room", "message_id": "m2", "thread_id": ""},
                ],
            })

    host = ToolHost([PlacedToolkit()], posting_tool="post")
    turn = BoundTurn("r1")
    host.bind("a", turn)

    result = json.loads(await host.call(_call("a", "post", text="long")))

    assert result["ok"] is False and turn.delivered
    assert turn.posted == [
        {"conversation": "room", "message_id": "m1", "thread": ""},
        {"conversation": "room", "message_id": "m2", "thread": ""},
    ]


def test_the_first_definition_of_a_name_is_kept():
    first, second = Toolkit(), Toolkit()
    host = ToolHost([first, second], posting_tool="post")

    assert [definition["name"] for definition in host.definitions("")] == ["post", "read", "wait", "explode"]


class AskingToolkit(Toolkit):
    """A toolkit that says, call by call, what a person must agree to first."""

    def __init__(self, question: str = "", *, broken: bool = False) -> None:
        super().__init__()
        self.question = question
        self.broken = broken
        self.asked: list[tuple[str, dict, dict]] = []

    async def widening_question(self, name: str, arguments: dict) -> str | None:
        self.asked.append((name, dict(arguments), request_metadata()))
        if self.broken:
            raise RuntimeError("the audience could not be read")
        return self.question if arguments.get("text") == "elsewhere" else None


async def test_a_call_its_toolkit_puts_a_question_about_does_not_run_unasked():
    toolkit = AskingToolkit("This would be seen by two more people. Post it?")
    host = ToolHost([toolkit], posting_tool="post")
    turn = BoundTurn("r1", metadata={"where": "a"})
    host.bind("a", turn)

    verdict = await host.authorize(_call("a", "post", text="elsewhere"))
    refused = json.loads(await host.call(_call("a", "post", text="elsewhere")))

    assert (verdict.action, verdict.question.text, verdict.question.tool) == (VERDICT_ASK, toolkit.question, "post")
    assert (refused["error"], refused["detail"]) == ("approval_required", toolkit.question)
    assert toolkit.seen == [] and not turn.delivered
    # The toolkit read the turn's own metadata to decide, and none remains bound after.
    assert toolkit.asked[0] == ("post", {"text": "elsewhere"}, {"where": "a"})
    assert request_metadata() == {}

    # Control: a call the toolkit has no question about runs.
    assert json.loads(await host.call(_call("a", "post", text="here")))["ok"] is True
    assert [text for text, _ in toolkit.seen] == ["here"]


async def test_a_call_with_a_question_answers_at_once_that_it_waits_and_runs_when_confirmed():
    toolkit = AskingToolkit("This would be seen by two more people. Post it?")
    held: list = []

    async def confirm(call, turn, question) -> bool:
        held.append((call, turn, question))
        return True

    host = ToolHost([toolkit], posting_tool="post", confirm=confirm)
    turn = BoundTurn("r1", metadata={"where": "a"})
    host.bind("a", turn)

    waiting = json.loads(await host.call(_call("a", "post", text="elsewhere")))

    assert waiting["error"] == "awaiting_confirmation" and waiting["detail"].startswith(toolkit.question)
    # Nothing ran, and the turn has not spoken: what it owes its conversation is still owed.
    assert toolkit.seen == [] and not turn.delivered
    (call, bound, question) = held[0]
    assert bound is turn and (question.text, question.tool) == (toolkit.question, "post")

    # The turn ends. The confirmed call still runs, once, with the metadata of the turn that made it.
    host.release("a", "r1")
    assert json.loads(await host.run_confirmed(call, bound))["ok"] is True
    assert toolkit.seen == [("elsewhere", {"where": "a"})]

    # A scope's own `ask` is not a toolkit's question: it is refused here, where no backend held it.
    host.bind("a", BoundTurn("r2", tools={"read": LEVEL_ASK}))
    assert json.loads(await host.call(_call("a", "read")))["error"] == "approval_required"
    assert len(held) == 1


async def test_scope_ask_prepares_toolkit_without_removing_scope_approval():
    toolkit = AskingToolkit("Approve this destination?")
    questions = []

    async def confirm(call, turn, question):
        questions.append(question)
        return True

    host = ToolHost([toolkit], confirm=confirm)
    turn = BoundTurn("r1", tools={"post": LEVEL_ASK}, metadata={"where": "a"})
    host.bind("a", turn)
    local = _call("a", "post", text="here")
    assert json.loads(await host.call(local))["error"] == "approval_required"
    assert toolkit.asked == [("post", {"text": "here"}, {"where": "a"})]
    assert not questions and not toolkit.seen
    assert json.loads(await host.run_confirmed(local, turn))["ok"]

    elsewhere = _call("a", "post", text="elsewhere")
    assert json.loads(await host.call(elsewhere))["error"] == "awaiting_confirmation"
    assert len(questions) == 1 and questions[0].text == toolkit.question
    assert json.loads(await host.run_confirmed(elsewhere, turn))["ok"]


async def test_a_question_nobody_could_be_asked_is_a_refusal():
    toolkit = AskingToolkit("Post it?")

    async def nobody(call, turn, question) -> bool:
        return False

    host = ToolHost([toolkit], posting_tool="post", confirm=nobody)
    host.bind("a", BoundTurn("r1"))

    refused = json.loads(await host.call(_call("a", "post", text="elsewhere")))

    assert (refused["error"], refused["detail"]) == ("approval_required", "Post it?") and toolkit.seen == []


async def test_a_question_that_cannot_be_worked_out_leaves_the_refusal_to_the_tool():
    toolkit = AskingToolkit(broken=True)
    host = ToolHost([toolkit], posting_tool="post")
    host.bind("a", BoundTurn("r1"))

    # The tool makes the same check when it runs, and reports what failed in its own words.
    assert (await host.authorize(_call("a", "post", text="elsewhere"))).action == VERDICT_ALLOW



class Named:
    """A toolkit of one platform: ``post`` and one tool only it has, each saying which platform ran it."""

    def __init__(self, platform: str) -> None:
        self.platform = platform

    def get_tool_definitions(self):
        return [
            {"name": name, "description": f"{name} on {self.platform}", "input_params": {"type": "object"}}
            for name in ("post", f"{self.platform}_only")
        ]

    async def post(self) -> str:
        return json.dumps({"ok": True, "platform": self.platform, "chat_id": "room", "message_id": "m1"})

    def __getattr__(self, name: str):
        if name == f"{self.platform}_only":
            return self.post
        raise AttributeError(name)


def _two_platforms() -> ToolHost:
    host = ToolHost([Toolkit()], posting_tool="")
    host.serve("north", [Named("north")], posting_tool="post")
    host.serve("south", [Named("south")], posting_tool="post")
    return host


async def test_a_turn_lists_and_runs_the_tools_of_its_own_platform_under_one_name():
    host = _two_platforms()
    host.bind("a", BoundTurn("r1", platform="north"))
    host.bind("b", BoundTurn("r2", platform="south"))

    names = {conversation: [d["name"] for d in host.definitions(conversation)] for conversation in ("a", "b")}
    ran = {conversation: json.loads(await host.call(_call(conversation, "post"))) for conversation in ("a", "b")}

    assert names == {"a": ["post", "north_only", "read", "wait", "explode"], "b": ["post", "south_only", "read", "wait", "explode"]}
    assert [definition["name"] for definition in host.definitions("")] == ["post", "north_only", "south_only", "read", "wait", "explode"]
    assert ran == {
        "a": {"ok": True, "platform": "north", "chat_id": "room", "message_id": "m1"},
        "b": {"ok": True, "platform": "south", "chat_id": "room", "message_id": "m1"},
    }
    assert await host.call(_call("a", "read")) == "plain text result"


def test_a_tool_is_foreign_to_a_platform_only_where_other_platforms_alone_serve_it():
    host = _two_platforms()
    single = ToolHost([Toolkit()])
    single.serve("north", [Named("north")], posting_tool="post")

    assert host.foreign("north") == {"south_only"} and host.foreign("south") == {"north_only"}
    assert single.foreign("north") == frozenset()


async def test_a_call_to_a_tool_of_another_platform_is_refused_with_its_own_code():
    host = _two_platforms()
    turn = BoundTurn("r1", platform="north")
    host.bind("a", turn)

    verdict = await host.authorize(_call("a", "south_only"))
    result = json.loads(await host.call(_call("a", "south_only")))

    assert verdict.action == VERDICT_DENY and "south_only is a tool of south" in verdict.message
    assert result["error"] == "tool_of_another_platform" and "runs on north" in result["detail"]
    assert json.loads(await host.call(_call("a", "nowhere")))["error"] == "unknown_tool"
    assert not turn.delivered


async def test_the_posting_tool_of_the_turns_platform_marks_it_delivered():
    host = _two_platforms()
    turn = BoundTurn("r1", platform="south")
    host.bind("a", turn)

    await host.call(_call("a", "south_only"))
    assert not turn.delivered
    await host.call(_call("a", "post"))
    assert turn.delivered


async def test_multiple_posting_tools_count_receipts_for_their_platform_only():
    class Posting(Toolkit):
        def get_tool_definitions(self):
            return [*super().get_tool_definitions(), {"name": "upload", "description": "upload", "input_params": {}}]

        async def upload(self, text: str, fail: bool = False) -> str:
            return await self.post(text, fail)

    host = ToolHost([])
    host.serve("north", [Posting()], posting_tool="post", posting_tools=("upload",))
    host.serve("south", [Posting()], posting_tool="post")
    first, second = BoundTurn("n", platform="north"), BoundTurn("s", platform="south")
    host.bind("a", first)
    host.bind("b", second)
    await host.call(_call("a", "upload", text="failed", fail=True))
    assert not first.delivered and not first.posted
    await host.call(_call("a", "upload", text="file"))
    assert first.delivered and first.posted == [{"conversation": "room", "message_id": "m1", "thread": ""}]
    await host.call(_call("a", "post", text="message"))
    assert len(first.posted) == 2
    await host.call(_call("b", "upload", text="file"))
    assert not second.delivered and not second.posted


@pytest.mark.parametrize("change", ["release", "replace", "stop"])
@pytest.mark.parametrize("operation", ["call", "authorize"])
async def test_turn_change_during_toolkit_check_refuses_the_old_call(change, operation):
    checking, proceed = asyncio.Event(), asyncio.Event()

    class WaitingToolkit(Toolkit):
        async def widening_question(self, name, arguments):
            checking.set()
            await proceed.wait()

    toolkit = WaitingToolkit()
    host = ToolHost([toolkit])
    turn = BoundTurn("r1", metadata={"sender": "original"})
    host.bind("a", turn)
    pending = asyncio.create_task(getattr(host, operation)(_call("a", "post", text="old call")))
    await checking.wait()
    if change == "release":
        host.release("a", "r1")
    elif change == "replace":
        host.bind("a", BoundTurn("r2", metadata={"sender": "replacement"}, tools={"post": "deny"}))
    else:
        turn.stopped = True
    proceed.set()

    result = await pending
    if operation == "call":
        assert json.loads(result)["error"] == "tool_denied"
    else:
        assert result.action == VERDICT_DENY
    assert toolkit.seen == []
