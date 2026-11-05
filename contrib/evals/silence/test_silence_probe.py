"""Deterministic expectations for the silence model probe."""

import argparse
import importlib.util
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
from typing import ClassVar
from unittest.mock import patch

from belle_agent import message_decisions as md
from belle_agent.message_decisions import RECENT_MESSAGE_DEPTH, format_advisory
from belle_agent.platforms.slack.post import POST_MESSAGE, _post_tool_definition
from belle_agent.silence import post_tool_instruction
from belle_agent.typed_decision import Answer

ROOT = Path(__file__).parents[3]
HERE = ROOT / "contrib/evals/silence"
# Load under a unique name so this probe can coexist with other eval probes in one pytest run.
_spec = importlib.util.spec_from_file_location("silence_probe", HERE / "probe.py")
probe = importlib.util.module_from_spec(_spec)
sys.modules["silence_probe"] = probe
_spec.loader.exec_module(probe)

CASES = HERE / "cases.json"
SKILL = ROOT / "src/belle_agent/skills/group-chat-participation/SKILL.md"
_CARDS_DIR = tempfile.TemporaryDirectory()
CARDS = Path(_CARDS_DIR.name) / "post_cards.json"
CARDS.write_text(json.dumps(probe.production_cards()))
INSTRUCTION = post_tool_instruction(POST_MESSAGE)
POST = {"name": POST_MESSAGE, "args": {"text": "Here you go."}}
MODELS = ROOT / "contrib/evals/models"
DECISION_MODELS = MODELS / "decision_models.jsonl"
CHAT_MODELS = MODELS / "chat_models.jsonl"
QUESTION_IDS = (md.AUDIENCE, md.ALREADY_ANSWERED, md.RESPONSE_REQUESTED, md.NEEDS_WORK, md.CLAIM)
AUDIENCES = {md.AUDIENCE_NOBODY, md.AUDIENCE_ROOM, md.AUDIENCE_PERSON, md.AUDIENCE_BOT}
KINDS = {md.KIND_DIRECT, md.KIND_GROUP, md.KIND_PUBLIC, md.KIND_PRIVATE}
CONFIG = {"API_BASE": "https://example.test", "API_KEY": "test"}


def case(expect="silence", **extra):
    labels = {md.AUDIENCE: md.AUDIENCE_NOBODY, md.ALREADY_ANSWERED: False, md.RESPONSE_REQUESTED: False,
              md.NEEDS_WORK: False, md.CLAIM: False}
    return {"id": "test", "suite": expect, "user": "hello", "conversation_kind": md.KIND_GROUP,
            "decision_expected": labels, **extra}


class SilenceEvalProbe(unittest.TestCase):
    def setUp(self):
        probe.use_post_instruction(None)

    def test_request_pacer_spaces_starts(self):
        with (patch("host.time.monotonic", return_value=10.0),
              patch("host.time.sleep") as sleep):
            pacer = probe.RequestPacer(30)
            for _ in range(3):
                pacer.wait()
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0, 4.0])

    def run_chat(self, directory, cases, advisories=None, models='{"model_name":"test"}\n', **kwargs):
        paths = [Path(directory) / name for name in ("skill", "cases", "advisories", "models")]
        paths[0].write_text("skill")
        paths[1].write_text(json.dumps(cases))
        paths[2].write_text(json.dumps(advisories if advisories is not None else {}))
        paths[3].write_text(models)
        return probe.run_inside(paths[0], paths[1], CARDS, paths[2], chat_models_path=paths[3], **kwargs)

    def test_runner_refills_a_free_slot_before_the_slowest_request_finishes(self):
        third_started = threading.Event()

        def fake_request(case, *_args):
            if case["id"] == "a":
                self.assertTrue(third_started.wait(timeout=3))
            elif case["id"] == "c":
                third_started.set()
            return {"case": case["id"], "suite": "silence", "pass": True}

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, CONFIG), patch("silence_probe.request_case", side_effect=fake_request), \
                patch("silence_probe.RequestPacer.wait"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_chat(directory, [case(id=name) for name in "abc"],
                                           workers=2, max_rpm=30), 0)
        self.assertTrue(third_started.is_set())

    def test_runner_reports_parallel_cases_as_they_finish(self):
        barrier = threading.Barrier(2)

        def fake_request(case, *_args):
            barrier.wait(timeout=3)
            return {"case": case["id"], "suite": case["suite"], "pass": True}

        output, progress = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, CONFIG), \
                patch("silence_probe.request_case", side_effect=fake_request), \
                redirect_stdout(output), redirect_stderr(progress):
            self.run_chat(directory, [case(id="a"), case("speak", id="b")], workers=2)
        self.assertEqual({json.loads(line)["case"] for line in output.getvalue().splitlines()}, {"a", "b"})
        self.assertIn("2/2 cases complete", progress.getvalue())

    def test_runner_filters_suite_before_requests(self):
        cases = [case(name, id=name) for name in ("silence", "speak")]
        output, progress = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, CONFIG), \
                patch("silence_probe.request_case", side_effect=lambda c, *_: probe.score_case(c, ["post_message"])) as request, \
                redirect_stdout(output), redirect_stderr(progress):
            self.assertEqual(self.run_chat(directory, cases, workers=1, suite="silence"), 1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(json.loads(output.getvalue())["suite"], "silence")
        summary = json.loads(progress.getvalue().splitlines()[-1])["summary"]
        self.assertEqual(set(summary), {"silence"})
        self.assertEqual(summary["silence"]["failure_kinds"], {"missed_silence": 1})

    def test_chat_runner_labels_each_model_and_skips_without_credentials(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, CONFIG), \
                patch("silence_probe.request_case", side_effect=lambda c, *_: {
                    "case": c["id"], "suite": c["suite"], "pass": True}), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.run_chat(directory, [case("speak", id="one")],
                          models='{"model_name":"a"}\n{"model_name":"b"}\n', workers=1)
        self.assertEqual([json.loads(line)["chat_model"] for line in output.getvalue().splitlines()], ["a", "b"])
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"API_BASE": "", "API_KEY": ""}), redirect_stdout(output):
            self.assertEqual(self.run_chat(directory, [], workers=1), 0)
        self.assertIn("SKIP", output.getvalue())

    def test_scoring_matrix(self):
        for expect in ("silence", "speak"):
            with self.subTest(expect=expect, calls="none"):
                result = probe.score_case(case(expect), [])
                if expect == "silence":
                    self.assertTrue(result["pass"])
                    self.assertEqual(result["observed"], "silence")
                else:
                    self.assertEqual((result["failure_kind"], result["severity"]), ("missed_reply", "high"))
        for calls in ([POST], [POST_MESSAGE], ["other", POST], [{"name": POST_MESSAGE, "args": {"text": " "}}, POST]):
            with self.subTest(calls=calls):
                self.assertTrue(probe.score_case(case("speak"), calls)["pass"])
                result = probe.score_case(case("silence"), calls)
                self.assertEqual((result["pass"], result["failure_kind"], result["severity"]),
                                 (False, "missed_silence", "medium"))
        for text in ("", "   \n", None):
            empty = [{"name": POST_MESSAGE, "args": {"text": text}}]
            result = probe.score_case(case("speak"), empty)
            self.assertEqual((result["failure_kind"], result["severity"]), ("empty_post", "medium"))
            self.assertEqual(probe.score_case(case("silence"), empty)["failure_kind"], "missed_silence")
        no_args = probe.score_case(case("speak"), [{"name": POST_MESSAGE, "args": {}}])
        self.assertEqual(no_args["failure_kind"], "empty_post")
        # Only post_message is speech; another tool is neither a post nor a missed silence.
        self.assertTrue(probe.score_case(case("silence"), ["react_to_message"])["pass"])
        self.assertEqual(probe.score_case(case("speak"), ["react_to_message"])["failure_kind"], "missed_reply")

    def test_final_text_is_withheld_and_never_graded(self):
        for text in ("NO_REPLY", "Sure, here you go.", "", None, [{"type": "text", "text": "Hi"}]):
            with self.subTest(text=text):
                self.assertTrue(probe.score_case(case("silence"), [], text)["pass"])
                self.assertEqual(probe.score_case(case("speak"), [], text)["failure_kind"], "missed_reply")
                self.assertTrue(probe.score_case(case("speak"), [POST], text)["pass"])
        result = probe.score_case(case("silence"), [], "x" * 500)
        self.assertEqual(len(result["final_text_withheld"]), probe.REPLY_LIMIT)

    def test_missed_silence_is_high_when_it_interrupts_another_person_or_repeats_an_answer(self):
        for key, value in ((md.AUDIENCE, md.AUDIENCE_PERSON), (md.ALREADY_ANSWERED, True)):
            interrupting = case("silence")
            interrupting["decision_expected"][key] = value
            self.assertEqual(probe.score_case(interrupting, [POST])["severity"], "high")

    def test_provider_failures_are_reported_separately_from_behavior(self):
        spoken = case("speak")
        failure = probe.provider_failure(spoken, "provider HTTP 503")
        summary = probe.suite_summary([failure, probe.score_case(spoken, [POST]), probe.score_case(spoken, [])])
        self.assertEqual((summary["total"], summary["passed"]), (3, 1))
        self.assertEqual(summary["failure_kinds"], {"provider_error": 1, "missed_reply": 1})
        self.assertEqual(summary["severities"], {"high": 1})
        self.assertEqual(failure["impact"], "unscored")

    def completion(self, calls=(), content=None):
        message = {"content": content, "tool_calls": [
            {"id": str(i), "type": "function", "function": {"name": name, "arguments": arguments}}
            for i, (name, arguments) in enumerate(calls)]}
        return io.BytesIO(json.dumps({"choices": [{"message": message}]}).encode())

    def test_request_offers_only_post_message_and_grades_tool_calls(self):
        spoken = {**case("speak"), "sender": "Dana"}
        body = {}

        def fake_urlopen(response):
            def urlopen(request, **_kwargs):
                body.update(json.loads(request.data))
                return response()
            return urlopen

        said = lambda: self.completion([(POST_MESSAGE, json.dumps({"text": "Done."}))], content="NO_REPLY")
        with patch("silence_probe.urllib.request.urlopen", side_effect=fake_urlopen(said)):
            result = probe.request_case(spoken, "", "SKILL TEXT", CONFIG, CARDS, "model-x")
        self.assertTrue(result["pass"])
        self.assertEqual(result["post_text"], "Done.")
        self.assertEqual(body["model"], "model-x")
        self.assertEqual(body["tool_choice"], "auto")
        (tool,) = body["tools"]
        self.assertEqual(tool["function"]["name"], POST_MESSAGE)
        card = _post_tool_definition()
        self.assertEqual(tool["function"]["description"], card["description"])
        self.assertEqual(tool["function"]["parameters"], card["input_params"])
        self.assertEqual(body["messages"][-1]["content"], "Latest message:\nDana: hello")
        # Prose with no tool call is withheld in production, so it is silence here.
        prose = lambda: self.completion(content="Sure, here is the answer.")
        with patch("silence_probe.urllib.request.urlopen", side_effect=fake_urlopen(prose)):
            self.assertEqual(probe.request_case(spoken, "", "s", CONFIG, CARDS, "m")["failure_kind"], "missed_reply")
            self.assertTrue(probe.request_case(case("silence"), "", "s", CONFIG, CARDS, "m")["pass"])
        for arguments, kind in (("{not json", "empty_post"), ("", "empty_post"), ("[]", "empty_post"),
                                ('{"text": ""}', "empty_post")):
            with self.subTest(arguments=arguments), patch(
                    "silence_probe.urllib.request.urlopen",
                    side_effect=fake_urlopen(lambda args=arguments: self.completion([(POST_MESSAGE, args)]))):
                self.assertEqual(probe.request_case(spoken, "", "s", CONFIG, CARDS, "m")["failure_kind"], kind)
        with patch("silence_probe.urllib.request.urlopen", return_value=io.BytesIO(b"{}")):
            self.assertEqual(probe.request_case(spoken, "", "s", CONFIG, CARDS, "m")["failure_kind"], "provider_error")
        with patch("silence_probe.urllib.request.urlopen", return_value=io.BytesIO(b"<html>nope</html>")):
            failure = probe.request_case(spoken, "", "s", CONFIG, CARDS, "m")
            self.assertEqual(failure["failure_kind"], "provider_error")
            self.assertEqual(failure["error"], "provider response was not valid JSON")

    def test_system_prompt_carries_skill_and_the_production_post_instruction(self):
        skill = SKILL.read_text()
        prompt = probe.chat_messages(case(), "", skill)[0]["content"]
        self.assertIn(skill, prompt)
        self.assertIn(INSTRUCTION, prompt)
        self.assertIn("This turn permits silence", prompt)
        self.assertIn("group direct message", prompt)
        self.assertNotIn("A post is required", prompt)
        self.assertNotIn("Silence is not available", prompt)
        self.assertNotIn("NO_REPLY", prompt.replace(skill, ""))
        named = probe.chat_messages(case(bot_name="Ember"), "", skill)[0]["content"]
        self.assertIn("You are Ember", named)

    def test_history_and_advice_precede_the_latest_message(self):
        history = [{"role": "user", "content": "Is it up?", "author": "Dana"},
                   {"role": "assistant", "content": "Yes."}]
        messages = probe.chat_messages(case(recent_messages=history, sender="Sam"), "ADVICE", "skill")
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], "Dana: Is it up?")
        self.assertEqual(messages[2]["content"], "Yes.")
        self.assertEqual(messages[3]["content"], "ADVICE\n\nLatest message:\nSam: hello")
        self.assertNotIn("ADVICE", json.dumps(probe.chat_messages(case(), "", "skill")))

    def test_host_instruction_replaces_the_import(self):
        probe.use_post_instruction("HOST TEXT")
        self.assertIn("HOST TEXT", probe.chat_messages(case(), "", "skill")[0]["content"])
        probe.use_post_instruction(None)
        self.assertEqual(probe.post_instruction(), INSTRUCTION)

    def test_production_card_is_the_post_definition(self):
        self.assertEqual(probe.production_cards(), [_post_tool_definition()])
        self.assertEqual(probe.POST_TOOL, POST_MESSAGE)
        self.assertEqual(probe.tool_spec(CARDS)[0]["function"]["name"], POST_MESSAGE)

    def test_chat_model_list_accepts_custom_models_and_rejects_duplicates(self):
        self.assertIn("deepseek/deepseek-v4.1-flash", probe.load_chat_models(CHAT_MODELS))
        self.assertEqual(CHAT_MODELS.resolve(), (ROOT / "contrib/evals/models/chat_models.jsonl").resolve())
        self.assertEqual(DECISION_MODELS.resolve(),
                         (ROOT / "contrib/evals/models/decision_models.jsonl").resolve())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.jsonl"
            path.write_text('{"model_name":"custom/a"}\n' * 2)
            with self.assertRaisesRegex(ValueError, "must be unique"):
                probe.load_chat_models(path)

    def test_host_skips_without_credentials_or_skill(self):
        def args(**kwargs):
            values = {"skill": SKILL, "cards": None, "env_file": Path("/nonexistent.env"), "cases": CASES,
                      "source": ROOT / "src", "pipeline_probe": False, "decision_probe": False,
                      "suite": "all", "advice": "none"}
            return argparse.Namespace(**{**values, **kwargs})

        blank = {key: "" for key in probe.ENV_KEYS + probe.DECISION_ENV_KEYS}
        for kwargs in ({}, {"decision_probe": True}, {"pipeline_probe": True}):
            output = io.StringIO()
            with patch.dict(os.environ, blank), redirect_stdout(output), \
                    patch("silence_probe.run_container") as container:
                self.assertEqual(probe.host_main(args(**kwargs)), 0)
            self.assertIn("SKIP", output.getvalue())
            container.assert_not_called()
        for kwargs in ({"skill": Path("/nonexistent/SKILL.md")},
                       {"skill": Path("/nonexistent/SKILL.md"), "pipeline_probe": True}):
            output = io.StringIO()
            with patch.dict(os.environ, {**blank, **CONFIG, "DECISION_API_BASE": "https://x.test", "DECISION_API_KEY": "k"}), \
                    redirect_stdout(output), patch("silence_probe.run_container") as container:
                self.assertEqual(probe.host_main(args(**kwargs)), 0)
            self.assertIn("SKIP: group-chat-participation skill", output.getvalue())
            container.assert_not_called()

    def test_host_refuses_a_cards_file_without_a_post_message_card(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cards.json"
            for text in ("[]", "not json", json.dumps([{"name": POST_MESSAGE}])):
                path.write_text(text)
                output = io.StringIO()
                with redirect_stdout(output), patch("silence_probe.run_container") as container:
                    self.assertEqual(probe.host_main(argparse.Namespace(cards=path, decision_probe=False)), 2)
                self.assertIn(f"--cards {path}", output.getvalue())
                container.assert_not_called()

    def chat_arguments(self, **kwargs):
        values = {
            "skill": SKILL, "cards": None, "env_file": Path("/nonexistent.env"), "cases": CASES,
            "source": ROOT / "src", "pipeline_probe": False, "decision_probe": False, "suite": "silence",
            "advice": "synthetic_good", "image": "img", "chat_models": CHAT_MODELS, "workers": 2, "max_rpm": 0,
        }
        return argparse.Namespace(**{**values, **kwargs})

    def mounted_cards(self, arguments):
        seen = {}

        def container(image, config, keys, mounts, command, *extra):
            (source,) = (source for source, target in mounts if target == "/post_cards.json")
            seen["path"], seen["cards"] = source, json.loads(source.read_text())
            return 0

        with patch.dict(os.environ, CONFIG), patch("silence_probe.run_container", side_effect=container):
            self.assertEqual(probe.host_main(arguments), 0)
        return seen["path"], seen["cards"]

    def test_chat_container_gets_the_code_card_by_default_and_a_custom_card_on_request(self):
        path, cards = self.mounted_cards(self.chat_arguments())
        self.assertEqual(cards, probe.production_cards())
        self.assertFalse(path.exists(), "the generated card file should be removed after the run")
        custom = [{**probe.production_cards()[0], "description": "A candidate description."}]
        with tempfile.TemporaryDirectory() as directory:
            given = Path(directory) / "custom.json"
            given.write_text(json.dumps(custom))
            path, cards = self.mounted_cards(self.chat_arguments(cards=given))
            self.assertEqual((path, cards), (given.resolve(), custom))
            self.assertTrue(given.exists())

    def test_chat_container_mounts_the_skill_and_cards_and_bakes_in_the_instruction(self):
        with patch.dict(os.environ, CONFIG), patch("silence_probe.run_container", return_value=0) as container:
            self.assertEqual(probe.host_main(self.chat_arguments()), 0)
        mounts = {target for _, target in container.call_args.args[3]}
        self.assertTrue({"/probe.py", "/skill.md", "/post_cards.json", "/cases.json", "/advisories.json"} <= mounts)
        self.assertNotIn("/silence.py", mounts)
        command = container.call_args.args[4]
        self.assertNotIn("--silence", command)
        self.assertEqual(command[command.index("--post-instruction") + 1], INSTRUCTION)


class SilenceEvalCases(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads(CASES.read_text())

    def test_schema_and_unique_ids(self):
        required = {"id", "user", "conversation_kind", "decision_expected", "suite"}
        optional = {"recent_messages", "bot_name", "sender"}
        self.assertEqual(len(self.cases), 130)
        self.assertEqual(len({c["id"] for c in self.cases}), len(self.cases))
        for c in self.cases:
            with self.subTest(case=c["id"]):
                self.assertTrue(required <= set(c) <= required | optional)
                self.assertTrue(c["id"] and c["user"].strip())
                self.assertIn(c["conversation_kind"], KINDS)
                self.assertIn(c["suite"], {"silence", "speak"})
                self.assertNotIn("expect", c)
                labels = c["decision_expected"]
                self.assertEqual(set(labels), set(QUESTION_IDS))
                self.assertIn(labels[md.AUDIENCE], AUDIENCES)
                self.assertTrue(all(type(labels[key]) is bool for key in QUESTION_IDS[1:]))
                if "bot_name" in c:
                    self.assertTrue(c["bot_name"].strip())
                for message in c.get("recent_messages", []):
                    self.assertTrue({"role", "content"} <= set(message) <= {"role", "content", "author"})
                    self.assertIn(message["role"], {"user", "assistant"})
                    self.assertTrue(message["content"].strip())
                    if message["role"] == "assistant":
                        self.assertNotIn("author", message)
                if c["conversation_kind"] == md.KIND_DIRECT:
                    self.assertNotIn("sender", c)
                else:
                    self.assertTrue(c.get("sender"))

    def test_both_suites_have_depth(self):
        counts = Counter(c["suite"] for c in self.cases)
        self.assertGreaterEqual(counts["silence"], 40)
        self.assertGreaterEqual(counts["speak"], 40)
        self.assertTrue(any(c.get("bot_name", "Belle") != "Belle" for c in self.cases))

    def test_cases_cover_kinds_audiences_and_labels(self):
        self.assertEqual({c["conversation_kind"] for c in self.cases}, KINDS)
        self.assertEqual({c["decision_expected"][md.AUDIENCE] for c in self.cases}, AUDIENCES)
        for key in QUESTION_IDS[1:]:
            self.assertEqual({c["decision_expected"][key] for c in self.cases}, {True, False}, key)
        labels = [c["decision_expected"] for c in self.cases]
        # A silence-expected case for each reason the skill gives, and a speak case for each.
        self.assertTrue(any(c["suite"] == "silence" and c["decision_expected"][md.AUDIENCE] == md.AUDIENCE_PERSON for c in self.cases))
        self.assertTrue(any(c["suite"] == "silence" and d[md.ALREADY_ANSWERED] for c, d in zip(self.cases, labels)))
        self.assertTrue(any(c["suite"] == "silence" and d[md.NEEDS_WORK] and d[md.AUDIENCE] == md.AUDIENCE_BOT
                            for c, d in zip(self.cases, labels)))
        self.assertTrue(any(c["suite"] == "speak" and d[md.CLAIM] for c, d in zip(self.cases, labels)))
        self.assertTrue(any(c["suite"] == "speak" and d[md.AUDIENCE] == md.AUDIENCE_BOT for c, d in zip(self.cases, labels)))
        self.assertTrue(any(c["suite"] == "silence" and d[md.CLAIM] for c, d in zip(self.cases, labels)))

    def test_histories_cover_zero_to_the_production_window(self):
        lengths = {len(c.get("recent_messages", [])) for c in self.cases}
        self.assertTrue({0, 1, 2, 3, 6, 11, RECENT_MESSAGE_DEPTH} <= lengths)
        self.assertTrue(max(lengths) <= RECENT_MESSAGE_DEPTH)
        self.assertTrue(any(m["role"] == "assistant" for c in self.cases for m in c.get("recent_messages", [])))
        for kind in (md.KIND_GROUP, md.KIND_PUBLIC):
            self.assertTrue(any(c["conversation_kind"] == kind and c.get("recent_messages") for c in self.cases))

    def test_decision_loader_accepts_every_case_and_rejects_bad_labels(self):
        loaded = probe.load_decision_cases(CASES)
        self.assertEqual(len(loaded), len(self.cases))
        self.assertEqual({c["conversation_kind"] for c in loaded}, KINDS)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            for mutate, error in [
                (lambda c: c["decision_expected"].__setitem__(md.AUDIENCE, "everyone"), "audience must be one of"),
                (lambda c: c["decision_expected"].__setitem__(md.CLAIM, 1), "noul answers must be booleans"),
                (lambda c: c["decision_expected"].pop(md.CLAIM), "all five expected answers"),
                (lambda c: c.__setitem__("conversation_kind", "dm"), "conversation_kind must be one of"),
                (lambda c: c.__setitem__("suite", "other"), "suite must be one of"),
                (lambda c: c.__setitem__("user", " "), "needs a message"),
                (lambda c: c.__setitem__("recent_messages", [{"role": "tool", "content": "x"}]), "unknown role"),
            ]:
                broken = json.loads(CASES.read_text())[:1]
                mutate(broken[0])
                path.write_text(json.dumps(broken))
                with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                    probe.load_decision_cases(path)

    def test_validate_cases_refuses_duplicate_ids_and_is_run_on_the_host(self):
        with self.assertRaisesRegex(ValueError, "unique ids"):
            probe.validate_cases(self.cases[:1] * 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps([{**self.cases[0], "suite": "other"}]))
            arguments = argparse.Namespace(
                skill=SKILL, cards=None, env_file=Path("/nonexistent.env"),
                cases=path, source=ROOT / "src", pipeline_probe=False, decision_probe=False,
                suite="all", advice="none", image="img", chat_models=CHAT_MODELS, workers=1, max_rpm=0)
            with patch.dict(os.environ, CONFIG), patch("silence_probe.run_container") as container, \
                    self.assertRaisesRegex(ValueError, "suite"):
                probe.host_main(arguments)
            container.assert_not_called()


class ProductionImports(unittest.TestCase):
    def test_decision_questions_are_the_production_definitions(self):
        self.assertEqual(probe.question_ids(), QUESTION_IDS)
        questions = probe.production_questions()
        self.assertEqual(set(questions), set(QUESTION_IDS))
        for key, question in questions.items():
            self.assertIs(question, md.QUESTIONS[key])
        self.assertEqual(questions[md.AUDIENCE].type, "choice")
        self.assertEqual(set(questions[md.AUDIENCE].criteria), AUDIENCES)
        self.assertTrue(all(questions[key].type == "noul" for key in QUESTION_IDS[1:]))

    def test_probe_tables_match_production_vocabulary(self):
        self.assertEqual(set(probe.KIND_LABELS), KINDS)
        self.assertEqual(set(probe.BAD_AUDIENCE) | set(probe.BAD_AUDIENCE.values()), AUDIENCES)
        self.assertTrue(all(key != value for key, value in probe.BAD_AUDIENCE.items()))
        self.assertEqual(probe.DEFAULT_SKILL.name, "SKILL.md")
        self.assertTrue(SKILL.is_file())

    def test_skill_text_is_read_from_production(self):
        self.assertEqual(probe.DEFAULT_SKILL, Path("src/belle_agent/skills/group-chat-participation/SKILL.md"))
        self.assertIn("The only way to reach a reader", INSTRUCTION)
        self.assertIn(POST_MESSAGE, INSTRUCTION)


class SyntheticAdvisories(unittest.TestCase):
    EXPECTED: ClassVar[dict[str, object]] = {md.AUDIENCE: md.AUDIENCE_PERSON, md.ALREADY_ANSWERED: False, md.RESPONSE_REQUESTED: True,
                md.NEEDS_WORK: False, md.CLAIM: True}

    def payload(self, text):
        return json.loads(text.rsplit("\n\n", 1)[1])

    def test_good_advisory_matches_the_production_formatter(self):
        c = case()
        c["decision_expected"] = self.EXPECTED
        rendered = probe.production_advisories([c])["test"]
        answers = probe.synthetic_answers(self.EXPECTED, True)
        self.assertEqual(rendered, format_advisory(answers, probe.production_questions()))
        self.assertTrue(rendered.startswith(md.ADVISORY_PREAMBLE))
        items = self.payload(rendered)
        self.assertEqual(set(items), set(QUESTION_IDS))
        self.assertEqual(items[md.AUDIENCE]["answer"]["value"], md.AUDIENCE_PERSON)
        self.assertEqual(set(items[md.AUDIENCE]["criteria"]), AUDIENCES)
        probabilities = items[md.AUDIENCE]["answer"]["probabilities"]
        self.assertEqual(set(probabilities), AUDIENCES)
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=3)
        self.assertEqual({key: items[key]["answer"] for key in QUESTION_IDS[1:]}, {
            md.ALREADY_ANSWERED: {"type": "noul", "value": 0.1}, md.RESPONSE_REQUESTED: {"type": "noul", "value": 0.9},
            md.NEEDS_WORK: {"type": "noul", "value": 0.1}, md.CLAIM: {"type": "noul", "value": 0.9}})
        self.assertTrue(all("confidence" not in items[key]["answer"] for key in items))

    def test_bad_advisory_reverses_every_answer(self):
        c = case()
        c["decision_expected"] = self.EXPECTED
        items = self.payload(probe.production_advisories([c], "synthetic_bad")["test"])
        self.assertEqual(items[md.AUDIENCE]["answer"]["value"], md.AUDIENCE_BOT)
        self.assertEqual([items[key]["answer"]["value"] for key in QUESTION_IDS[1:]], [0.9, 0.1, 0.9, 0.1])
        self.assertEqual(probe.synthetic_values(self.EXPECTED, False),
                         {md.ALREADY_ANSWERED: 0.9, md.RESPONSE_REQUESTED: 0.1, md.NEEDS_WORK: 0.9, md.CLAIM: 0.1})
        with self.assertRaisesRegex(ValueError, "synthetic_good or synthetic_bad"):
            probe.production_advisories([c], "real")

    def test_bad_audience_never_matches_the_label(self):
        for audience in AUDIENCES:
            expected = {**self.EXPECTED, md.AUDIENCE: audience}
            self.assertNotEqual(probe.synthetic_answers(expected, False)[md.AUDIENCE].value, audience)

    def test_every_case_renders_and_none_omits_the_block(self):
        cases = json.loads(CASES.read_text())
        advisories = probe.production_advisories(cases)
        self.assertEqual(set(advisories), {c["id"] for c in cases})
        with patch("silence_probe.production_advisories", side_effect=AssertionError("must not render advice")):
            path = probe.advisory_file(cases[:1], advice="none")
        try:
            self.assertEqual(json.loads(path.read_text()), {})
        finally:
            path.unlink()


class DecisionProbe(unittest.TestCase):
    def test_scoring_matches_audience_by_equality_and_noul_at_half(self):
        expected = {md.AUDIENCE: md.AUDIENCE_ROOM, md.ALREADY_ANSWERED: True, md.RESPONSE_REQUESTED: False,
                    md.NEEDS_WORK: False, md.CLAIM: True}
        values = {md.AUDIENCE: md.AUDIENCE_ROOM, md.ALREADY_ANSWERED: 0.5, md.RESPONSE_REQUESTED: 0.49,
                  md.NEEDS_WORK: 0.7, md.CLAIM: 0.9}
        result = probe.score_decision_case({"id": "x", "expected": expected}, values)
        self.assertEqual(result["matches"], {md.AUDIENCE: True, md.ALREADY_ANSWERED: True,
                                             md.RESPONSE_REQUESTED: True, md.NEEDS_WORK: False, md.CLAIM: True})
        self.assertFalse(result["all_correct"])
        wrong = probe.score_decision_case({"id": "x", "expected": expected}, {**values, md.AUDIENCE: md.AUDIENCE_BOT})
        self.assertFalse(wrong["matches"][md.AUDIENCE])
        with self.assertRaisesRegex(ValueError, "outside 0–1"):
            probe.score_decision_case({"id": "x", "expected": expected}, {**values, md.CLAIM: 1.5})

    def test_state_carries_the_case_conversation_kind_and_names(self):
        state = probe.decision_state(
            "Who's on call?", [{"role": "user", "content": "hi", "author": "Dana"}, {"role": "assistant", "content": "yo"}],
            md.KIND_PUBLIC, "Ember", "Sam")
        self.assertEqual(state["conversation_kind"], md.KIND_PUBLIC)
        self.assertEqual(state["bot_name"], "Ember")
        self.assertEqual(state["last_message"]["author"], "Sam")
        self.assertEqual([m["author"] for m in state["recent_messages"]], ["Dana", "Ember"])
        self.assertEqual(probe.decision_state("x", [], md.KIND_DIRECT)["conversation_kind"], md.KIND_DIRECT)

    def test_decision_model_list_selects_an_adapter_per_model(self):
        models = probe.load_decision_models(DECISION_MODELS)
        self.assertEqual(models[0], {"model_name": "typesafe/jev-1.13", "api": "openrouter"})
        with patch.dict(os.environ, {"DECISION_API_BASE": "https://openrouter.ai/api/v1", "DECISION_API_KEY": "test"}):
            self.assertEqual(probe.decision_client_from_env(models[0]).name, "openrouter")

    def fake_client(self, audience=md.AUDIENCE_BOT, number=0.9):
        states = []

        class Client:
            name = "openrouter"

            async def decide(self, state, questions):
                states.append(state)
                return {key: (Answer(type="choice", value=audience) if key == md.AUDIENCE else
                              Answer(type="noul", value=number)) for key in questions}

        return Client(), states

    def test_decision_probe_labels_results_for_each_model_and_sends_the_kind(self):
        client, states = self.fake_client()
        cases = [{**case("speak"), "id": "one", "conversation_kind": md.KIND_PUBLIC}]
        with tempfile.TemporaryDirectory() as directory:
            models, path = Path(directory) / "models.jsonl", Path(directory) / "cases.json"
            models.write_text('{"model_name":"a","api":"openrouter"}\n{"model_name":"b","api":"openrouter"}\n')
            path.write_text(json.dumps(cases))
            output, progress = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"DECISION_API_BASE": "https://example.test", "DECISION_API_KEY": "test"}), \
                    patch("silence_probe.decision_client_from_env", return_value=client), \
                    redirect_stdout(output), redirect_stderr(progress):
                self.assertEqual(probe.run_decision_probe(path, models), 1)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([line["model"] for line in lines], ["a", "b"])
        self.assertEqual({state["conversation_kind"] for state in states}, {md.KIND_PUBLIC})
        summary = json.loads(progress.getvalue().splitlines()[0])
        self.assertEqual(summary["questions"][md.AUDIENCE], {"correct": 0, "total": 1})
        self.assertEqual(summary["questions"][md.CLAIM]["false_positive"], 1)
        self.assertNotIn("audience_confusion", summary)

    def test_pipeline_probe_runs_four_arms_per_case_and_model(self):
        client, _ = self.fake_client()
        spoken = {**case("speak"), "id": "one"}
        spoken["decision_expected"][md.AUDIENCE] = md.AUDIENCE_BOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models, chats, path, skill = (root / name for name in ("models.jsonl", "chat.jsonl", "cases.json", "skill.md"))
            models.write_text('{"model_name":"a","api":"openrouter"}\n{"model_name":"b","api":"openrouter"}\n')
            chats.write_text('{"model_name":"one"}\n{"model_name":"two"}\n')
            path.write_text(json.dumps([spoken]))
            skill.write_text("Speak rarely.")
            output, progress = io.StringIO(), io.StringIO()
            with patch("silence_probe.decision_client_from_env", return_value=client), \
                    patch("silence_probe.request_case", side_effect=lambda c, *_: probe.score_case(c, [POST])) as request, \
                    redirect_stdout(output), redirect_stderr(progress):
                self.assertEqual(probe.run_pipeline_probe(path, skill, CARDS, models, chats, workers=1), 0)
        self.assertEqual(Counter(call.args[-1] for call in request.call_args_list), {"one": 8, "two": 8})
        self.assertEqual(sum(not call.args[1] for call in request.call_args_list), 4)
        self.assertEqual([(r["decision_model"], r["chat_model"]) for r in map(json.loads, output.getvalue().splitlines())],
                         [("a", "one"), ("a", "two"), ("b", "one"), ("b", "two")])
        summary = json.loads(progress.getvalue().splitlines()[0])
        self.assertEqual(set(summary["runs"]), set(probe.ARMS))
        self.assertEqual(summary["runs"]["real"]["speak"]["passed"], 1)

    def test_pipeline_pairs_decision_and_chat_outcomes(self):
        spoken = case("speak")
        values = {md.AUDIENCE: md.AUDIENCE_NOBODY, **{key: 0.1 for key in QUESTION_IDS[1:]}}
        runs = {"chat_only": probe.score_case(spoken, []), "real": probe.score_case(spoken, [POST]),
                "synthetic_good": probe.score_case(spoken, [POST]), "synthetic_bad": probe.score_case(spoken, [])}
        outcome = probe.pipeline_outcome(spoken, values, runs)
        self.assertNotIn("relationship", outcome)
        self.assertEqual(outcome["comparison"], "with_only_correct")
        self.assertEqual(outcome["expected"], "speak")
