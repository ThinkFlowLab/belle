"""Host behaviour proven on the scripted backend only.

These need a backend to fail, stall or ask on cue. Each joins the conformance
suite when a real backend can run their operation.
"""

import json
import logging
from dataclasses import replace

import pytest
from conformance_kit import ModelTurn, RecordedBackend, World, as_file, worlds
from scripted_backend import ScriptedBackend, ScriptedTurn

from belle_agent import host
from belle_agent.backend import (
    APPROVAL,
    EVENT_ACCEPTED,
    EVENT_STEP,
    EVENT_SUBAGENT_UPDATE,
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    OP_CANCEL,
    OP_REFUSE_QUESTION,
    OP_STEER,
    SCOPE_FIELDS,
    SUBAGENT,
    Answer,
    BackendCapabilities,
    Question,
    TurnEvent,
)
from belle_agent.config import load_config
from belle_agent.host import (
    MAX_QUEUED_PER_KEY,
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    OUTCOME_POSTED_FINAL,
    QUEUE_FULL_NOTICE,
    host_from_config,
)
from belle_agent.platforms.base import (
    MARK_DONE,
    MARK_LATE,
    MARK_QUEUED,
    MARK_REFUSED,
    MARK_STARTED,
    MARK_STEERED,
    InboundAnswer,
    InboundEvent,
    QuestionRefused,
)
from belle_agent.progress import RUN_DONE, RUN_FAILED, TurnProgress, note_tool_event
from belle_agent.scheduling.address import Address
from belle_agent.scheduling.host_turn import SessionRunTurn
from belle_agent.skills import packaged_skill_text
from belle_agent.tools.host import AWAITING_CONFIRMATION, refusal

ANSWER_EVERYTHING = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
"""


@pytest.fixture
def kit() -> ScriptedBackend:
    return ScriptedBackend()


async def test_a_failed_turn_tells_a_reader_who_is_owed_an_answer_and_frees_the_conversation(world):
    world.kit.play(ScriptedTurn(error="The provider refused the request."), ModelTurn(final="fine now"))

    await world.send(world.direct("first"))
    await world.send(world.direct("second"))

    assert [post.text for post in world.posts()] == ["The provider refused the request.", "fine now"]
    assert world.outcomes() == [OUTCOME_FAILED, OUTCOME_POSTED_FINAL]


async def test_a_turn_that_fails_after_it_wrote_text_posts_that_text_with_the_failure(build_world):
    world = await build_world()
    failure = "The review is on Thursday. Bring the Q3 fig\n\nThe model's response stopped: The connection closed."
    world.kit.play(ScriptedTurn(error=failure))

    await world.send(world.direct("when is the review"))

    assert [post.text for post in world.posts()] == [failure]
    assert world.outcomes() == [OUTCOME_FAILED]


class Continuing(ScriptedBackend):
    """A backend whose turn writes its answer in two steps, the second of which continues the first."""

    async def run_turn(self, request):
        yield TurnEvent(EVENT_ACCEPTED)
        yield TurnEvent(EVENT_STEP)
        yield TurnEvent(EVENT_TEXT_DELTA, text="The quick br")
        yield TurnEvent(EVENT_STEP, data={"continues": True})
        yield TurnEvent(EVENT_TEXT_DELTA, text="own fox.")
        yield TurnEvent(EVENT_SUCCEEDED, text="The quick brown fox.")


@pytest.mark.parametrize(("bound", "posted"), [(1, host.TURN_STEPS_NOTICE), (2, "The quick brown fox.")])
async def test_a_step_that_continues_the_text_counts_toward_the_step_bound(tmp_path, monkeypatch, bound, posted):
    monkeypatch.setattr(host, "MAX_TURN_STEPS", bound)
    async with worlds(Continuing(), tmp_path) as build:
        world = await build()
        await world.send(world.direct("go"))

        assert [post.text for post in world.posts()] == [posted]


async def test_a_failed_turn_stays_silent_where_no_answer_is_owed(build_world):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all], reply: optional}
"""
    )
    world.kit.play(ScriptedTurn(error="The provider refused the request."))

    await world.send(world.message("just talking"))

    assert world.platform.log == []
    assert world.outcomes() == [OUTCOME_FAILED]


LATE_ANSWERS_OPTIONAL_IN_C2 = """
scopes:
  - match: {channel: loopback, chat: C2}
    delivery: {mode: [all], reply: optional}
"""


async def test_a_late_message_owed_an_answer_is_marked_late_and_one_whose_reply_is_optional_is_not(tmp_path):
    # Only a platform sets ``late``, as it receives a message: the loopback platform hands it on as given.
    async with worlds(ScriptedBackend(), tmp_path) as build:
        world = await build(LATE_ANSWERS_OPTIONAL_IN_C2)
        owed = world.direct("sent before the outage", late=True)
        optional = world.message("just talking", conversation="C2", late=True)

        assert await world.send(owed) == "refused:reached-belle-late"
        assert await world.send(optional) == "ignored:reached-belle-late"
        assert world.requests == [] and world.platform.log == []
        assert world.platform.marks == [(owed.message_id, MARK_LATE)]

        # Control: the same message on time runs.
        assert (await world.send(world.direct("sent now"))).startswith("dispatched:")


TOO_MANY = ("Pick one", tuple(f"option {number}" for number in range(26)))


def _cannot_show(world: World) -> None:
    async def ask(*_: object, **__: object) -> str:
        raise QuestionRefused("a question here offers at most 25 options; this one has 26")

    world.platform.ask = ask


async def test_a_question_the_platform_cannot_show_is_refused_to_the_turn_with_its_reason(tmp_path):
    scripted = ScriptedBackend()
    capabilities = scripted.capabilities()
    backend = ScriptedBackend(replace(capabilities, operations=capabilities.operations | {OP_REFUSE_QUESTION}))
    async with worlds(backend, tmp_path) as build:
        world = await build(ANSWER_EVERYTHING)
        _cannot_show(world)
        world.kit.play(ModelTurn(final="Which of the first five?", ask=TOO_MANY))

        await world.send(world.direct("which one?"))

        ((_, question_id, reason),) = world.kit.refused
        assert question_id.endswith(":ask")
        assert reason == (
            "The question was not shown, so nobody was asked: a question here offers at most 25 options; this one has 26."
        )
        assert [post.text for post in world.posts()] == ["Which of the first five?"]
        assert world.outcomes() == [OUTCOME_POSTED_FINAL]


async def test_a_question_the_platform_cannot_show_ends_the_turn_with_its_reason_where_the_backend_cannot_be_told(
    tmp_path,
):
    async with worlds(ScriptedBackend(), tmp_path) as build:
        world = await build(ANSWER_EVERYTHING)
        _cannot_show(world)
        world.kit.play(ModelTurn(final="never written", ask=TOO_MANY))

        await world.send(world.direct("which one?"))

        assert [post.text for post in world.posts()] == [
            "The turn asked a question that cannot be shown here: a question here offers at most 25 options; this one has 26."
        ]
        assert world.outcomes() == [OUTCOME_FAILED]
        assert world.kit.refused == []


async def test_scheduled_final_starts_new_thread_and_records_its_post(world):
    inbound = world.message("scheduled", thread="new")
    turn = world.host._turn(inbound, "schedule_job", {}, reply_optional=False)
    turn.session_run = SessionRunTurn("ses-1234567890", "run-1234567890", Address("loopback", "W1", "C1", "new"), "refuse", {}, "post_final")

    assert await world.host._deliver(turn, TurnEvent(EVENT_SUCCEEDED, text="done")) == OUTCOME_POSTED_FINAL
    posted = world.posts("C1")
    assert len(posted) == 1
    assert posted[0].thread == ""
    assert turn.bound.posted == [{"conversation": "C1", "thread": posted[0].message_id, "message_id": posted[0].message_id}]


async def test_scheduled_sentinel_is_silent_even_when_reply_is_normally_required(world):
    inbound = world.message("scheduled")
    turn = world.host._turn(inbound, "schedule_job", {}, reply_optional=False)
    turn.session_run = SessionRunTurn("ses-1234567890", "run-1234567890", Address("loopback", "W1", "C1", ""), "refuse", {}, "post_final")

    assert await world.host._deliver(turn, TurnEvent(EVENT_SUCCEEDED, text="NO_REPLY")) == "withheld"
    assert world.posts() == []


async def test_a_backend_that_raises_ends_one_turn_and_not_the_host(world):
    world.kit.play(ScriptedTurn(crash=True), ModelTurn(final="still here"))

    await world.send(world.direct("first"))
    await world.send(world.direct("second"))

    assert world.outcomes() == [OUTCOME_FAILED, OUTCOME_POSTED_FINAL]
    assert world.posts()[-1].text == "still here"


async def test_a_full_queue_refuses_the_message_and_tells_its_sender(world):
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.direct("running"))
    for number in range(MAX_QUEUED_PER_KEY):
        assert (await world.platform.inject(world.direct(f"waiting {number}"))).startswith("queued:")

    outcome = await world.platform.inject(world.direct("one too many"))

    assert outcome.startswith("refused:queue-full")
    # A platform with no text for one person shows the refusal by its mark alone.
    told = world.host.platforms[world.platform_kit.name].ephemeral_posts
    assert [(action.kind, action.to, action.text) for action in world.platform.log] == (
        [("ephemeral", "U1", QUEUE_FULL_NOTICE)] if told else []
    )
    world.kit.release()
    await world.host.idle()
    # Every message accepted before the refusal ran, in order, and the refused one never did.
    assert world.texts() == [
        "running",
        *(f"waiting {number}" for number in range(MAX_QUEUED_PER_KEY)),
    ]


async def test_a_full_queue_whose_notice_is_refused_still_refuses_the_message(monkeypatch, world, caplog):
    platform = world.host.platforms[world.platform_kit.name]
    post = platform.post

    async def refuse_a_private_post(conversation, thread, text, *, ephemeral_to=None, workspace=""):
        if ephemeral_to:
            raise RuntimeError("a post for one person needs a permission the bot lacks")
        return await post(conversation, thread, text, workspace=workspace)

    monkeypatch.setattr(platform, "post", refuse_a_private_post)
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.direct("running"))
    for number in range(MAX_QUEUED_PER_KEY):
        await world.platform.inject(world.direct(f"waiting {number}"))

    with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
        outcome = await world.platform.inject(world.direct("one too many"))

    assert outcome.startswith("refused:queue-full")
    # One warning names the refusal, with no traceback. A platform with no text for one person is asked for none.
    warnings = [record for record in caplog.records if "needs a permission the bot lacks" in record.getMessage()]
    assert [record.exc_info for record in warnings] == ([None] if platform.ephemeral_posts else [])
    world.kit.release()
    await world.host.idle()
    assert "one too many" not in world.texts()


async def test_mid_turn_cancel_stops_the_running_turn_then_runs_the_new_message(build_world):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all], mid_turn: cancel}
"""
    )
    world.kit.play(ModelTurn(final="never read", hold=True), ModelTurn(final="the newer answer"))
    thread = {"thread": "root"}

    first = world.message("first", **thread)
    await world.platform.inject(first)
    await world.handed(1)
    await world.send(world.message("second", **thread))

    assert world.outcomes() == [OUTCOME_INTERRUPTED, OUTCOME_POSTED_FINAL]
    assert [post.text for post in world.posts("C1")] == ["the newer answer"]
    # The session keeps the stopped message. The newer one says it was stopped.
    stopped = host.STOPPED_NOTE.format(message=first.message_id)
    assert [stopped in request.text for request in world.requests] == [False, True]


async def test_one_session_for_a_conversation_when_its_scope_says_so(build_world):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all], session: channel}
  - match: {channel: loopback, chat: C2}
    delivery: {mode: [all]}
"""
    )

    for conversation in ("C1", "C2"):
        await world.send(world.message("one", conversation=conversation, thread="root-a"))
        await world.send(world.message("two", conversation=conversation, thread="root-b"))

    shared_a, shared_b, thread_a, thread_b = (request.conversation for request in world.requests)
    assert shared_a == shared_b
    assert thread_a != thread_b


APPROVE_RM = Question(id="q-1", text="Allow rm to run?", options=("approve", "reject"), kind=APPROVAL, tool="rm")


def _answer(world: World, sender: str, *, conversation: str = "C1", question_id: str = "q-1") -> InboundAnswer:
    return InboundAnswer(
        platform="loopback",
        workspace="W1",
        conversation=conversation,
        question_id=question_id,
        sender=sender,
        answer=Answer(values=("approve",), approved=True),
    )


async def test_a_question_is_shown_once_where_the_turn_runs_and_its_answer_resumes_the_turn(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    world.kit.play(ScriptedTurn(final="removed", question=APPROVE_RM))

    await world.platform.inject(world.message("clean up", thread="root"))
    await world.handed(1)
    for _ in range(3):
        await world.handed(1)
    prompts = [action for action in world.platform.log if action.kind == "approval"]
    assert [(prompt.conversation, prompt.thread, prompt.question) for prompt in prompts] == [
        ("C1", "root", APPROVE_RM)
    ]
    assert world.posts() == []

    # An answer from another conversation, or to a question nobody asked, releases nothing.
    assert await world.platform.inject(_answer(world, "U1", conversation="C2")) == "ignored:question-not-waiting"
    assert await world.platform.inject(_answer(world, "U1", question_id="q-2")) == "ignored:question-not-waiting"
    assert world.kit.answers == []

    assert (await world.send(_answer(world, "U1"))).startswith("answered")
    assert [(question_id, answer.approved) for _, question_id, answer in world.kit.answers] == [("q-1", True)]
    assert [post.text for post in world.posts("C1")] == ["removed"]
    # The question was claimed, so a second answer is too late.
    assert await world.platform.inject(_answer(world, "U1")) == "ignored:question-not-waiting"


async def test_a_clicks_rule_decides_who_may_answer(build_world):
    world = await build_world(
        ANSWER_EVERYTHING
        + """
  - match: {channel: loopback, chat: C1}
    clicks: {approve: {user: [U-LEAD]}}
"""
    )
    world.kit.play(ScriptedTurn(final="removed", question=APPROVE_RM))
    await world.platform.inject(world.message("clean up"))
    while not world.platform.log:
        await world.handed(1)

    refused = await world.platform.inject(_answer(world, "U1"))

    assert refused.startswith("refused:clicks.approve")
    assert world.kit.answers == []
    assert world.posts() == []

    # Control: the person the rule names releases the same question.
    assert (await world.send(_answer(world, "U-LEAD"))).startswith("answered")
    assert [post.text for post in world.posts("C1")] == ["removed"]


async def test_a_turn_being_stopped_gets_no_tool_call_out(world):
    late = (("post_message", {"text": "said after the stop"}),)
    world.kit.play(ScriptedTurn(final="never read", hold=True, ignores_stop=True, calls=late))
    message = world.direct("go")
    await world.platform.inject(message)
    await world.kit.holding()

    assert (await world.platform.inject(world.stop(message))).startswith("stopped")
    await world.host.idle()

    # The stopped work made its call all the same. The tool host refused it.
    assert [json.loads(result)["error"] for result in world.kit.results] == ["tool_denied"]
    assert world.platform.log == []
    assert world.outcomes() == [OUTCOME_INTERRUPTED]

    # Control: the same call from a turn nobody stopped posts.
    world.kit.play(ScriptedTurn(final="done", calls=late))
    await world.send(world.direct("go again"))
    assert [post.text for post in world.posts()] == ["said after the stop"]


async def test_the_next_turn_of_a_conversation_is_told_once_which_message_a_person_stopped(world):
    world.kit.play(
        ScriptedTurn(final="never read", hold=True), ScriptedTurn(final="later"), ScriptedTurn(final="last")
    )
    message = world.direct("summarise the channel")
    await world.platform.inject(message)
    await world.kit.holding()
    assert (await world.platform.inject(world.stop(message))).startswith("stopped")
    await world.host.idle()

    await world.send(world.direct("something else"))
    await world.send(world.direct("and more"))

    # The note goes into the message the session keeps, and only the next turn's.
    stopped = host.STOPPED_NOTE.format(message=message.message_id)
    assert [stopped in request.text for request in world.requests] == [False, True, False]
    assert [post.text for post in world.posts()] == ["later", "last"]


async def test_a_restart_keeps_what_the_next_turn_is_told_and_it_is_told_once(build_world):
    world = await build_world()
    world.kit.play(ScriptedTurn(final="never read", hold=True), ScriptedTurn(final="later"), ScriptedTurn(final="last"))
    message = world.direct("summarise the channel")
    await world.platform.inject(message)
    await world.kit.holding()
    assert (await world.platform.inject(world.stop(message))).startswith("stopped")
    await world.host.idle()
    await world.host.stop()

    restarted = await build_world()
    # The handled message ids outlive the restart too, so the new messages take new ones.
    restarted._ids = world._ids
    await restarted.send(restarted.direct("something else"))
    await restarted.send(restarted.direct("and more"))

    stopped = host.STOPPED_NOTE.format(message=message.message_id)
    assert [stopped in request.text for request in restarted.requests] == [True, False]


async def test_a_turn_left_at_its_time_bound_gets_no_tool_call_out(world, monkeypatch):
    monkeypatch.setattr(host, "TURN_TIMEOUT_SECONDS", 0.2)
    late = (("post_message", {"text": "said after the bound"}),)
    world.kit.play(ScriptedTurn(final="never read", hold=True, ignores_stop=True, calls=late))

    await world.send(world.direct("go"))

    assert [json.loads(result)["error"] for result in world.kit.results] == ["tool_denied"]
    assert [post.text for post in world.posts()] == [host.TURN_TIMEOUT_NOTICE]


async def test_a_turn_that_costs_more_than_one_may_is_stopped(world, monkeypatch):
    monkeypatch.setattr(host, "MAX_TURN_COST", 1.0)
    world.kit.play(
        ScriptedTurn(final="affordable", cost=1.0),
        ScriptedTurn(final="never read", cost=1.5, calls=(("post_message", {"text": "paid for"}),)),
    )

    await world.send(world.direct("cheap"))
    await world.send(world.direct("dear"))

    assert [post.text for post in world.posts()] == ["affordable", host.TURN_COST_NOTICE]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL, OUTCOME_FAILED]


async def test_a_stop_for_a_turn_that_is_not_running_stops_nothing(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    world.kit.play(ModelTurn(final="first", hold=True), ModelTurn(final="second"))
    running, waiting = world.message("one", thread="root"), world.message("two", thread="root")
    await world.platform.inject(running)
    await world.platform.inject(waiting)
    await world.kit.holding()

    # A queued message has no turn yet, and neither has a message of another conversation.
    assert await world.platform.inject(world.stop(waiting)) == "ignored:no-turn-running"
    assert await world.platform.inject(replace(world.stop(running), conversation="C2")) == "ignored:no-turn-running"
    world.kit.release()
    await world.host.idle()

    assert [post.text for post in world.posts()] == ["first", "second"]
    assert not any("stopped" in request.text for request in world.requests)


async def test_a_start_stops_the_turns_left_running_by_an_earlier_process(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("platforms: {loopback: {enabled: true}}\n", encoding="utf-8")
    backend = ScriptedBackend()
    # What a backend that outlives the host still runs when a new host starts.
    backend._running.add("loopback_W1_D-U1_U1")
    world_host = host_from_config(load_config(path), RecordedBackend(backend), state_dir=tmp_path)

    with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
        await world_host.start()
    await world_host.stop()

    assert backend.stopped == ["loopback_W1_D-U1_U1"]
    assert "running from before this start and stopped it" in caplog.text


async def test_stopping_drops_what_waits_and_closes_the_backend(world, caplog):
    world.kit.play(ModelTurn(final="never read", hold=True))
    await world.platform.inject(world.direct("running"))
    await world.platform.inject(world.direct("waiting"))
    await world.handed(1)

    with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
        await world.host.stop()

    assert "dropped 1 queued message(s) across 1 attached session(s)" in caplog.text
    assert world.kit.closed
    assert world.outcomes() == [OUTCOME_INTERRUPTED]
    assert world.platform.log == []
    assert await world.platform.inject(world.direct("too late")) == "dropped:platform-not-started"


async def test_startup_names_each_scoped_setting_the_backend_does_not_apply(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
platforms: {loopback: {enabled: true}}
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all], reply: optional, mid_turn: cancel}
    agent: {model_name: small-model, skills: {required: [glossary], unavailable: [style]}}
    permissions: {tools: {bash: deny}}
""",
        encoding="utf-8",
    )
    bare = ScriptedBackend(BackendCapabilities(backend="bare"))
    host = host_from_config(load_config(path), RecordedBackend(bare), state_dir=tmp_path)

    with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
        await host.start()
    await host.stop()

    warned = [record.getMessage() for record in caplog.records if "enforces nothing" in record.getMessage()]
    for setting, missing in (
        ("agent.model_name", "model"),
        ("agent.skills.required", "skills_required"),
        ("agent.skills.unavailable", "skills_unavailable"),
        ("delivery.reply", "system_additions"),
        ("delivery.mid_turn=cancel", "the cancel operation"),
        ("permissions.tools", "tools"),
    ):
        assert any(f"{setting} is set" in line and line.count(missing) for line in warned), (setting, warned)


async def test_startup_is_quiet_when_the_backend_applies_every_setting(build_world, caplog):
    with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
        await build_world(
            """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all], reply: optional, mid_turn: cancel}
    agent: {model_name: small-model}
"""
        )

    assert "enforces nothing" not in caplog.text


async def test_where_a_backend_cannot_attach_a_skill_the_host_puts_its_text_in_the_turn(tmp_path, caplog):
    bare = ScriptedBackend()
    kit = ScriptedBackend(replace(bare.capabilities(), scope_fields=SCOPE_FIELDS - {"skills_required"}))
    with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
        async with worlds(kit, tmp_path) as build:
            world = await build(
                """
scopes:
  - match: {channel: loopback}
    agent: {skills: {required: [messaging-context, not-packaged]}}
"""
            )
            await world.send(world.direct("plain"))
            asked = await world.host.receive(replace(world.direct("use it"), skills_required=("chat-digest",)))
            await world.host.idle()
            unknown = await world.host.receive(replace(world.direct("use that"), skills_required=("not-packaged",)))

    assert world.host.backend_capabilities.scope_fields == SCOPE_FIELDS
    assert asked.startswith("dispatched") and unknown == "refused:backend-does-not-apply-skills_required"
    plain, used = (request.scope.system_additions[-1] for request in world.requests)
    assert packaged_skill_text("messaging-context") in plain and packaged_skill_text("chat-digest") not in plain
    assert packaged_skill_text("chat-digest") in used and "not-packaged" not in plain + used
    # A required skill the host has no text for is said at start. Nothing else is.
    assert [line for line in caplog.messages if "enforces nothing" in line] == [
        (
            "scopes: agent.skills.required is set for loopback but backend scripted does not apply the skill"
            " not-packaged, which Belle does not package. That setting enforces nothing"
        )
    ]

    # Control: a backend that attaches skills itself gets no text from the host.
    (tmp_path / "attaching").mkdir()
    async with worlds(ScriptedBackend(), tmp_path / "attaching") as build:
        world = await build("scopes:\n  - match: {channel: loopback}\n    agent: {skills: {required: [messaging-context]}}\n")
        await world.send(world.direct("plain"))
    assert world.requests[0].scope.system_additions == ()

    # A scope that requires only skills Belle does not package puts no text in the turn.
    (tmp_path / "unpackaged").mkdir()
    lacking = ScriptedBackend(replace(bare.capabilities(), scope_fields=SCOPE_FIELDS - {"skills_required"}))
    async with worlds(lacking, tmp_path / "unpackaged") as build:
        world = await build("scopes:\n  - match: {channel: loopback}\n    agent: {skills: {required: [not-packaged]}}\n")
        await world.send(world.direct("plain"))
    assert world.requests[0].scope.system_additions == ()


async def test_command_help_uses_the_platforms_configured_prefix(build_world):
    world = await build_world("scopes:\n  - match: {channel: loopback}\n    agent: {skills: {required: [belle-command-help]}}\n")
    world.host._hosted[world.platform_kit.name].platform.command_prefix = lambda _workspace: "!custom"

    await world.send(world.direct("help"))

    assert "The Belle help command in this conversation is `!custom help`." in world.requests[0].scope.system_additions


async def test_a_turn_that_requires_an_unavailable_skill_is_refused(build_world, caplog):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
    agent: {skills: {unavailable: [chat-digest]}}
  - match: {channel: loopback, chat: C1}
    agent: {skills: {required: [chat-digest]}}
"""
    )

    with caplog.at_level(logging.WARNING, logger="belle_agent.host.commands"):
        composed = await world.host.receive(world.message("hello", conversation="C1"))
    asked = await world.host.receive(replace(world.direct("use it"), skills_required=("chat-digest",)))
    await world.send(world.direct("plain"))

    assert composed == "refused:skill-required-and-unavailable"
    assert asked == "refused:skill-not-available-here"
    (warned,) = [line for line in caplog.messages if "both name" in line]
    assert warned.startswith("scopes: agent.skills.required and agent.skills.unavailable both name chat-digest for")
    assert "conversation C1." in warned
    (plain,) = world.requests
    assert plain.scope.skills_unavailable == frozenset({"chat-digest"}) and plain.scope.skills_required == ()


async def test_an_event_turn_that_requires_an_unavailable_skill_is_refused(build_world):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all], reply: optional, events: {member: turn}}
    agent: {skills: {unavailable: [chat-digest]}}
  - match: {channel: loopback, chat: C1}
    agent: {skills: {required: [chat-digest]}}
"""
    )
    joined = InboundEvent(
        platform=world.platform_kit.name, workspace="W1", conversation="C1", kind="member", actor="U7", event_id="ev-join"
    )

    assert await world.send(joined) == "refused:skill-required-and-unavailable"
    assert world.requests == []


STEER_IN_C1 = """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all], mid_turn: steer}
    clicks: {stop: {user: [U-LEAD]}}
  - match: {channel: loopback, chat: C1, user: [U-SMALL]}
    agent: {model_name: small-model}
"""


def _steers() -> ScriptedBackend:
    return ScriptedBackend(
        BackendCapabilities(backend="steers", operations=frozenset({OP_CANCEL, OP_STEER}), scope_fields=SCOPE_FIELDS)
    )


async def _during_a_turn(world: World, *messages) -> list[str]:
    """Deliver ``messages`` while a turn of thread ``root`` runs, then let it end. Return their outcomes."""
    world.kit.play(ModelTurn(final="answered", hold=True))
    await world.platform.inject(world.message("first", thread="root"))
    await world.kit.holding()
    outcomes = [await world.platform.inject(message) for message in messages]
    world.kit.release()
    await world.host.idle()
    return outcomes


async def test_the_sender_and_whoever_may_stop_the_turn_steer_it_and_their_messages_end_with_it(tmp_path):
    async with worlds(_steers(), tmp_path) as build:
        world = await build(STEER_IN_C1)
        joined = [
            world.message("from its sender", thread="root"),
            world.message("from the lead", sender="U-LEAD", thread="root"),
        ]
        outcomes = await _during_a_turn(world, *joined)

    assert [outcome.split(":")[0] for outcome in outcomes] == ["steered", "steered"]
    assert [request.text for request in world.kit.steered] == ["from its sender", "from the lead"]
    assert world.texts() == ["first"]
    for message in joined:
        marks = [state for message_id, state in world.platform.marks if message_id == message.message_id]
        assert marks == [MARK_STEERED, MARK_DONE]


async def test_steered_messages_the_turn_did_not_read_run_next_as_one_turn_and_end_with_it(tmp_path):
    async with worlds(_steers(), tmp_path) as build:
        world = await build(STEER_IN_C1)
        world.kit.unread_steers = 2
        joined = [world.message(text, thread="root") for text in ("read", "late", "later")]
        await _during_a_turn(world, *joined)

    assert world.texts() == ["first", "late\n\nlater"]
    marks = {m.text: [state for mid, state in world.platform.marks if mid == m.message_id] for m in joined}
    assert marks == {
        "read": [MARK_STEERED, MARK_DONE],
        "late": [MARK_STEERED, MARK_QUEUED, MARK_STARTED, MARK_DONE],
        "later": [MARK_STEERED, MARK_DONE],
    }


@pytest.mark.parametrize(
    ("sender", "fields"),
    [("U2", {}), ("U-SMALL", {}), ("U1", {"files": (as_file("kite-bridle.md"),)})],
    # clicks.stop does not name U2. U-SMALL's own turn would run on another model.
    ids=["may-not-stop", "another-scope", "with-a-file"],
)
async def test_a_message_that_could_not_have_run_in_the_turn_waits_and_so_does_the_next_one(tmp_path, sender, fields):
    async with worlds(_steers(), tmp_path) as build:
        world = await build(STEER_IN_C1)
        waiting = world.message("waits", sender=sender, thread="root", **fields)
        # It would steer, but a message waits before it.
        after = world.message("after it", thread="root")
        outcomes = await _during_a_turn(world, waiting, after)

    assert [outcome.split(":")[0] for outcome in outcomes] == ["queued", "queued"]
    assert world.kit.steered == []
    assert world.texts() == ["first", "waits", "after it"]


async def test_a_full_queue_on_a_platform_with_no_private_text_marks_the_message_and_says_nothing(monkeypatch, world):
    platform = world.host.platforms[world.platform_kit.name]
    monkeypatch.setattr(platform, "ephemeral_posts", False)
    for member in getattr(platform, "members", {}).values():
        monkeypatch.setattr(member, "ephemeral_posts", False)
    marked = []
    mark = platform.mark

    async def record(inbound, state):
        marked.append((inbound.text, state))
        await mark(inbound, state)

    monkeypatch.setattr(platform, "mark", record)
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.direct("running"))
    for number in range(MAX_QUEUED_PER_KEY):
        await world.platform.inject(world.direct(f"waiting {number}"))

    assert (await world.platform.inject(world.direct("one too many"))).startswith("refused:queue-full")
    assert ("one too many", MARK_REFUSED) in marked
    assert not [action for action in world.platform.log if action.kind in ("ephemeral", "post")]
    world.kit.release()
    await world.host.idle()


async def test_status_rows_follow_exact_running_and_waiting_owners(world):
    world.kit.play(ModelTurn(final="done", hold=True), ModelTurn(final="next"))
    running = world.direct("private running text")
    await world.platform.inject(running)
    await world.kit.holding()
    waiting = world.direct("private waiting text")
    await world.platform.inject(waiting)
    # The admission lookup can expire or evict a running entry independently.
    world.host._admission.turn_initiators.clear()
    sample = await world.host.status_work()
    rows = sample['rows']
    assert [row['state'] for row in rows] == ['running', 'queued']
    owned_turns = [
        *world.host._active_work.values(),
        *(turn for queue in world.host._admission.queued_messages.values() for turn in queue),
    ]
    assert [(row['platform'], row['workspace'], row['conversation']) for row in rows] == [
        (turn.inbound.platform, turn.inbound.workspace, turn.inbound.conversation) for turn in owned_turns
    ]
    assert sample['metrics']['active_turns']['value'] == 1
    assert len({row['id'] for row in rows}) == 2
    assert rows[0]['started_at'] is not None
    assert rows[1]['started_at'] is None
    assert all(row['model'] is row['tokens_in'] is row['tokens_out'] is None for row in rows)
    assert 'private' not in json.dumps(sample)
    assert all(row['id'] != request.request_id for row in rows for request in world.requests)
    world.kit.release()
    await world.host.idle()
    assert (await world.host.status_work())['rows'] == []


async def test_subagent_rows_remain_until_parent_turn_ends(world):
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.direct("delegate"))
    await world.kit.holding()
    turn = next(iter(world.host._active_work.values()))
    track = world.host._track_subagent
    track(turn, TurnEvent(EVENT_TOOL_CALLED, data={"call_id": "private-call", SUBAGENT: "research"}))
    rows = (await world.host.status_work())["rows"]
    parent, child = rows
    assert (child["type"], child["agent"], child["state"], child["parent"]) == (
        "subagent", "research", "running", parent["id"])
    assert child["id"] != "private-call" and child["started_at"]
    track(turn, TurnEvent(EVENT_TOOL_RESULT, data={"call_id": "private-call", SUBAGENT: "research"}))
    assert (await world.host.status_work())["rows"][1]["state"] == "completed"
    track(turn, TurnEvent(EVENT_SUBAGENT_UPDATE, data={"id": "another", "name": "code", "state": "failed"}))
    assert [row["state"] for row in (await world.host.status_work())["rows"]] == [
        "running", "completed", "failed"]
    world.kit.release()
    await world.host.idle()
    assert (await world.host.status_work())["rows"] == []


async def test_a_subagent_ends_on_its_own_result_and_not_on_a_child_tool_result(world):
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.direct("delegate"))
    await world.kit.holding()
    turn = next(iter(world.host._active_work.values()))
    track = world.host._track_subagent
    track(turn, TurnEvent(EVENT_TOOL_CALLED, data={"call_id": "s1", SUBAGENT: "research"}))
    track(turn, TurnEvent(EVENT_TOOL_RESULT, data={"call_id": "s1", "failed": True, "child": True}))
    assert (await world.host.status_work())["rows"][1]["state"] == "running"
    track(turn, TurnEvent(EVENT_TOOL_RESULT, data={"call_id": "s1", "failed": True}))
    assert (await world.host.status_work())["rows"][1]["state"] == "failed"
    world.kit.release()
    await world.host.idle()


async def test_a_subagent_whose_result_refuses_fails_in_the_work_list_as_on_the_card(world):
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.direct("delegate"))
    await world.kit.holding()
    turn = next(iter(world.host._active_work.values()))
    card = TurnProgress()
    for call_id, result in (("s1", refusal("tool_denied", "no")), ("s2", refusal(AWAITING_CONFIRMATION, "ask"))):
        for event in (
            TurnEvent(EVENT_TOOL_CALLED, data={"call_id": call_id, SUBAGENT: "research"}),
            TurnEvent(EVENT_TOOL_RESULT, text=result, data={"call_id": call_id}),
        ):
            world.host._track_subagent(turn, event)
            note_tool_event(card, event)
    # A call held for a person's answer has not failed.
    assert [card.subagent_runs[call_id].state for call_id in ("s1", "s2")] == [RUN_FAILED, RUN_DONE]
    assert [row["state"] for row in (await world.host.status_work())["rows"][1:]] == ["failed", "completed"]
    world.kit.release()
    await world.host.idle()


async def test_detached_worker_posts_once_with_original_platform_context(build_world, monkeypatch):
    world = await build_world("scheduling: {engine: belle}")
    # World uses synthetic DM identities; target membership is checked by the scheduling tests.
    async def target(raw, origin, user):
        return origin, world.requests[0].metadata["belle_place"]["chat_type"]

    monkeypatch.setattr(world.host.schedule_toolkit, "_target", target)
    world.kit.play(
        ModelTurn(calls=(("session_start", {"prompt": "finish independently", "notify": False}),),
                  final="work started"),
        ModelTurn(calls=(("post_message", {"text": "detached answer"}),), final="private detached final"),
    )
    await world.send(world.direct("start detached work"))
    await world.handed(2)
    await world.host.idle()
    assert len(world.requests) == 2
    original, detached = world.requests
    assert detached.request_id != original.request_id
    assert detached.metadata["belle_inbound"] == original.metadata["belle_inbound"]
    assert detached.metadata["belle_place"] == original.metadata["belle_place"]
    assert original.metadata["belle_inbound"]["envelope"] in detached.text
    posts = [post.text for post in world.posts()]
    assert posts.count("detached answer") == 1
    assert all("private detached final" not in text for text in posts)
    assert not any(text.startswith("Session `") for text in posts)
