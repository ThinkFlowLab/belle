"""Probe post_message silence against an OpenAI-compatible chat endpoint."""

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
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

DEFAULT_SKILL = Path("src/belle_agent/skills/group-chat-participation/SKILL.md")
# The one tool offered to the chat model; a test keeps it equal to Slack's POST_MESSAGE.
POST_TOOL = "post_message"
ARMS = ("chat_only", "real", "synthetic_good", "synthetic_bad")
# Synthetic-bad audience: the answer that pushes toward the wrong side of speak/silence.
BAD_AUDIENCE = {"you": "a person", "a person": "you", "nobody": "you", "the room": "a person"}
REPLY_LIMIT = 240
THRESHOLD = 0.5

_post_instruction: str | None = None


def use_post_instruction(text: str | None = None) -> None:
    """Fix the post-tool instruction: given by the host, or rendered from Belle's silence.py."""
    global _post_instruction
    _post_instruction = text


def post_instruction() -> str:
    if _post_instruction is None:
        from belle_agent.silence import post_tool_instruction

        return post_tool_instruction(POST_TOOL)
    return _post_instruction


def production_cards() -> list[dict[str, Any]]:
    """The card Slack's posting toolkit advertises for post_message (not edit or delete)."""
    from belle_agent.platforms.slack.post import _post_tool_definition

    return [_post_tool_definition()]


def tool_spec(cards_path: Path) -> list[dict[str, object]]:
    """Chat-completions tool list holding only post_message, with the production description and schema."""
    (card,) = (card for card in json.loads(cards_path.read_text()) if card["name"] == POST_TOOL)
    return [{"type": "function", "function": {
        "name": POST_TOOL, "description": card["description"], "parameters": card["input_params"]}}]


def advisory_file(cases: list[dict[str, object]], advice: str = "synthetic_good") -> Path:
    return _advisory_file(cases, advice, prefix="silence-advisories-", build=production_advisories)


def question_ids() -> tuple[str, ...]:
    """The five production questions. Their answers are advisory only; chat grades post versus silence."""
    from belle_agent.message_decisions import ALREADY_ANSWERED, AUDIENCE, CLAIM, NEEDS_WORK, RESPONSE_REQUESTED

    return (AUDIENCE, ALREADY_ANSWERED, RESPONSE_REQUESTED, NEEDS_WORK, CLAIM)


def production_questions() -> dict[str, Any]:
    from belle_agent.message_decisions import QUESTIONS

    return {key: QUESTIONS[key] for key in question_ids()}


def synthetic_values(expected: dict[str, object], accurate: bool) -> dict[str, float]:
    """Aligned (0.9/0.1) or reversed noul estimates; the audience choice is handled separately."""
    return {key: 0.9 if value == accurate else 0.1 for key, value in expected.items() if isinstance(value, bool)}


def synthetic_answers(expected: dict[str, object], accurate: bool) -> dict[str, Any]:
    """Production-shaped answers: a choice for audience and noul estimates for the rest."""
    from belle_agent.message_decisions import AUDIENCE, QUESTIONS
    from belle_agent.typed_decision import Answer

    audience = cast(str, expected[AUDIENCE])
    value = audience if accurate else BAD_AUDIENCE[audience]
    others = [key for key in QUESTIONS[AUDIENCE].criteria if key != value]
    answers = {AUDIENCE: Answer(
        type="choice", value=value,
        probabilities={value: 0.85, **{key: round(0.15 / len(others), 4) for key in others}})}
    for key, number in synthetic_values(expected, accurate).items():
        answers[key] = Answer(type="noul", value=number)
    return answers


def production_advisories(cases: list[dict[str, object]], advice: str = "synthetic_good") -> dict[str, str]:
    """Render case hints with Belle's production questions and formatter."""
    if advice not in {"synthetic_good", "synthetic_bad"}:
        raise ValueError("advice must be synthetic_good or synthetic_bad")
    from belle_agent.message_decisions import format_advisory

    questions = production_questions()
    return {
        cast(str, case["id"]): format_advisory(
            synthetic_answers(cast(dict[str, object], case["decision_expected"]), advice == "synthetic_good"), questions)
        for case in cases
    }


def reply_text(content: object) -> str:
    """Flatten chat content; None and non-text parts count as empty."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def post_texts(calls: list[Any]) -> list[str | None]:
    """Text of each post_message call; None when the call carries no args (a bare tool name)."""
    texts: list[str | None] = []
    for call in calls:
        name, args = (call, None) if isinstance(call, str) else (call.get("name"), call.get("args"))
        if name == POST_TOOL:
            texts.append(None if args is None else str(args.get("text") or "") if isinstance(args, dict) else "")
    return texts


def score_case(case: dict[str, object], calls: list[Any], final_text: object = None) -> dict[str, object]:
    """Grade the tool calls: speech is a post_message with text; the final text is withheld and ignored."""
    posts = post_texts(calls)
    usable = [text for text in posts if text is None or text.strip()]
    expected = cast(str, case["suite"])
    observed = "speak" if usable else "empty_post" if posts else "silence"
    kind, impact, severity = None, None, None
    if expected == "silence" and posts:
        expected_decision = cast(dict[str, object], case.get("decision_expected", {}))
        interrupts = expected_decision.get("audience") == "a person" or expected_decision.get("already_answered") is True
        kind, impact = "missed_silence", "unneeded_interruption"
        severity = "high" if interrupts else "medium"
    elif expected == "speak" and not posts:
        kind, impact, severity = "missed_reply", "requested_or_needed_text_withheld", "high"
    elif expected == "speak" and not usable:
        kind, impact, severity = "empty_post", "post_without_text", "medium"
    return {
        "case": case["id"], "suite": case["suite"], "expected": expected, "observed": observed,
        "pass": kind is None, "failure_kind": kind, "impact": impact, "severity": severity,
        "posts": len(posts), "post_text": (usable[0] or "")[:REPLY_LIMIT] if usable else "",
        "final_text_withheld": reply_text(final_text)[:REPLY_LIMIT],
    }


def labelled(author: str | None, text: str) -> str:
    return f"{author}: {text}" if author else text


def chat_messages(case: dict[str, object], advisory: str, skill: str) -> list[dict[str, str]]:
    """The model input: skill, post-tool silence instruction, history, advisory, latest message."""
    bot = str(case.get("bot_name") or "Belle")
    kind = str(case["conversation_kind"])
    turn = (
        f"{post_instruction()} This turn permits silence: call no {POST_TOOL} to stay silent."
    )
    system = (
        f"You are {bot}, a participant in a {KIND_LABELS.get(kind, kind)}. Follow the supplied "
        f"group-chat-participation skill. Decide whether to reach the conversation with {POST_TOOL} in "
        f"reply to the latest message, and if so write it.\n\n{turn}\n\n<skill name=\"group-chat-participation\">\n{skill}\n</skill>"
    )
    sender = cast("str | None", case.get("sender"))
    latest = labelled(sender, cast(str, case["user"]))
    latest = f"{advisory}\n\nLatest message:\n{latest}" if advisory else f"Latest message:\n{latest}"
    history = [
        {"role": message["role"], "content": labelled(message.get("author"), message["content"])}
        for message in cast("list[dict[str, str]]", case.get("recent_messages", []))
    ]
    return [{"role": "system", "content": system}, *history, {"role": "user", "content": latest}]


def parse_arguments(raw: object) -> dict[str, Any]:
    """Tool arguments as an object; text that is not a JSON object yields {} and so no usable text."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def request_case(
    case: dict[str, object],
    advisory: str,
    skill: str,
    config: dict[str, str],
    cards_path: Path,
    model_name: str,
) -> dict[str, object]:
    payload = {"model": model_name, "messages": chat_messages(case, advisory, skill),
               "tools": tool_spec(cards_path), "tool_choice": "auto"}
    request = urllib.request.Request(
        config["API_BASE"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {config['API_KEY']}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.load(response)
        message = result["choices"][0]["message"]
        calls = [{"name": entry["function"]["name"], "args": parse_arguments(entry["function"].get("arguments"))}
                 for entry in message.get("tool_calls") or []]
        content = message.get("content")
    except urllib.error.HTTPError as error:
        return provider_failure(case, f"provider HTTP {error.code}")
    except (urllib.error.URLError, TimeoutError) as error:
        return provider_failure(case, f"provider request failed ({type(error).__name__})")
    except json.JSONDecodeError:
        return provider_failure(case, "provider response was not valid JSON")
    except (KeyError, IndexError, TypeError, AttributeError):
        return provider_failure(case, "provider response did not contain a chat completion")
    return score_case(case, calls, content)


def select(cases: list[dict[str, Any]], suite: str) -> list[dict[str, Any]]:
    return [case for case in cases if suite == "all" or case["suite"] == suite]


def run_inside(skill_path: Path, cases_path: Path, cards_path: Path, advisories_path: Path,
               workers: int, chat_models_path: Path, suite: str = "all", advice: str = "synthetic_good",
               max_rpm: int = 0, instruction: str | None = None) -> int:
    use_post_instruction(instruction)
    skill = skill_path.read_text()
    config = {key: os.environ.get(key, "") for key in ENV_KEYS}
    if not all(config.values()):
        print("SKIP: API_BASE and API_KEY are required")
        return 0
    models = load_chat_models(chat_models_path)
    cases = select(json.loads(cases_path.read_text()), suite)
    advisories = json.loads(advisories_path.read_text())
    failed = False
    pacer = RequestPacer(max_rpm) if max_rpm else None

    def paced_request(case: dict[str, object], model_name: str) -> dict[str, object]:
        if pacer:
            pacer.wait()
        return request_case(case, advisories.get(case["id"], ""), skill, config, cards_path, model_name)

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


SUITES = ("silence", "speak")


def validate_cases(cases: list[dict[str, Any]]) -> None:
    """Refuse a case file that does not fit the production question contract."""
    from belle_agent import message_decisions as md

    kinds = {md.KIND_DIRECT, md.KIND_GROUP, md.KIND_PUBLIC, md.KIND_PRIVATE}
    audiences = set(md.QUESTIONS[md.AUDIENCE].criteria)
    ids = [case.get("id") for case in cases]
    if not cases or len(ids) != len(set(ids)):
        raise ValueError("cases need unique ids")
    for case in cases:
        label = f"case {case['id']}"
        if not isinstance(case.get("user"), str) or not case["user"].strip():
            raise ValueError(f"{label} needs a message")
        if case.get("suite") not in SUITES:
            raise ValueError(f"{label} suite must be one of {SUITES}")
        expected = case.get("decision_expected")
        if not isinstance(expected, dict) or set(expected) != set(question_ids()):
            raise ValueError(f"{label} needs all five expected answers")
        if expected[md.AUDIENCE] not in audiences:
            raise ValueError(f"{label} audience must be one of {sorted(audiences)}")
        if any(type(value) is not bool for key, value in expected.items() if key != md.AUDIENCE):
            raise ValueError(f"{label} noul answers must be booleans")
        if case.get("conversation_kind") not in kinds:
            raise ValueError(f"{label} conversation_kind must be one of {sorted(kinds)}")
        for message in case.get("recent_messages", []):
            if message.get("role") not in {"user", "assistant"}:
                raise ValueError(f"{label} history has an unknown role")
            if not isinstance(message.get("content"), str) or not message["content"].strip():
                raise ValueError(f"{label} history messages need content text")


def load_decision_cases(path: Path) -> list[dict[str, object]]:
    cases = json.loads(path.read_text())
    validate_cases(cases)
    return [{"id": case["id"], "last_message": case["user"], "sender": case.get("sender"),
             "recent_messages": case.get("recent_messages", []), "bot_name": case.get("bot_name", "Belle"),
             "conversation_kind": case["conversation_kind"],
             "expected": case["decision_expected"], "suite": case["suite"]} for case in cases]


def score_decision_case(case: dict[str, object], values: dict[str, object]) -> dict[str, object]:
    """Compare the audience choice by equality and noul estimates at 0.5."""
    from belle_agent.message_decisions import AUDIENCE

    expected = cast(dict[str, object], case["expected"])
    if any(isinstance(value, float) and not 0 <= value <= 1 for value in values.values()):
        raise ValueError(f"decision case {case['id']} returned a value outside 0–1")
    matches = {name: values[name] == expected[name] if name == AUDIENCE else
               (cast(float, values[name]) >= THRESHOLD) == expected[name] for name in expected}
    return {"case": case["id"], "expected": expected, "values": values, "matches": matches,
            "all_correct": all(matches.values())}


def decision_state(last_message: str, recent_messages: list[dict[str, str]], conversation_kind: str,
                   bot_name: str = "Belle", sender: str | None = None):
    from belle_agent.message_decisions import build_state, message_entry

    history = [message_entry(bot_name if entry["role"] == "assistant" else entry.get("author", "sender"),
                             entry["content"]) for entry in recent_messages]
    return build_state(channel="eval", bot_name=bot_name, conversation_kind=conversation_kind,
                       recent_messages=history, last_message=message_entry(sender or "sender", last_message))


def answer_values(answers: dict[str, Any], questions: dict[str, Any]) -> dict[str, object]:
    """Read the audience as a criteria key and every other answer as a probability."""
    from belle_agent.message_decisions import AUDIENCE

    audience = answers[AUDIENCE].value
    if audience not in questions[AUDIENCE].criteria:
        raise ValueError("audience answer is not a criteria key")
    return {key: audience if key == AUDIENCE else float(answers[key].number) for key in questions}


def decision_summary(valid: list[dict[str, object]]) -> dict[str, object]:
    """Per-question agreement, with noul confusion counts for the non-audience questions."""
    questions = question_ids()
    summary: dict[str, object] = {key: {"correct": sum(bool(result["matches"][key]) for result in valid),
                                         "total": len(valid)} for key in questions}
    for key in questions[1:]:
        counts = Counter(
            ("t" if result["matches"][key] else "f") + ("p" if (result["values"][key] >= THRESHOLD) else "n")
            for result in valid)
        summary[key].update(true_positive=counts["tp"], false_negative=counts["fn"],
                            false_positive=counts["fp"], true_negative=counts["tn"])
    return {"questions": summary}


def run_decision_probe(cases_path: Path, models_path: Path, suite: str = "all") -> int:
    from belle_agent.typed_decision import TypedDecisionError

    config = {key: os.environ.get(key, "") for key in DECISION_ENV_KEYS}
    if not all(config.values()):
        print("SKIP: DECISION_API_BASE and DECISION_API_KEY are required")
        return 0
    questions = production_questions()
    cases = select(load_decision_cases(cases_path), suite)
    models = load_decision_models(models_path)

    async def evaluate(client) -> list[dict[str, object]]:
        limit = asyncio.Semaphore(8)

        async def one(case: dict[str, object]) -> dict[str, object]:
            state = decision_state(case["last_message"], case["recent_messages"], case["conversation_kind"],
                                   case["bot_name"], case["sender"])
            async with limit:
                try:
                    answers = await client.decide(state, questions)
                    return score_decision_case(case, answer_values(answers, questions))
                except (TypedDecisionError, TimeoutError, TypeError, KeyError, ValueError) as error:
                    return {"case": case["id"], "error": type(error).__name__}

        return await asyncio.gather(*(one(case) for case in cases))

    failed = False
    for model in models:
        client = decision_client_from_env(model)
        results = asyncio.run(evaluate(client))
        for result in results:
            print(json.dumps({"model": model["model_name"], **result}, sort_keys=True))
        valid = [result for result in results if "error" not in result]
        print(json.dumps({"model": model["model_name"], "adapter": client.name, "cases": len(cases),
                          "errors": len(cases) - len(valid), **decision_summary(valid),
                          "all_correct": sum(bool(result["all_correct"]) for result in valid)}, sort_keys=True),
              file=sys.stderr)
        failed |= len(valid) != len(cases) or any(not result["all_correct"] for result in valid)
    return int(failed)


def pipeline_outcome(case: dict[str, object], values: dict[str, object],
                     runs: dict[str, dict[str, object]]) -> dict[str, object]:
    decision = score_decision_case({"id": case["id"], "expected": case["decision_expected"]}, values)
    advised_ok, unadvised_ok = bool(runs["real"]["pass"]), bool(runs["chat_only"]["pass"])
    return {
        "case": case["id"], "suite": case["suite"], "expected": case["suite"],
        "decision": decision, "runs": runs,
        "comparison": ("both_correct" if advised_ok and unadvised_ok else
                       "with_only_correct" if advised_ok else
                       "without_only_correct" if unadvised_ok else "both_wrong"),
    }


def run_pipeline_probe(cases_path: Path, skill_path: Path, cards_path: Path, decision_models_path: Path,
                       chat_models_path: Path, workers: int, suite: str = "all", max_rpm: int = 0) -> int:
    from belle_agent.message_decisions import format_advisory
    from belle_agent.typed_decision import TypedDecisionError

    config = {key: os.environ.get(key, "") for key in ENV_KEYS}
    questions = production_questions()
    decision_models = load_decision_models(decision_models_path)
    chat_models = load_chat_models(chat_models_path)
    all_cases = json.loads(cases_path.read_text())
    validate_cases(all_cases)
    cases = select(all_cases, suite)
    skill = skill_path.read_text()
    pacer = RequestPacer(max_rpm) if max_rpm else None

    async def decide_cases(client) -> list[dict[str, object]]:
        limit = asyncio.Semaphore(workers)

        async def one(case: dict[str, object]) -> dict[str, object]:
            async with limit:
                try:
                    if pacer:
                        await asyncio.to_thread(pacer.wait)
                    state = decision_state(case["user"], case.get("recent_messages", []),
                                           case["conversation_kind"], case.get("bot_name", "Belle"), case.get("sender"))
                    answers = await client.decide(state, questions)
                    return {"values": answer_values(answers, questions),
                            "advisory": format_advisory(answers, questions)}
                except (TypedDecisionError, TimeoutError, TypeError, KeyError, ValueError) as error:
                    return {"case": case["id"], "suite": case["suite"],
                            "error": f"decision {type(error).__name__}"}

        return await asyncio.gather(*(one(case) for case in cases))

    async def evaluate_chat(chat_model: str, decisions: list[dict[str, object]]) -> list[dict[str, object]]:
        limit = asyncio.Semaphore(workers)

        async def one(case: dict[str, object], decision: dict[str, object]) -> dict[str, object]:
            if "error" in decision:
                return decision
            async with limit:
                expected = cast(dict[str, object], case["decision_expected"])
                advisories = {"chat_only": "", "real": cast(str, decision["advisory"]),
                              **{label: format_advisory(synthetic_answers(expected, aligned), questions)
                                 for label, aligned in (("synthetic_good", True), ("synthetic_bad", False))}}

                def request(advisory: str) -> dict[str, object]:
                    if pacer:
                        pacer.wait()
                    return request_case(case, advisory, skill, config, cards_path, chat_model)

                results = await asyncio.gather(*(asyncio.to_thread(request, advisory)
                                                 for advisory in advisories.values()))
                if any("error" in result for result in results):
                    return {"case": case["id"], "suite": case["suite"], "error": "chat provider error"}
                return pipeline_outcome(case, cast(dict[str, object], decision["values"]),
                                        dict(zip(advisories, results, strict=True)))

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
    """Evaluate the code's post_message card, or the ``--cards`` file when one is given."""
    if args.cards is not None:
        try:
            check_cards(args.cards, [POST_TOOL])
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
        return run_host(args, args.cards.resolve())
    if args.decision_probe and not args.pipeline_probe:
        return run_host(args, None)  # the decision probe offers no tool
    cards_path = json_file(production_cards(), prefix="silence-cards-")
    try:
        return run_host(args, cards_path)
    finally:
        cards_path.unlink(missing_ok=True)


def run_host(args: argparse.Namespace, cards_path: Path | None) -> int:
    skill_path = args.skill.resolve()
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
        source = args.source.resolve()
        if not source.is_dir():
            print(f"ERROR: source directory not found: {source}")
            return 2
        return run_container(
            args.decision_image, config, keys,
            [(Path(__file__), "/probe.py"), (source, "/src"), (args.cases, "/cases.json"),
             (cards_path, "/post_cards.json"), (skill_path, "/skill.md"),
             (args.decision_models, "/decision_models.jsonl"),
             (args.chat_models, "/chat_models.jsonl")],
            ["--inside-pipeline", "--cases", "/cases.json", "--cards", "/post_cards.json", "--skill", "/skill.md",
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
        source = args.source.resolve()
        if not source.is_dir():
            print(f"ERROR: source directory not found: {source}")
            return 2
        return run_container(
            args.decision_image,
            config,
            DECISION_ENV_KEYS,
            [(Path(__file__), "/probe.py"), (source, "/src"),
             (args.cases, "/cases.json"),
             (args.decision_models, "/decision_models.jsonl")],
            ["--inside-decisions", "--cases", "/cases.json",
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

    all_cases = json.loads(args.cases.read_text())
    validate_cases(all_cases)
    cases = select(all_cases, args.suite)
    advisories_path = advisory_file(cases, args.advice)
    try:
        return run_container(
            args.image,
            config,
            ENV_KEYS,
            [
                (Path(__file__), "/probe.py"), (args.cases, "/cases.json"),
                (skill_path, "/skill.md"), (cards_path, "/post_cards.json"),
                (advisories_path, "/advisories.json"), (args.chat_models, "/chat_models.jsonl"),
            ],
            [
                "--inside", "--skill", "/skill.md", "--cases", "/cases.json", "--cards", "/post_cards.json",
                "--advisories", "/advisories.json", "--post-instruction", post_instruction(),
                "--chat-models", "/chat_models.jsonl", "--workers", str(args.workers),
                "--suite", args.suite, "--advice", args.advice, "--max-rpm", str(args.max_rpm),
            ],
        )
    finally:
        advisories_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--cards", type=Path, default=None,
                        help="tool cards JSON to evaluate instead of the code's post_message definition")
    parser.add_argument("--post-instruction", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    parser.add_argument("--advisories", type=Path, default=Path("/advisories.json"), help=argparse.SUPPRESS)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--image", default="docker.io/library/python:3.11-slim")
    parser.add_argument("--suite", choices=("all", "silence", "speak"), default="all")
    parser.add_argument("--advice", choices=("synthetic_good", "synthetic_bad", "none"),
                        default="synthetic_good")
    parser.add_argument("--workers", type=int, default=16, help="concurrent model requests (1–32)")
    parser.add_argument("--max-rpm", type=int, default=0,
                        help="maximum chat request starts per minute; 0 disables pacing")
    parser.add_argument("--pipeline-workers", type=int, default=2,
                        help="concurrent paired decision/chat cases (1–32)")
    parser.add_argument("--decision-probe", action="store_true", help="probe real decision-model estimates")
    parser.add_argument("--pipeline-probe", action="store_true", help="compare decision answers and chat replies per case")
    parser.add_argument("--decision-models", type=Path,
                        default=Path(__file__).resolve().parent.parent / "models" / "decision_models.jsonl",
                        help="JSONL list of decision models for decision and pipeline probes")
    parser.add_argument("--chat-models", type=Path,
                        default=Path(__file__).resolve().parent.parent / "models" / "chat_models.jsonl",
                        help="JSONL list of chat models for chat and pipeline probes")
    parser.add_argument("--source", type=Path, default=Path("src"))
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
        return run_decision_probe(args.cases, args.decision_models, args.suite)
    if args.inside_pipeline:
        return run_pipeline_probe(args.cases, args.skill, args.cards, args.decision_models,
                                  args.chat_models, args.pipeline_workers, args.suite, args.max_rpm)
    if args.inside:
        return run_inside(args.skill, args.cases, args.cards, args.advisories, args.workers, args.chat_models,
                          args.suite, args.advice, args.max_rpm, args.post_instruction)
    return host_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
