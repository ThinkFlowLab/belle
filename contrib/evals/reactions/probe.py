"""Probe ``react_to_message`` use against an OpenAI-compatible chat endpoint.

Pass/fail is based on usable reaction tool calls. Assistant text is logged.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, cast

_COMMON = Path(__file__).resolve().parent.parent / "common"
if not (_COMMON / "host.py").is_file():
    _COMMON = Path("/common")
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))
from host import (  # noqa: E402
    DECISION_ENV_KEYS,
    ENV_KEYS,
    KIND_LABELS,
    RequestPacer,
    advisory_file as _advisory_file,
    check_cards,
    decision_client_from_env,
    load_chat_models,
    json_file,
    load_decision_models,
    probe_config,
    provider_failure,
    run_container,
    suite_summary,
)

_PARTS = Path(__file__).resolve().parents
# Inside the model container the probe sits at /probe.py with no repository around it.
ROOT = _PARTS[3] if len(_PARTS) > 3 else Path("/")
DEFAULT_SKILL = ROOT / "src/belle_agent/skills/group-chat-participation/SKILL.md"
# Operator example: replaces deserves_emoji wording and adds the emoji choice.
DEFAULT_QUESTIONS = ROOT / "examples/config/slack-decision-questions.json"
SUITES = ("react", "no_react")
ARMS = ("chat_only", "real", "synthetic_good", "synthetic_bad")
REACT_TOOL = "react_to_message"
CASE_TOOLS = [REACT_TOOL]
EMOJI = "emoji"
MAX_TOOL_ROUNDS = 1
FAILURE_SEVERITY = {
    "unnecessary_reaction": "low",
    "missed_reaction": "medium",
}
WRONG_AUDIENCE = {"nobody": "you", "the room": "nobody", "a person": "you", "you": "nobody"}
WRONG_EMOJI = {
    "no_reaction": "joy",
    "joy": "no_reaction",
    "thumbsup": "no_reaction",
    "heart": "no_reaction",
    "thinking_face": "joy",
    "tada": "no_reaction",
    "clap": "no_reaction",
    "wave": "no_reaction",
    "open_mouth": "joy",
    "other_emoji": "no_reaction",
}


def ensure_source() -> None:
    source = str(ROOT / "src")
    if source not in sys.path:
        sys.path.insert(0, source)


def production_cards() -> list[dict[str, Any]]:
    """The tool cards the Slack reaction toolkit advertises to the model."""
    ensure_source()
    from belle_agent.platforms.slack.reactions import SlackReactionToolkit

    return SlackReactionToolkit().get_tool_definitions()


def advisory_file(cases: list[dict[str, object]], advice: str = "synthetic_good",
                  questions_path: Path | None = None) -> Path:
    return _advisory_file(
        cases, advice, prefix="reaction-advisories-",
        build=lambda selected, mode: production_advisories(selected, mode, questions_path=questions_path),
    )


def decision_question_ids() -> tuple[str, ...]:
    from belle_agent.message_decisions import AUDIENCE, DESERVES_EMOJI, RESPONSE_REQUESTED

    return DESERVES_EMOJI, EMOJI, AUDIENCE, RESPONSE_REQUESTED


def decision_questions(path: Path | None = None) -> dict[str, Any]:
    """Load the operator example questions, then keep the probe's decision subset."""
    ensure_source()
    from belle_agent.message_decisions import load_questions

    questions_path = path or DEFAULT_QUESTIONS
    if not questions_path.is_file():
        raise FileNotFoundError(f"decision questions file not found: {questions_path}")
    loaded = load_questions(questions_path)
    return {key: loaded[key] for key in decision_question_ids()}


def choice_answer(question: Any, choice: str) -> Any:
    from belle_agent.typed_decision import Answer

    keys = list(question.criteria or {})
    others = [name for name in keys if name != choice]
    probabilities = {choice: 0.9, **{name: (0.1 / len(others) if others else 0.0) for name in others}}
    return Answer(type="choice", value=choice, probabilities=probabilities)


def synthetic_answers(expected: dict[str, Any], accurate: bool,
                      questions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Answers that agree (0.9 / the true choice) or disagree with the case labels."""
    from belle_agent.message_decisions import AUDIENCE
    from belle_agent.typed_decision import Answer

    questions = questions or decision_questions()
    answers: dict[str, Answer] = {}
    for key, value in expected.items():
        if key == AUDIENCE:
            choice = value if accurate else WRONG_AUDIENCE[str(value)]
            answers[key] = choice_answer(questions[AUDIENCE], choice)
        elif key == EMOJI:
            choice = value if accurate else WRONG_EMOJI[str(value)]
            answers[key] = choice_answer(questions[EMOJI], choice)
        else:
            answers[key] = Answer(type="noul", value=0.9 if value == accurate else 0.1)
    return answers


def production_advisories(cases: list[dict[str, object]], advice: str = "synthetic_good",
                          questions_path: Path | None = None) -> dict[str, str]:
    """Render case hints with the operator question file and Belle's formatter."""
    if advice not in {"synthetic_good", "synthetic_bad"}:
        raise ValueError("advice must be synthetic_good or synthetic_bad")
    ensure_source()
    from belle_agent.message_decisions import format_advisory

    questions = decision_questions(questions_path)
    return {
        cast(str, case["id"]): format_advisory(
            synthetic_answers(
                cast(dict[str, Any], case["decision_expected"]),
                advice == "synthetic_good",
                questions,
            ),
            questions,
        )
        for case in cases
    }


def validate_cases(cases: list[dict[str, Any]], questions_path: Path | None = None) -> None:
    """Refuse a case file that does not fit the production question and tool contract."""
    ensure_source()
    from belle_agent import message_decisions as md

    questions = decision_questions(questions_path)
    kinds = {md.KIND_DIRECT, md.KIND_GROUP, md.KIND_PUBLIC, md.KIND_PRIVATE}
    audiences = set(questions[md.AUDIENCE].criteria or {})
    emojis = set(questions[EMOJI].criteria or {})
    ids = [case.get("id") for case in cases]
    if not cases or len(ids) != len(set(ids)):
        raise ValueError("cases need unique ids")
    for case in cases:
        name = case["id"]
        if not isinstance(case.get("user"), str) or not case["user"].strip():
            raise ValueError(f"case {name} needs a user message")
        if case.get("conversation_kind") not in kinds:
            raise ValueError(f"case {name} conversation_kind must be one of {sorted(kinds)}")
        expected = case.get("decision_expected")
        if not isinstance(expected, dict) or set(expected) != set(decision_question_ids()):
            raise ValueError(f"case {name} decision_expected needs exactly {sorted(decision_question_ids())}")
        for key in (md.DESERVES_EMOJI, md.RESPONSE_REQUESTED):
            if type(expected[key]) is not bool:
                raise ValueError(f"case {name} {key} must be a boolean")
        if expected[md.AUDIENCE] not in audiences:
            raise ValueError(f"case {name} audience must be one of {sorted(audiences)}")
        if expected[EMOJI] not in emojis:
            raise ValueError(f"case {name} emoji must be one of {sorted(emojis)}")
        if case.get("suite") not in SUITES:
            raise ValueError(f"case {name} suite must be one of {SUITES}")
        for message in case.get("recent_messages", []):
            if set(message) != {"author", "text"} or not all(isinstance(v, str) and v for v in message.values()):
                raise ValueError(f"case {name} recent_messages need author and text strings")


def tool_spec(names: list[str], cards_paths: Sequence[Path]) -> list[dict[str, object]]:
    cards = {card["name"]: card for path in cards_paths for card in json.loads(path.read_text())}
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": cards[name]["description"],
                "parameters": cards[name]["input_params"],
            },
        }
        for name in names
    ]


def system_prompt(case: dict[str, object], skill: str) -> str:
    kind = KIND_LABELS.get(str(case.get("conversation_kind")), "conversation")
    return (
        f"You are {case.get('bot_name', 'Belle')}, a participant in a {kind}. Follow the supplied "
        "group-chat-participation skill, and use react_to_message when a reaction is appropriate.\n\n"
        f"<skill name=\"group-chat-participation\">\n{skill}\n</skill>"
    )


def transcript(case: dict[str, object]) -> str:
    lines = [f"{m['author']}: {m['text']}" for m in cast(list[dict[str, str]], case.get("recent_messages", []))]
    history = "Recent messages:\n" + "\n".join(lines) + "\n\n" if lines else ""
    return f"{history}New message from {case.get('sender', 'sender')}:\n{case['user']}"


def user_content(case: dict[str, object], advisory: str) -> str:
    return f"{advisory}\n\n{transcript(case)}" if advisory else transcript(case)


def reaction_emoji(call: dict[str, Any]) -> str | None:
    """The emoji of one react call that adds a reaction, if its arguments are usable."""
    try:
        arguments = json.loads(call["function"].get("arguments") or "{}")
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(arguments, dict) or arguments.get("remove") is True:
        return None
    emoji = arguments.get("emoji")
    return (emoji.strip().strip(":").strip() or None) if isinstance(emoji, str) else None


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def score_case(case: dict[str, object], observed: dict[str, Any]) -> dict[str, object]:
    """Grade whether a usable reaction occurred. Assistant text is ignored."""
    expected = cast(str, case["suite"])
    reacted = bool(observed["reactions"])
    if expected == "react":
        kind, impact = (None, None) if reacted else ("missed_reaction", "missing_acknowledgement")
    elif expected == "no_react":
        kind, impact = ("unnecessary_reaction", "unwanted_reaction") if reacted else (None, None)
    else:
        raise ValueError(f"unknown suite {expected!r}")
    return {
        "case": case["id"], "suite": case["suite"], "expected": expected,
        "observed_tools": observed["calls"], "reactions": observed["reactions"],
        "final_text": str(observed["content"]).strip()[:200],
        "pass": kind is None, "failure_kind": kind, "impact": impact,
        "severity": FAILURE_SEVERITY.get(kind) if kind else None,
    }


def reaction_result(emoji: str | None) -> str:
    """A tool result shaped like the production success payload (the call is never executed)."""
    if emoji is None:
        return json.dumps({"ok": False, "error": "emoji_required"})
    return json.dumps({"ok": True, "chat_id": "C0EVAL", "message_id": "1700000000.000100",
                       "emoji": emoji, "removed": False}, separators=(",", ":"))


def post_chat(payload: dict[str, object], config: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        config["API_BASE"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {config['API_KEY']}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def request_case(
    case: dict[str, object],
    advisory: str,
    skill: str,
    config: dict[str, str],
    cards_paths: Sequence[Path],
    model_name: str,
    pacer: RequestPacer | None = None,
) -> dict[str, object]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt(case, skill)},
        {"role": "user", "content": user_content(case, advisory)},
    ]
    tools = tool_spec(CASE_TOOLS, cards_paths)
    calls: list[str] = []
    reactions: list[str] = []
    text = ""
    try:
        for _ in range(MAX_TOOL_ROUNDS + 1):
            if pacer:
                pacer.wait()
            result = post_chat({"model": model_name, "messages": messages, "tools": tools,
                                "tool_choice": "auto"}, config)
            try:
                message = result["choices"][0]["message"]
                tool_calls = message.get("tool_calls") or []
                calls.extend(entry["function"]["name"] for entry in tool_calls)
            except (KeyError, IndexError, TypeError, AttributeError):
                return provider_failure(case, "provider response did not contain a chat completion")
            text = message_text(message).strip()
            if not tool_calls:
                break
            outcomes = []
            for entry in tool_calls:
                emoji = reaction_emoji(entry) if entry["function"]["name"] == REACT_TOOL else None
                if emoji:
                    reactions.append(emoji)
                outcomes.append({"role": "tool", "tool_call_id": entry.get("id", ""),
                                 "content": reaction_result(emoji)})
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": tool_calls})
            messages.extend(outcomes)
    except urllib.error.HTTPError as error:
        return provider_failure(case, f"provider HTTP {error.code}")
    except (urllib.error.URLError, TimeoutError) as error:
        return provider_failure(case, f"provider request failed ({type(error).__name__})")
    except json.JSONDecodeError:
        return provider_failure(case, "provider response was not valid JSON")
    return score_case(case, {"calls": calls, "reactions": reactions, "content": text})


def run_inside(skill_path: Path, cases_path: Path, cards_paths: Sequence[Path], advisories_path: Path,
               workers: int, chat_models_path: Path, suite: str = "all", advice: str = "fixed",
               max_rpm: int = 0) -> int:
    skill = skill_path.read_text()
    config = {key: os.environ.get(key, "") for key in ENV_KEYS}
    if not all(config.values()):
        print("SKIP: API_BASE and API_KEY are required")
        return 0
    models = load_chat_models(chat_models_path)
    cases = [case for case in json.loads(cases_path.read_text())
             if suite == "all" or case["suite"] == suite]
    advisories = json.loads(advisories_path.read_text())
    failed = False
    pacer = RequestPacer(max_rpm) if max_rpm else None

    def paced_request(case: dict[str, object], model_name: str) -> dict[str, object]:
        return request_case(case, advisories.get(case["id"], ""), skill, config, cards_paths, model_name,
                            pacer=pacer)

    for model_name in models:
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(paced_request, case, model_name) for case in cases]
            for completed, future in enumerate(as_completed(futures), 1):
                result = future.result()
                result.update(chat_model=model_name, advice=advice)
                print(json.dumps(result, sort_keys=True), flush=True)
                print(f"{model_name}: {completed}/{len(cases)} cases complete", file=sys.stderr, flush=True)
                results.append(result)
        summaries = {
            name: suite_summary([result for result in results if result["suite"] == name])
            for name in SUITES if suite in ("all", name)
        }
        print(json.dumps({"chat_model": model_name, "advice": advice, "summary": summaries}, sort_keys=True),
              file=sys.stderr, flush=True)
        failed |= any(not result["pass"] for result in results)
    return int(failed)


def load_decision_cases(path: Path, questions_path: Path | None = None) -> list[dict[str, object]]:
    cases = json.loads(path.read_text())
    validate_cases(cases, questions_path)
    return [{"id": case["id"], "last_message": case["user"], "sender": case.get("sender", "sender"),
             "recent_messages": case.get("recent_messages", []),
             "bot_name": case.get("bot_name", "Belle"),
             "conversation_kind": case["conversation_kind"],
             "expected": case["decision_expected"], "suite": case["suite"]} for case in cases]


def answer_value(answer: Any) -> float | str:
    """A noul answer's probability, or a choice answer's key."""
    if answer.type == "choice":
        return str(answer.value)
    if answer.number is None:
        raise ValueError("noul answer was not numeric")
    return float(answer.number)


def score_decision_case(case: dict[str, object], values: dict[str, float | str],
                        questions: dict[str, Any] | None = None) -> dict[str, object]:
    from belle_agent.message_decisions import AUDIENCE

    questions = questions or decision_questions()
    expected = cast(dict[str, Any], case["expected"])
    matches: dict[str, bool] = {}
    for name, value in expected.items():
        got = values[name]
        if name in (AUDIENCE, EMOJI):
            allowed = questions[name].criteria or {}
            if got not in allowed:
                raise ValueError(f"decision case {case['id']} returned an unknown {name} {got!r}")
            matches[name] = got == value
        else:
            if not isinstance(got, float) or not 0 <= got <= 1:
                raise ValueError(f"decision case {case['id']} returned a value outside 0–1")
            matches[name] = (got >= 0.5) == value
    return {"case": case["id"], "expected": expected, "values": values, "matches": matches,
            "all_correct": all(matches.values())}


def decision_state(case: dict[str, object]):
    from belle_agent.message_decisions import build_state, message_entry

    history = [message_entry(m["author"], m["text"]) for m in cast(list[dict[str, str]], case["recent_messages"])]
    return build_state(channel="eval", bot_name=cast(str, case["bot_name"]),
                       conversation_kind=cast(str, case["conversation_kind"]), recent_messages=history,
                       last_message=message_entry(cast(str, case["sender"]), cast(str, case["last_message"])))


def run_decision_probe(cases_path: Path, models_path: Path, suite: str = "all",
                       questions_path: Path | None = None) -> int:
    from belle_agent.typed_decision import TypedDecisionError

    config = {key: os.environ.get(key, "") for key in DECISION_ENV_KEYS}
    if not all(config.values()):
        print("SKIP: DECISION_API_BASE and DECISION_API_KEY are required")
        return 0
    questions = decision_questions(questions_path)
    cases = [case for case in load_decision_cases(cases_path, questions_path)
             if suite == "all" or case["suite"] == suite]
    models = load_decision_models(models_path)

    async def evaluate(client) -> list[dict[str, object]]:
        limit = asyncio.Semaphore(8)

        async def one(case: dict[str, object]) -> dict[str, object]:
            async with limit:
                try:
                    answers = await client.decide(decision_state(case), questions)
                    return score_decision_case(
                        case, {key: answer_value(answers[key]) for key in questions}, questions)
                except (TypedDecisionError, TimeoutError, ValueError, KeyError) as error:
                    return {"case": case["id"], "error": type(error).__name__}

        return await asyncio.gather(*(one(case) for case in cases))

    failed = False
    for model in models:
        client = decision_client_from_env(model)
        results = asyncio.run(evaluate(client))
        for result in results:
            print(json.dumps({"model": model["model_name"], **result}, sort_keys=True))
        valid = [result for result in results if "error" not in result]
        summary = {key: {"correct": sum(bool(result["matches"][key]) for result in valid),
                         "total": len(valid)} for key in questions}
        print(json.dumps({"model": model["model_name"], "adapter": client.name, "cases": len(cases),
                          "errors": len(cases) - len(valid), "questions": summary,
                          "all_correct": sum(bool(result["all_correct"]) for result in valid)}, sort_keys=True),
              file=sys.stderr)
        failed |= len(valid) != len(cases) or any(not result["all_correct"] for result in valid)
    return int(failed)


def pipeline_outcome(case: dict[str, object], values: dict[str, float | str],
                     runs: dict[str, dict[str, object]],
                     questions: dict[str, Any] | None = None) -> dict[str, object]:
    decision = score_decision_case(
        {"id": case["id"], "expected": case["decision_expected"]}, values, questions)
    advised_ok, unadvised_ok = bool(runs["real"]["pass"]), bool(runs["chat_only"]["pass"])
    return {
        "case": case["id"], "suite": case["suite"], "expected": case["suite"],
        "decision": decision, "runs": runs,
        "comparison": ("both_correct" if advised_ok and unadvised_ok else
                       "with_only_correct" if advised_ok else
                       "without_only_correct" if unadvised_ok else "both_wrong"),
    }


def run_pipeline_probe(cases_path: Path, skill_path: Path, cards_paths: Sequence[Path],
                       decision_models_path: Path, chat_models_path: Path, workers: int,
                       suite: str = "all", max_rpm: int = 0, questions_path: Path | None = None) -> int:
    from belle_agent.message_decisions import format_advisory
    from belle_agent.typed_decision import TypedDecisionError

    config = {key: os.environ.get(key, "") for key in ENV_KEYS}
    questions = decision_questions(questions_path)
    decision_models = load_decision_models(decision_models_path)
    chat_models = load_chat_models(chat_models_path)
    cases = json.loads(cases_path.read_text())
    validate_cases(cases, questions_path)
    cases = [case for case in cases if suite == "all" or case["suite"] == suite]
    skill = skill_path.read_text()
    pacer = RequestPacer(max_rpm) if max_rpm else None

    async def decide_cases(client) -> list[dict[str, object]]:
        limit = asyncio.Semaphore(workers)

        async def one(case: dict[str, object]) -> dict[str, object]:
            async with limit:
                try:
                    if pacer:
                        await asyncio.to_thread(pacer.wait)
                    state_case = {"bot_name": case.get("bot_name", "Belle"), "sender": case.get("sender", "sender"),
                                  "conversation_kind": case["conversation_kind"], "last_message": case["user"],
                                  "recent_messages": case.get("recent_messages", [])}
                    answers = await client.decide(decision_state(state_case), questions)
                    values = {key: answer_value(answers[key]) for key in questions}
                    return {"values": values, "advisory": format_advisory(answers, questions)}
                except (TypedDecisionError, TimeoutError, TypeError, ValueError, KeyError) as error:
                    return {"case": case["id"], "suite": case["suite"],
                            "error": f"decision {type(error).__name__}"}

        return await asyncio.gather(*(one(case) for case in cases))

    async def evaluate_chat(chat_model: str, decisions: list[dict[str, object]]) -> list[dict[str, object]]:
        limit = asyncio.Semaphore(workers)

        async def one(case: dict[str, object], decision: dict[str, object]) -> dict[str, object]:
            if "error" in decision:
                return decision
            async with limit:
                expected = cast(dict[str, Any], case["decision_expected"])
                synthetics = {
                    label: format_advisory(
                        synthetic_answers(expected, aligned, questions), questions)
                    for label, aligned in (("synthetic_good", True), ("synthetic_bad", False))
                }
                advisories = {"chat_only": "", "real": cast(str, decision["advisory"]), **synthetics}

                def request(advisory: str) -> dict[str, object]:
                    return request_case(case, advisory, skill, config, cards_paths, chat_model, pacer=pacer)

                results = await asyncio.gather(*(asyncio.to_thread(request, advisory)
                                                 for advisory in advisories.values()))
                if any("error" in result for result in results):
                    return {"case": case["id"], "suite": case["suite"], "error": "chat provider error"}
                return pipeline_outcome(case, cast(dict[str, float | str], decision["values"]),
                                        dict(zip(advisories, results, strict=True)), questions)

        return await asyncio.gather(*(one(case, decision) for case, decision in zip(cases, decisions, strict=True)))

    failed = False
    for decision_model in decision_models:
        client = decision_client_from_env(decision_model)
        decisions = asyncio.run(decide_cases(client))
        for chat_model in chat_models:
            results = asyncio.run(evaluate_chat(chat_model, decisions))
            for result in results:
                print(json.dumps({"decision_model": decision_model["model_name"],
                                  "chat_model": chat_model, **result}, sort_keys=True))
            valid = [result for result in results if "error" not in result]
            print(json.dumps({"decision_model": decision_model["model_name"], "chat_model": chat_model,
                              "adapter": client.name, "cases": len(cases), "errors": len(cases) - len(valid),
                              "comparisons": dict(Counter(result["comparison"] for result in valid)),
                              "runs": {arm: {name: suite_summary([result["runs"][arm] for result in valid
                                                                  if result["suite"] == name])
                                             for name in SUITES if suite in ("all", name)}
                                       for arm in ARMS}},
                             sort_keys=True), file=sys.stderr)
            failed |= len(valid) != len(cases) or any(not result["runs"]["real"]["pass"] for result in valid)
    return int(failed)


def host_main(args: argparse.Namespace) -> int:
    """Evaluate the code's reaction cards, or the ``--cards`` file when one is given."""
    if args.cards is not None:
        try:
            check_cards(args.cards, CASE_TOOLS)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
        return run_host(args, args.cards.resolve())
    if args.decision_probe and not args.pipeline_probe:
        return run_host(args, None)  # the decision probe offers no tool
    cards_path = json_file(production_cards(), prefix="reaction-cards-")
    try:
        return run_host(args, cards_path)
    finally:
        cards_path.unlink(missing_ok=True)


def run_host(args: argparse.Namespace, cards_path: Path | None) -> int:
    skill_path = args.skill.resolve()
    questions_path = args.questions.resolve()
    if args.pipeline_probe:
        keys = ENV_KEYS + DECISION_ENV_KEYS
        config = probe_config(args.env_file, keys)
        missing = [key for key in keys if not config.get(key)]
        if missing:
            print("SKIP: pipeline model credentials unavailable; set " + ", ".join(missing))
            return 0
        if not re.fullmatch(r"https?://[^\s]+", config["API_BASE"]):
            print("ERROR: API_BASE must be an HTTP(S) URL")
            return 2
        if not skill_path.is_file():
            print(f"SKIP: group-chat-participation skill is not available at {skill_path}")
            return 0
        if not questions_path.is_file():
            print(f"ERROR: decision questions file not found: {questions_path}")
            return 2
        source = args.source.resolve()
        if not source.is_dir():
            print(f"ERROR: source directory not found: {source}")
            return 2
        return run_container(
            args.decision_image, config, keys,
            [(Path(__file__), "/probe.py"), (source, "/src"), (args.cases, "/cases.json"),
             (cards_path, "/reaction_cards.json"),
             (skill_path, "/skill.md"), (questions_path, "/questions.json"),
             (args.decision_models, "/decision_models.jsonl"),
             (args.chat_models, "/chat_models.jsonl")],
            ["--inside-pipeline", "--cases", "/cases.json",
             "--cards", "/reaction_cards.json",
             "--skill", "/skill.md", "--questions", "/questions.json",
             "--decision-models", "/decision_models.jsonl", "--chat-models", "/chat_models.jsonl",
             "--pipeline-workers", str(args.pipeline_workers), "--suite", args.suite,
             "--max-rpm", str(args.max_rpm)],
            ("PYTHONPATH=/src",),
        )
    if args.decision_probe:
        config = probe_config(args.env_file, DECISION_ENV_KEYS)
        missing = [key for key in DECISION_ENV_KEYS if not config.get(key)]
        if missing:
            print("SKIP: decision model credentials unavailable; set " + ", ".join(missing))
            return 0
        if not questions_path.is_file():
            print(f"ERROR: decision questions file not found: {questions_path}")
            return 2
        source = args.source.resolve()
        if not source.is_dir():
            print(f"ERROR: source directory not found: {source}")
            return 2
        return run_container(
            args.decision_image, config, DECISION_ENV_KEYS,
            [(Path(__file__), "/probe.py"), (source, "/src"), (args.cases, "/cases.json"),
             (questions_path, "/questions.json"),
             (args.decision_models, "/decision_models.jsonl")],
            ["--inside-decisions", "--cases", "/cases.json", "--questions", "/questions.json",
             "--decision-models", "/decision_models.jsonl", "--suite", args.suite],
            ("PYTHONPATH=/src",),
        )
    config = probe_config(args.env_file, ENV_KEYS)
    missing = [key for key in ENV_KEYS if not config.get(key)]
    if missing:
        print("SKIP: model credentials unavailable; set " + ", ".join(missing))
        return 0
    if not skill_path.is_file():
        print(f"SKIP: group-chat-participation skill is not available at {skill_path}")
        return 0
    if not re.fullmatch(r"https?://[^\s]+", config["API_BASE"]):
        print("ERROR: API_BASE must be an HTTP(S) URL")
        return 2
    if not questions_path.is_file():
        print(f"ERROR: decision questions file not found: {questions_path}")
        return 2

    cases = json.loads(args.cases.read_text())
    validate_cases(cases, questions_path)
    advisories_path = advisory_file(
        [case for case in cases if args.suite == "all" or case["suite"] == args.suite], args.advice,
        questions_path=questions_path)
    try:
        return run_container(
            args.image, config, ENV_KEYS,
            [
                (Path(__file__), "/probe.py"), (args.cases, "/cases.json"),
                (cards_path, "/reaction_cards.json"),
                (skill_path, "/skill.md"),
                (advisories_path, "/advisories.json"), (args.chat_models, "/chat_models.jsonl"),
            ],
            [
                "--inside", "--skill", "/skill.md", "--cases", "/cases.json",
                "--cards", "/reaction_cards.json",
                "--advisories", "/advisories.json",
                "--chat-models", "/chat_models.jsonl", "--workers", str(args.workers),
                "--suite", args.suite, "--advice", args.advice, "--max-rpm", str(args.max_rpm),
            ],
        )
    finally:
        advisories_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    parser.add_argument("--cards", type=Path, default=None,
                        help="tool cards JSON to evaluate instead of the code's reaction toolkit definitions")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS,
                        help="operator decision-question JSON used by decision and pipeline probes")
    parser.add_argument("--advisories", type=Path, default=Path("/advisories.json"), help=argparse.SUPPRESS)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--image", default="docker.io/library/python:3.11-slim")
    parser.add_argument("--suite", choices=("all", *SUITES), default="all")
    parser.add_argument("--advice", choices=("synthetic_good", "synthetic_bad", "none"),
                        default="synthetic_good")
    parser.add_argument("--workers", type=int, default=16, help="concurrent model requests (1–32)")
    parser.add_argument("--max-rpm", type=int, default=0,
                        help="maximum chat request starts per minute; 0 disables pacing")
    parser.add_argument("--pipeline-workers", type=int, default=2,
                        help="concurrent paired decision/chat cases (1–32)")
    parser.add_argument("--decision-probe", action="store_true", help="probe real decision-model estimates")
    parser.add_argument("--pipeline-probe", action="store_true", help="compare decision answers and chat actions per case")
    parser.add_argument("--decision-models", type=Path,
                        default=Path(__file__).resolve().parent.parent / "models" / "decision_models.jsonl",
                        help="JSONL list of decision models for decision and pipeline probes")
    parser.add_argument("--chat-models", type=Path,
                        default=Path(__file__).resolve().parent.parent / "models" / "chat_models.jsonl",
                        help="JSONL list of chat models for chat and pipeline probes")
    parser.add_argument("--source", type=Path, default=ROOT / "src")
    parser.add_argument("--decision-image", default="localhost/belle:launcher-local")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--inside-decisions", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--inside-pipeline", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        parser.error("--workers must be between 1 and 32")
    if args.max_rpm < 0:
        parser.error("--max-rpm must be zero or greater")
    if not 1 <= args.pipeline_workers <= 32:
        parser.error("--pipeline-workers must be between 1 and 32")
    if args.inside_decisions:
        return run_decision_probe(args.cases, args.decision_models, args.suite, args.questions)
    if args.inside_pipeline:
        return run_pipeline_probe(args.cases, args.skill, (args.cards,), args.decision_models,
                                  args.chat_models, args.pipeline_workers, args.suite, args.max_rpm,
                                  args.questions)
    if args.inside:
        return run_inside(args.skill, args.cases, (args.cards,), args.advisories,
                          args.workers, args.chat_models, args.suite, args.advice, args.max_rpm)
    return host_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
