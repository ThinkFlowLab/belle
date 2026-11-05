"""Deterministic expectations for the session-work model probe."""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from belle_agent.message_decisions import (
    CONTINUATION_CONTEXT,
    INDEPENDENT_WORK,
    RECENT_MESSAGE_DEPTH,
    format_advisory,
)
from belle_agent.message_decisions import (
    QUESTIONS as PRODUCTION_QUESTIONS,
)
from belle_agent.typed_decision import Answer

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "contrib/evals/session-work"))
from probe import (
    RequestPacer,
    advisory_file,
    decision_client_from_env,
    load_chat_models,
    load_decision_cases,
    load_decision_models,
    pipeline_outcome,
    production_advisories,
    provider_failure,
    request_case,
    run_decision_probe,
    run_inside,
    run_pipeline_probe,
    score_case,
    score_decision_case,
    suite_summary,
    synthetic_values,
    tool_spec,
)

CASES = ROOT / "contrib/evals/session-work/cases.json"
SESSION_CARDS = ROOT / "src/belle_agent/scheduling/session_cards.json"
DECISION_MODELS = ROOT / "contrib/evals/models/decision_models.jsonl"
CHAT_MODELS = ROOT / "contrib/evals/models/chat_models.jsonl"


class SessionWorkEvalCases(unittest.TestCase):
    def test_request_pacer_spaces_starts(self):
        with (patch("host.time.monotonic", return_value=10.0),
              patch("host.time.sleep") as sleep):
            pacer = RequestPacer(30)
            for _ in range(3):
                pacer.wait()
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0, 4.0])

    def test_runner_refills_a_free_slot_before_the_slowest_request_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("skill", "cases", "cards", "advisories", "models")]
            paths[0].write_text("skill")
            paths[1].write_text(json.dumps([{"id": name, "suite": "start"} for name in "abc"]))
            paths[2].write_text("[]")
            paths[3].write_text("{}")
            paths[4].write_text('{"model_name":"test"}\n')
            third_started = threading.Event()

            def fake_request(case, *_args):
                if case["id"] == "a":
                    self.assertTrue(third_started.wait(timeout=3))
                elif case["id"] == "c":
                    third_started.set()
                return {"case": case["id"], "suite": "start", "pass": True}

            with (patch.dict(os.environ, {"API_BASE": "https://example.test", "API_KEY": "test"}),
                  patch("probe.request_case", side_effect=fake_request),
                  patch("probe.RequestPacer.wait"),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(run_inside(*paths[:4], workers=2, chat_models_path=paths[4], max_rpm=30), 0)
            self.assertTrue(third_started.is_set())

    def test_runner_reports_parallel_cases_as_they_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("skill", "cases", "cards", "advisories", "models")]
            paths[0].write_text("skill")
            paths[1].write_text(json.dumps([{"id": "a", "suite": "start"}, {"id": "b", "suite": "continue"}]))
            paths[2].write_text("[]")
            paths[3].write_text(json.dumps({"a": "", "b": ""}))
            paths[4].write_text('{"model_name":"test"}\n')
            barrier = threading.Barrier(2)

            def fake_request(case, *_args):
                barrier.wait(timeout=3)
                return {"case": case["id"], "suite": case["suite"], "pass": True}

            output, progress = io.StringIO(), io.StringIO()
            with (patch.dict(os.environ, {"API_BASE": "https://example.test", "API_KEY": "test"}),
                  patch("probe.request_case", side_effect=fake_request),
                  redirect_stdout(output), redirect_stderr(progress)):
                self.assertEqual(run_inside(*paths[:4], workers=2, chat_models_path=paths[4]), 0)
            self.assertEqual({json.loads(line)["case"] for line in output.getvalue().splitlines()}, {"a", "b"})
            self.assertIn("2/2 cases complete", progress.getvalue())

    def test_scoring_distinguishes_misses_detaches_lookups_and_wrong_tools(self):
        checks = [
            ("session_start", [], False, "missed_detach", "high"),
            ("session_continue", [], False, "missed_continuation", "high"),
            ("session_get", [], False, "missed_lookup", "medium"),
            ("no_session_tool", ["session_start"], False, "unnecessary_detach", "medium"),
            ("no_session_tool", ["session_start"], True, "unnecessary_detach", "high"),
            ("no_session_tool", ["session_list"], False, "unnecessary_lookup", "low"),
            ("session_continue", ["session_get"], False, "intermediate_lookup", "low"),
            ("session_continue", ["session_result"], False, "wrong_tool", "medium"),
            ("session_start", ["session_start", "session_start"], False, "wrong_tool", "medium"),
        ]
        for expected, calls, interactive, kind, severity in checks:
            with self.subTest(expected=expected, calls=calls, interactive=interactive):
                case = {"id": "test", "suite": "start", "expect": expected,
                        "interaction_required": interactive}
                result = score_case(case, calls)
                self.assertFalse(result["pass"])
                self.assertEqual(result["failure_kind"], kind)
                self.assertEqual(result["severity"], severity)
                self.assertIsNotNone(result["impact"])
        for expected, calls in [("session_start", ["session_start"]), ("no_session_tool", [])]:
            result = score_case({"id": "pass", "suite": "start", "expect": expected}, calls)
            self.assertTrue(result["pass"])
            self.assertIsNone(result["failure_kind"])
            self.assertIsNone(result["severity"])

    def test_provider_failures_are_reported_separately_from_behavior(self):
        case = {"id": "test", "suite": "continue", "expect": "session_continue"}
        result = provider_failure(case, "provider HTTP 503", expected_field="expect")
        summary = suite_summary([result, score_case(case, ["session_continue"])])
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failure_kinds"], {"provider_error": 1})
        self.assertEqual(summary["severities"], {})

    def test_runner_filters_suite_before_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("skill", "cases", "cards", "advisories", "models")]
            paths[0].write_text("skill")
            cases = [{"id": name, "suite": name, "expect": "session_start"} for name in ("start", "continue")]
            paths[1].write_text(json.dumps(cases))
            paths[2].write_text("[]")
            paths[3].write_text(json.dumps({"start": ""}))
            paths[4].write_text('{"model_name":"test"}\n')
            output, progress = io.StringIO(), io.StringIO()
            with (patch.dict(os.environ, {"API_BASE": "https://example.test", "API_KEY": "test"}),
                  patch("probe.request_case", side_effect=lambda case, *_: score_case(case, [])) as request,
                  redirect_stdout(output), redirect_stderr(progress)):
                self.assertEqual(run_inside(*paths[:4], workers=1, chat_models_path=paths[4], suite="start"), 1)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(json.loads(output.getvalue())["suite"], "start")
            summary = json.loads(progress.getvalue().splitlines()[-1])["summary"]
            self.assertEqual(set(summary), {"start"})
            self.assertEqual(summary["start"]["failure_kinds"], {"missed_detach": 1})

    def test_no_advice_omits_decision_block_from_chat_request(self):
        case = {"id": "test", "suite": "start", "user": "Say hello.", "tools": ["session_start"],
                "expect": "no_session_tool"}
        request_body = {}

        def fake_urlopen(request, **_kwargs):
            request_body.update(json.loads(request.data))
            return io.BytesIO(b'{"choices":[{"message":{"content":"Hello."}}]}')

        with patch("probe.urllib.request.urlopen", side_effect=fake_urlopen):
            result = request_case(case, "", "Use a session for sustained work.",
                                  {"API_BASE": "https://example.test", "API_KEY": "test"}, SESSION_CARDS, "test")
        self.assertTrue(result["pass"])
        self.assertEqual(request_body["model"], "test")
        self.assertEqual(request_body["messages"][-1]["content"], "User request: Say hello.")
        self.assertNotIn("noul", json.dumps(request_body))
        with patch("probe.production_advisories", side_effect=AssertionError("must not render advice")):
            path = advisory_file([case], advice="none")
        try:
            self.assertEqual(json.loads(path.read_text()), {})
        finally:
            path.unlink()

    def test_chat_model_list_accepts_custom_models_and_rejects_duplicates(self):
        self.assertIn("deepseek/deepseek-v4.1-flash", load_chat_models(CHAT_MODELS))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.jsonl"
            path.write_text('{"model_name":"custom/a"}\n{"model_name":"custom/b"}\n')
            self.assertEqual(load_chat_models(path), ["custom/a", "custom/b"])
            path.write_text('{"model_name":"custom/a"}\n' * 2)
            with self.assertRaisesRegex(ValueError, "must be unique"):
                load_chat_models(path)

    def test_chat_runner_labels_each_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / name for name in ("skill", "cases", "cards", "advisories", "models")]
            paths[0].write_text("skill")
            paths[1].write_text(json.dumps([{"id": "one", "suite": "start"}]))
            paths[2].write_text("[]")
            paths[3].write_text("{}")
            paths[4].write_text('{"model_name":"a"}\n{"model_name":"b"}\n')
            output = io.StringIO()
            with (patch.dict(os.environ, {"API_BASE": "https://example.test", "API_KEY": "test"}),
                  patch("probe.request_case", side_effect=lambda case, *_: {
                      "case": case["id"], "suite": case["suite"], "pass": True}),
                  redirect_stdout(output), redirect_stderr(io.StringIO())):
                self.assertEqual(run_inside(*paths[:4], workers=1, chat_models_path=paths[4]), 0)
            self.assertEqual([json.loads(line)["chat_model"] for line in output.getvalue().splitlines()], ["a", "b"])

    def test_canonical_cases_cover_both_decision_questions(self):
        cases = load_decision_cases(CASES)
        self.assertEqual(len(cases), 151)
        self.assertEqual(
            {tuple(case["expected"].values()) for case in cases},
            {(True, True), (True, False), (False, True), (False, False)},
        )
        self.assertTrue(any(case["recent_messages"] for case in cases))

    def test_decision_model_list_selects_an_adapter_per_model(self):
        models = load_decision_models(DECISION_MODELS)
        self.assertEqual([model["model_name"] for model in models], [
            "typesafe/jev-1.13", "typesafe/jev-1.14", "perplexity/pplx-decider-v1.2",
            "openai/gpt-6-luna-decisions", "scaledown/classify-1", "cloudflare/clef",
        ])
        self.assertEqual(models[-2], {"model_name": "scaledown/classify-1", "api": "openrouter_text_state"})
        with patch.dict(os.environ, {"DECISION_API_BASE": "https://openrouter.ai/api/v1",
                                          "DECISION_API_KEY": "test"}):
            self.assertEqual(decision_client_from_env(models[0]).name, "openrouter")
            self.assertEqual(decision_client_from_env(models[-2]).name, "openrouter_text_state")

    def test_decision_model_list_rejects_invalid_or_duplicate_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.jsonl"
            for text, error in [
                ('{"model_name":"a","api":"unknown"}\n', "unsupported decision api"),
                ('{"model_name":"a","api":"openrouter"}\n' * 2, "must be unique"),
                ('{"model_name":"a"}\n', "needs model_name and api"),
            ]:
                path.write_text(text)
                with self.subTest(text=text), self.assertRaisesRegex(ValueError, error):
                    load_decision_models(path)

    def test_decision_probe_labels_results_for_each_model(self):
        class Client:
            name = "openrouter"

            async def decide(self, _state, questions):
                return {key: Answer(type="noul", value=0.9) for key in questions}

        with tempfile.TemporaryDirectory() as directory:
            models = Path(directory) / "models.jsonl"
            cases = Path(directory) / "cases.jsonl"
            models.write_text('{"model_name":"a","api":"openrouter"}\n'
                              '{"model_name":"b","api":"openrouter"}\n')
            cases.write_text(json.dumps([{"id": "one", "suite": "start", "user": "Research this.",
                                          "decision_expected": {
                                              INDEPENDENT_WORK: True, CONTINUATION_CONTEXT: True}}]))
            output = io.StringIO()
            with (patch.dict(os.environ, {"DECISION_API_BASE": "https://example.test",
                                              "DECISION_API_KEY": "test"}),
                  patch("probe.decision_client_from_env", return_value=Client()),
                  redirect_stdout(output), redirect_stderr(io.StringIO())):
                self.assertEqual(run_decision_probe(cases, models), 0)
            self.assertEqual([json.loads(line)["model"] for line in output.getvalue().splitlines()], ["a", "b"])

    def test_pipeline_probe_labels_results_for_each_model(self):
        class Client:
            name = "openrouter"
            calls = 0

            async def decide(self, _state, questions):
                self.calls += 1
                return {key: Answer(type="noul", value=0.9) for key in questions}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models, chats, cases, skill = (root / name for name in (
                "models.jsonl", "chat_models.jsonl", "cases.json", "skill.md"))
            models.write_text('{"model_name":"a","api":"openrouter"}\n'
                              '{"model_name":"b","api":"openrouter"}\n')
            chats.write_text('{"model_name":"one"}\n{"model_name":"two"}\n')
            cases.write_text(json.dumps([{"id": "one", "suite": "start", "user": "Hello.",
                                          "expect": "no_session_tool", "tools": ["session_start"],
                                          "decision_expected": {
                                              INDEPENDENT_WORK: True, CONTINUATION_CONTEXT: False}}]))
            skill.write_text("Use sessions when needed.")
            output = io.StringIO()
            client = Client()
            with (patch("probe.decision_client_from_env", return_value=client),
                  patch("probe.request_case", side_effect=lambda case, *_: score_case(case, [])) as request,
                  redirect_stdout(output), redirect_stderr(io.StringIO())):
                self.assertEqual(run_pipeline_probe(cases, skill, SESSION_CARDS, models, chats, workers=1), 0)
            self.assertEqual(client.calls, 2)
            self.assertEqual(Counter(call.args[-1] for call in request.call_args_list), {"one": 8, "two": 8})
            self.assertEqual(sum(not call.args[1] for call in request.call_args_list), 4)
            self.assertEqual([(result["decision_model"], result["chat_model"])
                              for result in map(json.loads, output.getvalue().splitlines())],
                             [("a", "one"), ("a", "two"), ("b", "one"), ("b", "two")])

    def test_decision_scoring_keeps_questions_independent(self):
        case = {"id": "example", "expected": {INDEPENDENT_WORK: True, CONTINUATION_CONTEXT: False}}
        result = score_decision_case(case, {INDEPENDENT_WORK: 0.9, CONTINUATION_CONTEXT: 0.8})
        self.assertEqual(result["matches"], {INDEPENDENT_WORK: True, CONTINUATION_CONTEXT: False})
        self.assertFalse(result["both_correct"])

    def test_pipeline_pairs_decision_and_chat_outcomes(self):
        case = {"id": "example", "suite": "continue", "expect": "session_continue",
                "decision_expected": {INDEPENDENT_WORK: True, CONTINUATION_CONTEXT: True}}
        good = score_case(case, ["session_continue"])
        bad = score_case(case, [])
        result = pipeline_outcome(case, {INDEPENDENT_WORK: 0.2, CONTINUATION_CONTEXT: 0.9}, {
            "chat_only": bad, "real": good, "synthetic_good": good, "synthetic_bad": bad,
        })
        self.assertEqual(result["relationship"], "decision_wrong_chat_correct")
        self.assertEqual(result["comparison"], "with_only_correct")
        self.assertEqual(set(result["runs"]), {"chat_only", "real", "synthetic_good", "synthetic_bad"})

    def test_canonical_cases_have_labels_and_valid_session_ids(self):
        cases = json.loads(CASES.read_text())
        self.assertEqual(len(cases), 151)
        for case in cases:
            self.assertEqual(set(case["decision_expected"]), {INDEPENDENT_WORK, CONTINUATION_CONTEXT})
            self.assertTrue(all(type(value) is bool for value in case["decision_expected"].values()))
            if case["expect"] == "session_continue":
                visible = case["user"] + " ".join(message["content"] for message in case.get("recent_messages", []))
                self.assertRegex(visible, r"\bses-[a-z0-9]{10}\b")

    def test_probe_cases_cover_session_guidance_boundaries(self):
        cases = {case["id"]: case for case in json.loads(CASES.read_text())}

        self.assertEqual(cases["short_request_positive_hint"]["expect"], "no_session_tool")
        self.assertEqual(cases["long_independent_negative_hint"]["expect"], "session_start")
        self.assertEqual(cases["long_independent_sender_continues"]["expect"], "session_start")
        self.assertEqual(cases["continuation_with_required_context"]["expect"], "session_continue")
        self.assertNotIn("session_continue", cases["continuation_unavailable"]["tools"])
        self.assertEqual(cases["continuation_unavailable"]["expect"], "no_session_tool")
        self.assertTrue(cases["short_request_positive_hint"]["decision_expected"]["independent_work"])
        self.assertTrue(cases["long_independent_negative_hint"]["decision_expected"]["independent_work"])

    def test_case_schema_and_tool_expectations(self):
        raw_cases = json.loads(CASES.read_text())
        cards = {card["name"] for card in json.loads(SESSION_CARDS.read_text())}
        self.assertEqual(len({case["id"] for case in raw_cases}), len(raw_cases))
        for case in raw_cases:
            self.assertEqual(
                set(case),
                {"id", "suite", "user", "decision_expected", "tools", "expect", "purpose"}
                | ({"interaction_required"} if "interaction_required" in case else set())
                | ({"recent_messages"} if "recent_messages" in case else set())
                | ({"history_profile"} if "history_profile" in case else set())
                | ({"relevant_history_index"} if "relevant_history_index" in case else set()),
            )
            self.assertIn(case["suite"], {"start", "continue"})
            if "interaction_required" in case:
                self.assertIs(case["interaction_required"], True)
                self.assertEqual(case["expect"], "no_session_tool")
            self.assertTrue(case["id"] and case["user"] and case["purpose"])
            self.assertEqual(set(case["decision_expected"]), {INDEPENDENT_WORK, CONTINUATION_CONTEXT})
            self.assertTrue(all(type(value) is bool for value in case["decision_expected"].values()))
            self.assertTrue(set(case["tools"]) <= cards)
            self.assertIn(case["expect"], {"no_session_tool", *cards})
            for message in case.get("recent_messages", []):
                self.assertEqual(set(message), {"role", "content"})
                self.assertIn(message["role"], {"user", "assistant"})
                self.assertTrue(message["content"])
            if case["expect"] != "no_session_tool":
                self.assertIn(case["expect"], case["tools"])

    def test_cases_cover_production_history_window_and_relevance_shapes(self):
        cases = json.loads(CASES.read_text())
        histories = [case.get("recent_messages", []) for case in cases]
        profiled = [case for case in cases if "history_profile" in case]
        profiles = {case["history_profile"] for case in profiled}

        self.assertGreaterEqual(len(cases), 100)
        self.assertTrue({"fresh", "irrelevant", "relevant", "sparse_relevant", "session_lifecycle"} <= profiles)
        self.assertTrue(any(not history for history in histories))
        self.assertTrue({1, 2, 3, 6, 11, RECENT_MESSAGE_DEPTH} <= {len(h) for h in histories})
        self.assertTrue(all(len(history) <= RECENT_MESSAGE_DEPTH for history in histories))
        self.assertTrue(all(
            {message["role"] for message in case.get("recent_messages", [])} <= {"user", "assistant"}
            for case in profiled
        ))
        sparse = [case for case in profiled if case["history_profile"] == "sparse_relevant"]
        self.assertTrue(all(len(case["recent_messages"]) == RECENT_MESSAGE_DEPTH for case in sparse))
        self.assertGreater(len({case["relevant_history_index"] for case in sparse}), 1)
        self.assertTrue(any(
            any(message["role"] == "assistant" for message in case.get("recent_messages", []))
            for case in profiled
        ))

    def test_hints_use_forwarded_noul_shape(self):
        case = {"id": "example", "decision_expected": {
            INDEPENDENT_WORK: True, CONTINUATION_CONTEXT: False}}
        advisory = production_advisories([case])["example"]
        payload = json.loads(advisory.rsplit("\n\n", 1)[1])
        answers = {key: value["answer"] for key, value in payload.items()}

        self.assertEqual(answers[INDEPENDENT_WORK], {"type": "noul", "value": 0.9})
        self.assertEqual(answers[CONTINUATION_CONTEXT], {"type": "noul", "value": 0.1})
        opposed = production_advisories([case], "synthetic_bad")["example"]
        opposed_answers = json.loads(opposed.rsplit("\n\n", 1)[1])
        self.assertEqual(opposed_answers[INDEPENDENT_WORK]["answer"]["value"], 0.1)
        self.assertEqual(opposed_answers[CONTINUATION_CONTEXT]["answer"]["value"], 0.9)
        self.assertTrue(all("confidence" not in answer for answer in answers.values()))

    def test_advisory_matches_production_formatter(self):
        values = {INDEPENDENT_WORK: 0.9, CONTINUATION_CONTEXT: 0.1}
        questions = {
            key: PRODUCTION_QUESTIONS[key]
            for key in values
        }
        answers = {key: Answer(type="noul", value=value) for key, value in values.items()}

        case = {"id": "example", "decision_expected": {
            INDEPENDENT_WORK: True, CONTINUATION_CONTEXT: False}}
        self.assertEqual(production_advisories([case])["example"], format_advisory(answers, questions))
        self.assertEqual(synthetic_values(case["decision_expected"], False), {
            INDEPENDENT_WORK: 0.1, CONTINUATION_CONTEXT: 0.9})

    def test_tool_cases_use_the_production_session_cards(self):
        cards = {card["name"]: card for card in json.loads(SESSION_CARDS.read_text())}
        cases = {case["id"]: case for case in json.loads(CASES.read_text())}
        exposed = tool_spec(["session_start"], SESSION_CARDS)[0]["function"]

        self.assertIn("session_start", cards)
        self.assertIn("session_continue", cards)
        self.assertEqual(cases["continuation_unavailable"]["tools"], ["session_start"])
        self.assertEqual(exposed["description"], cards["session_start"]["description"])
        self.assertEqual(exposed["parameters"], cards["session_start"]["input_params"])
