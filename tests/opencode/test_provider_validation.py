"""Provider validation: the shapes and the behaviour a stand-in cannot prove, on the real provider.

Run with OCB_PROXY set. OpenCode reaches the provider through
the recording proxy (`recording_proxy.py`), so each check reads what the model
was sent and what the provider answered. A check records what it observed as
one JSON line in the artefact directory and on standard output. It asserts
only what makes the observation mean something: that the turn it read was the
one it sent, and that no side effect followed a refusal. Whether the provider
accepted a shape, or the model did what it was asked, is the recorded result.

The provider key is in the OpenCode container's environment on these runs, and
OpenCode's shell and file tools can read it. Every scope here denies them, and
the proxy forwards no request that offers one. No prompt asks the model about
its environment.
"""

import asyncio
import contextlib
import json
import os
import sys
import types

import httpx
import pytest
from opencode_kit import ARTEFACTS, PLUGIN, backend

from belle_agent.backend import EVENT_TEXT_DELTA, EVENT_USAGE, TurnRequest, TurnScope
from belle_agent.host import OUTCOME_POSTED_BY_TOOL, OUTCOME_WITHHELD
from belle_agent.integrations.opencode.sessions import session_id
from belle_agent.tools.host import BoundTurn, ToolHost

pytestmark = [pytest.mark.adapter, pytest.mark.provider, pytest.mark.validation]

PROXY = os.environ.get("OCB_PROXY", "")
TRIALS = int(os.environ.get("OCB_TRIALS") or 12)
SECOND_MODEL = os.environ.get("OCB_SECOND_MODEL", "")
MODE = "plugin" if PLUGIN else "plain"
POST = "belle_post_message"

# Built-in tools that can read or send what the OpenCode container holds, and the two that only add to a request.
KEY_REACHING = tuple(filter(None, os.environ.get("OCB_KEY_REACHING_TOOLS", "").split(",")))
DENIED = {name: "deny" for name in (*KEY_REACHING, "skill", "question")}
DENY_YAML = ", ".join(f"{name}: deny" for name in DENIED)

RULE = (
    "You are Belle, an assistant in a group chat. Most messages here are people talking to each other."
    " Reply only when a message addresses Belle by name. Otherwise stay silent: call no tool and end your turn."
)

GROUP = f"""
scopes:
  - match: {{channel: loopback}}
    delivery: {{mode: [all], reply: optional, session: channel, prompt: "{RULE}"}}
    permissions: {{tools: {{{DENY_YAML}}}}}
"""

ADDRESSED = (
    "Belle, what is the capital of France?",
    "Belle, how many days are there in a leap year?",
    "Belle, what is 17 plus 25?",
    "Belle, which planet is closest to the sun?",
    "Belle, what is the chemical symbol for gold?",
    "Belle, how many minutes are there in two hours?",
    "Belle, what colour do you get by mixing blue and yellow?",
    "Belle, what is the plural of 'mouse'?",
    "Belle, which month comes after October?",
    "Belle, what is the square root of 81?",
    "Belle, how many sides does a hexagon have?",
    "Belle, what is the opposite of 'north'?",
)
UNADDRESSED = (
    "Bob, are we still on for lunch at noon?",
    "ok, I pushed the fix, thanks Chen",
    "Dana, can you send me the slides when you have a minute?",
    "haha yes, exactly what I thought",
    "I'll be ten minutes late to standup, sorry",
    "Erin, your parcel is at the front desk",
    "Good morning everyone",
    "Farid and I are taking the 3pm slot for the meeting room",
    "thanks Gita, that worked",
    "Has anyone seen my blue umbrella? I left it near the kitchen",
    "Ivan, I replied to your email",
    "see you all tomorrow",
)


def note(row: str, **observed) -> None:
    """Record one observation of one row."""
    line = json.dumps({"row": row, "mode": MODE, **observed}, ensure_ascii=False)
    with open(os.path.join(ARTEFACTS, "provider-validation.jsonl"), "a") as handle:
        handle.write(line + "\n")
    print("\nOBSERVED " + line)


def exchanges(session: str | None = None) -> list[dict]:
    """What the proxy recorded, without title requests: of one session, or of this run."""
    path = os.path.join(ARTEFACTS, "provider-requests.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        entries = [json.loads(line) for line in handle]
    return [e for e in entries if e.get("answered") != "title" and (session is None or e["session"] == session)]


def script(*answers: dict) -> None:
    """Have the proxy answer the next requests itself: a way to build a history the model would not write on cue."""
    httpx.post(PROXY + "/_script", json={"answers": list(answers)}).raise_for_status()


def roles(exchange: dict) -> list[str]:
    return [message["role"] for message in exchange["body"]["messages"]]


def text_of(message: dict) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else "".join(part.get("text", "") for part in content or ())


def shown(exchange: dict) -> dict:
    """One recorded exchange, reduced to what a row reports."""
    body, response = exchange["body"], exchange["response"]
    summary = response.get("summary") or {}
    return {
        "answered_by": exchange.get("answered", "provider"),
        "status": response["status"],
        "roles": roles(exchange),
        "tools": sorted(tool["function"]["name"] for tool in body.get("tools") or ()),
        "requested_model": body.get("model"),
        "served_model": summary.get("model"),
        "served_by": summary.get("provider"),
        "finish": summary.get("finish_reason"),
        "calls": [call["name"] for call in summary.get("tool_calls") or ()],
        "said": (summary.get("content") or "")[:300],
        "usage": summary.get("usage"),
        "error": None if response["status"] == 200 else summary.get("error") or response.get("error") or (response.get("body") or "")[:300],
    }


def session_of(world, index: int = -1) -> str:
    return session_id(world.kit.backend._namespace, world.requests[index].conversation)


async def direct_turn(opencode, conversation: str, request_id: str, text: str, **scope) -> dict:
    """Run one turn on the backend with no host: what it ended as, its text, its usage events and its deltas."""
    request = TurnRequest(conversation=conversation, request_id=request_id, text=text, scope=TurnScope(tools=DENIED, **scope))
    events = [event async for event in opencode.run_turn(request)]
    session = session_id(opencode._namespace, conversation)
    return {
        "ended": events[-1].kind,
        "text": events[-1].text[:300],
        "deltas": sum(event.kind == EVENT_TEXT_DELTA for event in events),
        "usage_events": [event.data for event in events if event.kind == EVENT_USAGE][-1:],
        "exchanges": [shown(e) for e in exchanges(session)],
    }


# ── request shapes ───────────────────────────────────────────────────────


class SchemaOnly:
    """Toolkits reduced to their definitions. A call is answered, and reaches nothing."""

    def __init__(self, definitions: list[dict]) -> None:
        self._definitions = definitions

    def get_tool_definitions(self) -> list[dict]:
        return self._definitions

    def __getattr__(self, name: str):
        async def refuse(**arguments) -> str:
            return json.dumps({"ok": False, "error": "schema_validation_only", "detail": "this run serves schemas only"})

        return refuse


class _Hollow(types.ModuleType):
    """A module that has every name. It stands for a library the definitions do not use."""

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return type(name, (), {})


def toolkit_definitions() -> dict[str, list[dict]]:
    """Every toolkit's definitions, by toolkit. No toolkit is constructed, so none needs a client or a token."""
    import importlib

    from belle_agent.second_brain.tools import second_brain_tool_definitions

    slack = {
        "bookmarks": "SlackBookmarkToolkit", "canvases": "SlackCanvasToolkit", "directory": "SlackDirectoryToolkit",
        "history": "SlackHistoryToolkit", "home_tab": "SlackHomeTabToolkit", "lists": "SlackListToolkit",
        "pins": "SlackPinToolkit", "post": "SlackPostToolkit", "reactions": "SlackReactionToolkit",
        "search": "SlackSearchToolkit",
    }
    found = {"second_brain": second_brain_tool_definitions()}
    for module, name in slack.items():
        try:
            toolkit = getattr(importlib.import_module(f"belle_agent.platforms.slack.{module}"), name)
        except ModuleNotFoundError as missing:
            # The image holds no aiohttp. The definitions need none of it, so the import gets a hollow module.
            sys.modules.setdefault(str(missing.name), _Hollow(str(missing.name)))
            toolkit = getattr(importlib.import_module(f"belle_agent.platforms.slack.{module}"), name)
        found[f"slack.{module}"] = toolkit.__new__(toolkit).get_tool_definitions()
    return found


async def _schema_turn(definitions: list[dict], conversation: str) -> dict:
    opencode, tools = backend(), ToolHost([SchemaOnly(definitions)])
    await opencode.start(tools)
    tools.bind(conversation, BoundTurn(request_id="r1"))
    try:
        return await direct_turn(opencode, conversation, "r1", "Reply with the single word ready. Call no tool.")
    finally:
        await opencode.close()


async def test_shape_every_toolkit_schema_in_one_request(model):  # `model`: the stand-in, for a rehearsal
    by_toolkit = toolkit_definitions()
    unique = {definition["name"]: definition for definitions in by_toolkit.values() for definition in definitions}

    together = await _schema_turn(list(unique.values()), "schemas-all")

    (exchange,) = together["exchanges"]
    assert len(exchange["tools"]) == len(unique)
    note("schemas", toolkits={name: [d["name"] for d in defs] for name, defs in by_toolkit.items()},
         tools_offered=len(exchange["tools"]), status=exchange["status"], ended=together["ended"],
         said=together["text"], usage=exchange["usage"], error=exchange["error"])
    if exchange["status"] == 200:
        return
    # A rejection names no toolkit. One request per toolkit finds which.
    for name, definitions in by_toolkit.items():
        alone = await _schema_turn(definitions, f"schemas-{name}")
        note("schemas-alone", toolkit=name, status=alone["exchanges"][0]["status"], error=alone["exchanges"][0]["error"])


@pytest.mark.parametrize("answer", [{}, {"text": ""}], ids=["no-content", "empty-content"])
async def test_shape_an_empty_assistant_message_after_a_silent_turn(build_world, answer):
    world = await build_world(GROUP)
    script(answer)
    await world.send(world.message("see you all tomorrow", conversation="C-EMPTY"))
    assert world.outcomes() == [OUTCOME_WITHHELD]

    await world.send(world.message("Belle, what is the capital of France?", conversation="C-EMPTY"))

    silent, *asked = exchanges(session_of(world))
    assert silent["answered"] == "script"
    stored = [m for m in httpx_messages(world) if m["type"] == "assistant"]
    note("empty-assistant", scripted=answer, stored_assistant_content=[m.get("content") for m in stored][:1],
         next_request=shown(asked[0]), assistant_messages_sent=[m for m in asked[0]["body"]["messages"] if m["role"] == "assistant"],
         outcome=world.outcomes()[-1], posts=[post.text for post in world.posts()])


def httpx_messages(world) -> list[dict]:
    """The stored messages of the latest conversation's session, oldest first."""
    from drive import Client

    return Client().messages(session_of(world))


async def test_shape_two_user_messages_in_a_row_after_a_failed_turn(build_world):
    world = await build_world(GROUP)
    script({"status": 400})
    await world.send(world.message("Belle, are you there?", conversation="C-TWICE"))

    await world.send(world.message("Belle, what is the capital of France?", conversation="C-TWICE"))

    failed, *asked = exchanges(session_of(world))
    assert failed["answered"] == "script" and failed["response"]["status"] == 400
    note("consecutive-user", next_request=shown(asked[0]), outcomes=world.outcomes(), posts=[post.text for post in world.posts()])


@pytest.mark.no_plugin
async def test_shape_the_system_update_message_of_a_changed_instruction_entry(world):
    opencode = world.kit.backend
    turns = [
        ("Reply with the single word one.", ()),
        ("What is the codeword? Reply with the codeword only.", ("When asked for the codeword, the codeword is HERON.",)),
        ("What is the codeword now? Reply with the codeword only.", ("When asked for the codeword, the codeword is OSPREY.",)),
        ("Is any codeword instruction still in force? Reply yes or no only.", ()),
    ]
    for number, (text, additions) in enumerate(turns):
        turn = await direct_turn(opencode, "c-update", f"r{number}", text, system_additions=additions)
        exchange = exchanges(session_id(opencode._namespace, "c-update"))[-1]
        updates = [text_of(m)[:200] for m in exchange["body"]["messages"] if m["role"] == "user" and "<system-update>" in text_of(m)]
        note("system-update", turn=number, additions=additions, ended=turn["ended"], said=turn["text"],
             request=shown(exchange), update_messages=updates,
             last_user_messages=[text_of(m)[:120] for m in exchange["body"]["messages"] if m["role"] == "user"][-3:])


async def test_model_follows_an_addition_that_arrives_after_a_sessions_first_turn(world):
    """With no plugin such an addition is a ``<system-update>`` user message; behind the plugin it is system text."""
    opencode, rows = world.kit.backend, []
    for number in range(min(TRIALS, 6)):
        conversation = f"c-late-{number}"
        await direct_turn(opencode, conversation, "r0", "Reply with the single word one.")
        turn = await direct_turn(opencode, conversation, "r1", "What is the codeword? Reply with the codeword only.",
                                 system_additions=("When asked for the codeword, the codeword is HERON.",))
        last = exchanges(session_id(opencode._namespace, conversation))[-1]
        rows.append({"said": turn["text"], "roles": roles(last), "status": last["response"]["status"],
                     "in_system": "HERON" in _system(last),
                     "as_update_message": any("<system-update>" in text_of(m) for m in last["body"]["messages"] if m["role"] == "user")})
    note("late-addition", trials=len(rows), followed=sum("HERON" in row["said"] for row in rows), rows=rows)


# ── a real model under the constraints ──────────────────────────────────


def _system(exchange: dict) -> str:
    return "\n".join(text_of(m) for m in exchange["body"]["messages"] if m["role"] == "system")


async def _trials(world, texts, prefix: str) -> list[dict]:
    rows = []
    for number, text in enumerate(texts[:TRIALS]):
        before, posted = len(world.outcomes()), len(world.posts())
        await world.send(world.message(text, conversation=f"{prefix}{number}"))
        asked = exchanges(session_of(world))
        system = _system(asked[0])
        rows.append({
            "text": text,
            "outcome": world.outcomes()[before:],
            "posts": [post.text[:200] for post in world.posts()[posted:]],
            "requests": len(asked),
            "statuses": [e["response"]["status"] for e in asked],
            "calls": [call for e in asked for call in shown(e)["calls"]],
            "unposted_final": shown(asked[-1])["said"],
            "roster": shown(asked[0])["tools"],
            "rule_in_system": RULE in system,
            "rule_as_context_entry": '<context key="belle">' in system,
        })
    return rows


async def test_model_posts_when_addressed_and_is_silent_when_not(build_world):
    world = await build_world(GROUP)

    addressed = await _trials(world, ADDRESSED, "C-ASK")
    unaddressed = await _trials(world, UNADDRESSED, "C-CHAT")

    posted = sum(row["outcome"] == [OUTCOME_POSTED_BY_TOOL] for row in addressed)
    silent = sum(row["outcome"] == [OUTCOME_WITHHELD] and not row["posts"] for row in unaddressed)
    note("posting-when-addressed", trials=len(addressed), posted_through_the_tool=posted, rows=addressed)
    note("silence-when-not-addressed", trials=len(unaddressed), silent=silent, rows=unaddressed,
         silent_with_text=sum(bool(row["unposted_final"].strip()) for row in unaddressed if not row["posts"]))

    # The stored unposted finals of the silent trials are now history. Ask what the model makes of them.
    followed = []
    for number, row in enumerate(unaddressed):
        if len(followed) == 4 or row["posts"] or not row["unposted_final"].strip():
            continue
        posts = len(world.posts())
        await world.send(world.message(
            "Belle, what was the last thing you said in this channel? Quote it exactly, or say that you have"
            " not said anything here yet.", conversation=f"C-CHAT{number}"))
        last = exchanges(session_of(world))[-1]
        followed.append({"unposted_final": row["unposted_final"], "posts": [post.text[:300] for post in world.posts()[posts:]],
                         "outcome": world.outcomes()[-1], "roles": roles(last)})
    note("unposted-final-natural", trials=len(followed), rows=followed)


async def test_model_with_its_own_unposted_statement_in_the_history(build_world):
    world = await build_world(GROUP)
    rows = []
    for number in range(2):
        script({"text": "For the record: the deploy window has moved to Friday at 15:00."})
        await world.send(world.message("ok, deploy planning is done", conversation=f"C-DEPLOY{number}"))
        posts = len(world.posts())
        await world.send(world.message("Belle, when is the deploy window? And did you already tell us?",
                                       conversation=f"C-DEPLOY{number}"))
        rows.append({"outcomes": world.outcomes()[-2:], "posts": [post.text[:300] for post in world.posts()[posts:]],
                     "roles": roles(exchanges(session_of(world))[-1])})
    note("unposted-final-scripted", trials=len(rows), rows=rows)


WANTS_A_DENIED_TOOL = (
    "Belle, fetch https://example.com/ and tell us the title of the page.",
    "Belle, create a file named notes.txt that contains the word hello.",
    "Belle, search the web for the tallest building in the world and tell us its height.",
    "Belle, run the command `date` and tell us what it printed.",
    "Belle, call the tool named webfetch with the url https://example.com/ and post what it returns.",
)


async def test_model_inside_a_restricted_roster(build_world):
    world = await build_world(GROUP)
    rows = []
    for number, text in enumerate(WANTS_A_DENIED_TOOL):
        posts = len(world.posts())
        await world.send(world.message(text, conversation=f"C-DENIED{number}"))
        asked = [shown(e) for e in exchanges(session_of(world))]
        calls = [call for e in asked for call in e["calls"]]
        rows.append({
            "text": text, "roster": asked[0]["tools"], "calls": calls,
            "calls_outside_the_roster": [call for call in calls if call not in asked[0]["tools"]],
            "outcome": world.outcomes()[-1], "posts": [post.text[:300] for post in world.posts()[posts:]],
            "requests": len(asked),
        })
    note("restricted-roster", trials=len(rows), rows=rows,
         tried_a_tool_outside_the_roster=sum(bool(row["calls_outside_the_roster"]) for row in rows))


# ── which model served a turn, what a turn cost, and whether it streamed ──────


async def test_model_per_turn_and_what_the_provider_reports(world):
    opencode = world.kit.backend
    turns = [("", "Write two sentences about rain."), (f"configured/{SECOND_MODEL}" if SECOND_MODEL else "", "Reply with the single word two."),
             ("", "Reply with the single word three.")]
    for number, (model, text) in enumerate(turns):
        turn = await direct_turn(opencode, "c-model", f"r{number}", text, model=model)
        exchange = turn["exchanges"][-1]
        note("model-per-turn", turn=number, scope_model=model, ended=turn["ended"], said=turn["text"],
             requested_model=exchange["requested_model"], served_model=exchange["served_model"], served_by=exchange["served_by"],
             stream_chunks=(exchanges(session_id(opencode._namespace, "c-model"))[-1]["response"].get("summary") or {}).get("chunks"),
             text_deltas=turn["deltas"], provider_usage=exchange["usage"], opencode_usage=turn["usage_events"])


# ── a person in the loop ─────────────────────────────────────────────────


ASK_TO_POST = f"""
scopes:
  - match: {{channel: loopback}}
    delivery: {{mode: [all], reply: optional}}
    permissions: {{tools: {{post_message: ask, {DENY_YAML}}}}}
"""


@pytest.mark.parametrize(("approved", "hold"), [(True, 0), (False, 0), (True, 90)], ids=["approve", "reject", "approve-after-90s"])
async def test_person_decides_a_tool_call_the_model_asks_for(build_world, approved, hold):
    world = await build_world(ASK_TO_POST)
    sent = asyncio.create_task(world.send(world.direct(f"Post the word PERMITTED with the {POST} tool.")))

    (question,) = await world.wait_question(timeout=240)
    assert world.posts() == []  # nothing ran while nobody had answered
    await asyncio.sleep(hold)
    await world.send(world.answer_to(question, approved=approved))
    await sent

    asked = [shown(e) for e in exchanges(session_of(world))]
    posts = [post.text for post in world.posts()]
    assert len(posts) == (1 if approved else 0)
    note("approval", approved=approved, held_seconds=hold, question=question.question.text, tool=question.question.tool,
         posts=posts, outcome=world.outcomes()[-1], requests=len(asked), calls=[call for e in asked for call in e["calls"]],
         statuses=[e["status"] for e in asked])


ALLOW_QUESTION = GROUP.replace(", question: deny", "")


async def test_person_answers_a_question_the_model_puts(build_world):
    world = await build_world(ALLOW_QUESTION)
    sent = asyncio.create_task(world.send(world.message(
        "Belle, use your question tool to ask me whether I prefer red or blue, with those two options."
        f" Then post my choice with the {POST} tool.", conversation="C-QUESTION")))

    question = None
    with contextlib.suppress(TimeoutError):
        (question,) = await world.wait_question(timeout=240)
    if question is not None:
        options = question.question.options
        choice = next((option for option in options if "red" in option.lower()), options[0] if options else "red")
        await world.send(world.answer_to(question, values=(choice,)))
    await sent

    asked = [shown(e) for e in exchanges(session_of(world))]
    note("operator-question", asked=question is not None, question=question and question.question.text,
         options=question and list(question.question.options), posts=[post.text for post in world.posts()],
         outcome=world.outcomes()[-1], requests=len(asked), calls=[call for e in asked for call in e["calls"]])
