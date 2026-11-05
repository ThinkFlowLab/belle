"""Probe session-work guidance against an OpenAI-compatible chat endpoint."""

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
from typing import cast

_COMMON = Path(__file__).resolve().parent.parent / "common"
if not (_COMMON / "host.py").is_file():
    _COMMON = Path("/common")
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))
from host import (  # noqa: E402
    DECISION_ENV_KEYS,
    ENV_KEYS,
    RequestPacer,
    advisory_file as _advisory_file,
    decision_client_from_env,
    load_chat_models,
    load_decision_models,
    probe_config,
    provider_failure,
    run_container,
    suite_summary,
)

DEFAULT_SKILL = Path("src/belle_agent/skills/session-work/SKILL.md")


def advisory_file(cases: list[dict[str, object]], advice: str = "synthetic_good") -> Path:
    return _advisory_file(cases, advice, prefix="session-advisories-", build=production_advisories)


def synthetic_values(expected: dict[str, bool], accurate: bool) -> dict[str, float]:
    return {key: 0.9 if value == accurate else 0.1 for key, value in expected.items()}


def production_advisories(cases: list[dict[str, object]], advice: str = "synthetic_good") -> dict[str, str]:
    """Render case hints with Belle's production questions and formatter."""
    if advice not in {"synthetic_good", "synthetic_bad"}:
        raise ValueError("advice must be synthetic_good or synthetic_bad")
    source = Path(__file__).resolve().parents[3] / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    from belle_agent.message_decisions import (
        CONTINUATION_CONTEXT,
        INDEPENDENT_WORK,
        QUESTIONS,
        format_advisory,
    )
    from belle_agent.typed_decision import Answer

    question_ids = (INDEPENDENT_WORK, CONTINUATION_CONTEXT)
    questions = {key: QUESTIONS[key] for key in question_ids}
    advisories: dict[str, str] = {}
    for case in cases:
        case_id = cast(str, case["id"])
        expected = cast(dict[str, bool], case["decision_expected"])
        values = synthetic_values(expected, advice == "synthetic_good")
        advisories[case_id] = format_advisory(
            {
                key: Answer(type="noul", value=values[key])
                for key in question_ids
            },
            questions,
        )
    return advisories


def tool_spec(names: list[str], cards_path: Path) -> list[dict[str, object]]:
    cards = {card["name"]: card for card in json.loads(cards_path.read_text())}
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


def score_case(case: dict[str, object], calls: list[str]) -> dict[str, object]:
    """Classify tool-choice failures by their observable consequence."""
    expected = case["expect"]
    passed = (not any(name.startswith("session_") for name in calls)
              if expected == "no_session_tool" else calls == [expected])
    kind, impact = None, None
    if not passed:
        if "session_start" in calls and expected != "session_start":
            kind = "unnecessary_detach"
            impact = "interrupted_interaction" if case.get("interaction_required") else "unintended_background_work"
        elif not calls:
            kind, impact = {
                "session_start": ("missed_detach", "foreground_work"),
                "session_continue": ("missed_continuation", "missing_retained_context"),
            }.get(expected, ("missed_lookup", "missing_session_information"))
        elif expected == "no_session_tool" and all(name in {"session_list", "session_get", "session_result"} for name in calls):
            kind, impact = "unnecessary_lookup", "unnecessary_session_read"
        elif expected in {"session_start", "session_continue"} and calls in (["session_list"], ["session_get"]):
            kind, impact = "intermediate_lookup", "expected_action_not_yet_observed"
        else:
            kind, impact = "wrong_tool", "incorrect_session_action"
    severity = None
    if not passed:
        severity = "high" if kind in {"missed_detach", "missed_continuation"} or impact == "interrupted_interaction" else (
            "low" if kind in {"unnecessary_lookup", "intermediate_lookup"} else "medium"
        )
    return {
        "case": case["id"], "suite": case["suite"], "severity": severity,
        "observed_tools": calls, "expected": expected, "pass": passed,
        "failure_kind": kind, "impact": impact,
    }


def request_case(
    case: dict[str, object],
    advisory: str,
    skill: str,
    config: dict[str, str],
    cards_path: Path,
    model_name: str,
) -> dict[str, object]:
    context = f"{advisory}\n\nUser request: {case['user']}" if advisory else f"User request: {case['user']}"
    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant. Follow the supplied session-work skill. "
                    "Use only the session tools listed for this case. Decide whether to answer in "
                    "this turn or call a session tool. Do not execute work in a tool call; provide "
                    "a concise acknowledgement if detaching.\n\n"
                    f"<skill name=\"session-work\">\n{skill}\n</skill>"
                ),
            },
            *case.get("recent_messages", []),
            {"role": "user", "content": context},
        ],
        "tools": tool_spec(case["tools"], cards_path),
        "tool_choice": "auto",
    }
    url = config["API_BASE"].rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {config['API_KEY']}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.load(response)
        message = result["choices"][0]["message"]
        calls = [entry["function"]["name"] for entry in message.get("tool_calls", [])]
    except urllib.error.HTTPError as error:
        return provider_failure(case, f"provider HTTP {error.code}", expected_field="expect")
    except (urllib.error.URLError, TimeoutError) as error:
        return provider_failure(
            case, f"provider request failed ({type(error).__name__})", expected_field="expect")
    except json.JSONDecodeError:
        return provider_failure(case, "provider response was not valid JSON", expected_field="expect")
    except (KeyError, IndexError, TypeError):
        return provider_failure(
            case, "provider response did not contain a chat completion", expected_field="expect")
    return score_case(case, calls)


def run_inside(skill_path: Path, cases_path: Path, cards_path: Path, advisories_path: Path,
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
            for name in ("start", "continue") if suite in ("all", name)
        }
        print(json.dumps({"chat_model": model_name, "advice": advice, "summary": summaries}, sort_keys=True),
              file=sys.stderr, flush=True)
        failed |= any(not result["pass"] for result in results)
    return int(failed)


def load_decision_cases(path: Path) -> list[dict[str, object]]:
    cases = json.loads(path.read_text())
    ids = [case["id"] for case in cases]
    if not cases or len(ids) != len(set(ids)):
        raise ValueError("decision cases need unique ids")
    for case in cases:
        if not case["user"] or set(case["decision_expected"]) != {"independent_work", "continuation_context"}:
            raise ValueError(f"decision case {case['id']} needs a message and both expected answers")
        if any(type(value) is not bool for value in case["decision_expected"].values()):
            raise ValueError(f"decision case {case['id']} expected answers must be booleans")
        if any(message["role"] not in {"user", "assistant"} for message in case.get("recent_messages", [])):
            raise ValueError(f"decision case {case['id']} history has an unknown role")
    return [{"id": case["id"], "last_message": case["user"],
             "recent_messages": case.get("recent_messages", []),
             "expected": case["decision_expected"], "suite": case["suite"]} for case in cases]


def score_decision_case(case: dict[str, object], values: dict[str, float]) -> dict[str, object]:
    expected = cast(dict[str, bool], case["expected"])
    if any(not 0 <= value <= 1 for value in values.values()):
        raise ValueError(f"decision case {case['id']} returned a value outside 0–1")
    matches = {name: (values[name] >= 0.5) == expected[name] for name in expected}
    return {"case": case["id"], "expected": expected, "values": values, "matches": matches,
            "both_correct": all(matches.values())}


def decision_state(last_message: str, recent_messages: list[dict[str, str]]):
    from belle_agent.message_decisions import build_state, message_entry

    history = [message_entry("Belle" if entry["role"] == "assistant" else "sender", entry["content"])
               for entry in recent_messages]
    return build_state(channel="eval", bot_name="Belle", conversation_kind="one_to_one_dm",
                       recent_messages=history, last_message=message_entry("sender", last_message))


def run_decision_probe(cases_path: Path, models_path: Path, suite: str = "all") -> int:
    from belle_agent.message_decisions import (
        CONTINUATION_CONTEXT,
        INDEPENDENT_WORK,
        QUESTIONS,
    )
    from belle_agent.typed_decision import TypedDecisionError

    config = {key: os.environ.get(key, "") for key in DECISION_ENV_KEYS}
    if not all(config.values()):
        print("SKIP: DECISION_API_BASE and DECISION_API_KEY are required")
        return 0
    question_ids = (INDEPENDENT_WORK, CONTINUATION_CONTEXT)
    questions = {key: QUESTIONS[key] for key in question_ids}
    cases = [case for case in load_decision_cases(cases_path)
             if suite == "all" or case["suite"] == suite]
    models = load_decision_models(models_path)

    async def evaluate(client) -> list[dict[str, object]]:
        limit = asyncio.Semaphore(8)

        async def one(case: dict[str, object]) -> dict[str, object]:
            state = decision_state(case["last_message"], case["recent_messages"])
            async with limit:
                try:
                    answers = await client.decide(state, questions)
                    return score_decision_case(case, {key: float(answers[key].number) for key in question_ids})
                except (TypedDecisionError, TimeoutError, ValueError) as error:
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
                         "total": len(valid)} for key in question_ids}
        print(json.dumps({"model": model["model_name"], "adapter": client.name, "cases": len(cases),
                          "errors": len(cases) - len(valid), "questions": summary,
                          "both_correct": sum(bool(result["both_correct"]) for result in valid)}, sort_keys=True),
              file=sys.stderr)
        failed |= len(valid) != len(cases) or any(not result["both_correct"] for result in valid)
    return int(failed)


def pipeline_outcome(case: dict[str, object], values: dict[str, float],
                     runs: dict[str, dict[str, object]]) -> dict[str, object]:
    decision = score_decision_case({"id": case["id"], "expected": case["decision_expected"]}, values)
    decision_ok = decision["both_correct"]
    advised_ok, unadvised_ok = bool(runs["real"]["pass"]), bool(runs["chat_only"]["pass"])
    return {
        "case": case["id"], "suite": case["suite"], "expected_tool": case["expect"],
        "decision": decision, "runs": runs,
        "relationship": f"decision_{'correct' if decision_ok else 'wrong'}_chat_{'correct' if advised_ok else 'wrong'}",
        "comparison": ("both_correct" if advised_ok and unadvised_ok else
                       "with_only_correct" if advised_ok else
                       "without_only_correct" if unadvised_ok else "both_wrong"),
    }


def run_pipeline_probe(cases_path: Path, skill_path: Path, cards_path: Path,
                       decision_models_path: Path, chat_models_path: Path, workers: int,
                       suite: str = "all", max_rpm: int = 0) -> int:
    from belle_agent.message_decisions import CONTINUATION_CONTEXT, INDEPENDENT_WORK, QUESTIONS, format_advisory
    from belle_agent.typed_decision import Answer, TypedDecisionError

    config = {key: os.environ.get(key, "") for key in ENV_KEYS}
    questions = {key: QUESTIONS[key] for key in (INDEPENDENT_WORK, CONTINUATION_CONTEXT)}
    decision_models = load_decision_models(decision_models_path)
    chat_models = load_chat_models(chat_models_path)
    cases = [case for case in json.loads(cases_path.read_text())
             if suite == "all" or case["suite"] == suite]
    skill = skill_path.read_text()
    pacer = RequestPacer(max_rpm) if max_rpm else None

    async def decide_cases(client) -> list[dict[str, object]]:
        limit = asyncio.Semaphore(workers)

        async def one(case: dict[str, object]) -> dict[str, object]:
            async with limit:
                try:
                    if pacer:
                        await asyncio.to_thread(pacer.wait)
                    answers = await client.decide(
                        decision_state(case["user"], case.get("recent_messages", [])), questions)
                    values = {key: float(answers[key].number) for key in questions}
                    return {"values": values, "advisory": format_advisory(answers, questions)}
                except (TypedDecisionError, TimeoutError, TypeError, ValueError) as error:
                    return {"case": case["id"], "suite": case["suite"],
                            "error": f"decision {type(error).__name__}"}

        return await asyncio.gather(*(one(case) for case in cases))

    async def evaluate_chat(chat_model: str, decisions: list[dict[str, object]]) -> list[dict[str, object]]:
        limit = asyncio.Semaphore(workers)

        async def one(case: dict[str, object], decision: dict[str, object]) -> dict[str, object]:
            if "error" in decision:
                return decision
            async with limit:
                expected = cast(dict[str, bool], case["decision_expected"])
                synthetics = {
                    label: format_advisory({key: Answer(type="noul", value=value)
                                            for key, value in synthetic_values(expected, aligned).items()}, questions)
                    for label, aligned in (("synthetic_good", True), ("synthetic_bad", False))
                }
                advisories = {"chat_only": "", "real": cast(str, decision["advisory"]), **synthetics}

                def request(advisory: str) -> dict[str, object]:
                    if pacer:
                        pacer.wait()
                    return request_case(case, advisory, skill, config, cards_path, chat_model)

                results = await asyncio.gather(*(asyncio.to_thread(request, advisory)
                                                 for advisory in advisories.values()))
                if any("error" in result for result in results):
                    return {"case": case["id"], "suite": case["suite"], "error": "chat provider error"}
                return pipeline_outcome(case, cast(dict[str, float], decision["values"]),
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
                              "relationships": dict(Counter(result["relationship"] for result in valid)),
                              "comparisons": dict(Counter(result["comparison"] for result in valid)),
                              "runs": {name: suite_summary([result["runs"][name] for result in valid])
                                       for name in ("chat_only", "real", "synthetic_good", "synthetic_bad")}},
                             sort_keys=True), file=sys.stderr)
            failed |= len(valid) != len(cases) or any(not result["runs"]["real"]["pass"] for result in valid)
    return int(failed)


def host_main(args: argparse.Namespace) -> int:
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
        return run_container(
            args.decision_image, config, keys,
            [(Path(__file__), "/probe.py"), (args.source, "/src"), (args.cases, "/cases.json"),
             (args.cards, "/session_cards.json"), (args.skill, "/skill.md"),
             (args.decision_models, "/decision_models.jsonl"),
             (args.chat_models, "/chat_models.jsonl")],
            ["--inside-pipeline", "--cases", "/cases.json",
             "--cards", "/session_cards.json", "--skill", "/skill.md",
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
    skill_path = args.skill.resolve()
    config = probe_config(args.env_file, ENV_KEYS)
    missing = [key for key in ENV_KEYS if not config.get(key)]
    if missing:
        print("SKIP: model credentials unavailable; set " + ", ".join(missing))
        return 0
    if not skill_path.is_file():
        print(f"SKIP: session-work skill is not available at {skill_path}")
        return 0
    if not re.fullmatch(r"https?://[^\s]+", config["API_BASE"]):
        print("ERROR: API_BASE must be an HTTP(S) URL")
        return 2

    cases = json.loads(args.cases.read_text())
    advisories_path = advisory_file(
        [case for case in cases if args.suite == "all" or case["suite"] == args.suite], args.advice)
    try:
        return run_container(
            args.image,
            config,
            ENV_KEYS,
            [
                (Path(__file__), "/probe.py"), (args.cases, "/cases.json"),
                (args.cards, "/session_cards.json"), (skill_path, "/skill.md"),
                (advisories_path, "/advisories.json"), (args.chat_models, "/chat_models.jsonl"),
            ],
            [
                "--inside", "--skill", "/skill.md", "--cases", "/cases.json",
                "--cards", "/session_cards.json", "--advisories", "/advisories.json",
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
    parser.add_argument("--cards", type=Path, default=Path("src/belle_agent/scheduling/session_cards.json"))
    parser.add_argument("--advisories", type=Path, default=Path("/advisories.json"), help=argparse.SUPPRESS)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--image", default="docker.io/library/python:3.11-slim")
    parser.add_argument("--suite", choices=("all", "start", "continue"), default="all")
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
    return run_inside(args.skill, args.cases, args.cards, args.advisories,
                      args.workers, args.chat_models, args.suite, args.advice, args.max_rpm) if args.inside else host_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
