"""Deterministic expectations for the reactions model probe."""

import argparse
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from belle_agent import message_decisions as md
from belle_agent.platforms.slack.reactions import SlackReactionToolkit
from belle_agent.typed_decision import Answer

ROOT = Path(__file__).parents[3]
HERE = ROOT / "contrib/evals/reactions"
_spec = importlib.util.spec_from_file_location("reactions_probe", HERE / "probe.py")
probe = importlib.util.module_from_spec(_spec)
sys.modules["reactions_probe"] = probe
_spec.loader.exec_module(probe)
CASES = HERE / "cases.json"
_CARDS_DIR = tempfile.TemporaryDirectory()
CARDS = Path(_CARDS_DIR.name) / "reaction_cards.json"
CARDS.write_text(json.dumps(probe.production_cards()))
CARD_FILES = (CARDS,)
CASE_TOOLS = ["react_to_message"]
SKILL = ROOT / "src/belle_agent/skills/group-chat-participation/SKILL.md"
DECISION_IDS = probe.decision_question_ids()
OPERATOR_QUESTIONS = probe.decision_questions()
EMOJI = probe.EMOJI


def labels(**kwargs):
    base = {
        md.DESERVES_EMOJI: False,
        EMOJI: "no_reaction",
        md.AUDIENCE: md.AUDIENCE_NOBODY,
        md.RESPONSE_REQUESTED: False,
    }
    base.update(kwargs)
    return base


def fake_decision_answers(questions):
    return {
        key: Answer(type="choice", value=md.AUDIENCE_NOBODY) if key == md.AUDIENCE
        else Answer(type="choice", value="joy") if key == EMOJI
        else Answer(type="noul", value=0.9)
        for key in questions
    }


def case(expect, **extra):
    return {"id": "c", "suite": expect, **extra}


def observed(*, reactions=(), content="", calls=None):
    reactions = list(reactions)
    return {"calls": calls if calls is not None else ["react_to_message"] * len(reactions),
            "reactions": reactions, "content": content}


def tool_call(arguments, name="react_to_message"):
    return {"id": "call_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def reply(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


class ReactionEvalCases(unittest.TestCase):
    def test_production_cards_are_the_toolkit_definitions(self):
        production = SlackReactionToolkit().get_tool_definitions()
        self.assertEqual(probe.production_cards(), production)
        self.assertEqual([card["name"] for card in production], ["react_to_message"])

    def test_host_refuses_a_cards_file_without_a_reaction_card(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cards.json"
            for text in ("[]", "not json", json.dumps([{"name": "react_to_message", "description": "x"}])):
                path.write_text(text)
                output = io.StringIO()
                with redirect_stdout(output), patch("reactions_probe.run_container") as container:
                    self.assertEqual(probe.host_main(argparse.Namespace(cards=path, decision_probe=False)), 2)
                self.assertIn(f"--cards {path}", output.getvalue())
                container.assert_not_called()

    def test_tool_spec_exposes_the_production_description_and_schema(self):
        (react,) = probe.tool_spec(CASE_TOOLS, CARD_FILES)
        (card,) = probe.production_cards()
        self.assertEqual(react["function"]["name"], "react_to_message")
        self.assertEqual(react["function"]["description"], card["description"])
        self.assertEqual(react["function"]["parameters"], card["input_params"])
        self.assertEqual(react["function"]["parameters"]["required"], ["emoji"])

    def test_canonical_cases_fit_the_schema(self):
        cases = json.loads(CASES.read_text())
        self.assertEqual(len(cases), 121)
        probe.validate_cases(cases)
        for entry in cases:
            self.assertNotIn("expect", entry)
            self.assertNotIn("tools", entry)
        counts = Counter(entry["suite"] for entry in cases)
        self.assertEqual(set(counts), {"react", "no_react"})
        self.assertTrue(all(count >= 30 for count in counts.values()), counts)
        emoji_keys = set(OPERATOR_QUESTIONS[EMOJI].criteria or {})
        for entry in cases:
            expected = entry["decision_expected"]
            self.assertEqual(set(expected), set(DECISION_IDS))
            self.assertIn(expected[EMOJI], emoji_keys, entry["id"])
            self.assertIn(entry["conversation_kind"], {md.KIND_GROUP, md.KIND_PUBLIC, md.KIND_PRIVATE})
            self.assertEqual(entry["bot_name"], "Belle")
            if entry["suite"] == "react":
                self.assertTrue(expected[md.DESERVES_EMOJI], entry["id"])
                self.assertNotEqual(expected[EMOJI], "no_reaction", entry["id"])
            if entry["suite"] == "no_react":
                self.assertFalse(expected[md.DESERVES_EMOJI], entry["id"])
                self.assertEqual(expected[EMOJI], "no_reaction", entry["id"])

    def test_cases_cover_the_required_situations(self):
        cases = json.loads(CASES.read_text())
        by_id = {entry["id"]: entry for entry in cases}
        prefixes = {entry["id"].split("_")[0] for entry in cases}
        self.assertTrue({"joke", "thanks", "celebrate", "ack", "ask", "req", "followup", "room", "correct",
                         "badnews", "action", "thanksplus", "fact", "other", "answered", "logistics",
                         "opinion", "passing", "closed"} <= prefixes)
        # Thanks plus a question still deserves a reaction; replying is out of scope.
        for name in ("thanksplus_delete_volume", "thanksplus_background_flag",
                     "thanksplus_tag_image", "thanksplus_email"):
            self.assertEqual(by_id[name]["suite"], "react")
            self.assertTrue(by_id[name]["decision_expected"][md.DESERVES_EMOJI])
        for entry in cases:
            if entry["id"].startswith(("ask_", "req_", "badnews_", "other_", "answered_")):
                self.assertEqual(entry["suite"], "no_react", entry["id"])
        self.assertEqual(by_id["other_review_pr"]["decision_expected"][md.AUDIENCE], md.AUDIENCE_PERSON)

    def test_validation_refuses_misfitting_cases(self):
        good = json.loads(CASES.read_text())[0]
        for change, error in [
            ({"conversation_kind": "cafeteria"}, "conversation_kind"),
            ({"suite": "other"}, "suite"),
            ({"decision_expected": {md.DESERVES_EMOJI: True}}, "decision_expected"),
            ({"decision_expected": labels(audience="everyone")}, "audience"),
            ({"decision_expected": labels(**{md.DESERVES_EMOJI: 1})}, "boolean"),
            ({"decision_expected": labels(**{EMOJI: "not_an_emoji"})}, "emoji"),
            ({"recent_messages": [{"role": "user", "content": "x"}]}, "author and text"),
        ]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, error):
                probe.validate_cases([{**good, **change}])
        with self.assertRaisesRegex(ValueError, "unique"):
            probe.validate_cases([good, good])

    def test_advisories_use_operator_questions_and_formatter(self):
        entry = {"id": "x", "decision_expected": labels(
            **{md.DESERVES_EMOJI: True, md.AUDIENCE: md.AUDIENCE_ROOM, EMOJI: "joy"})}
        advisory = probe.production_advisories([entry], "synthetic_good")["x"]
        expected = md.format_advisory(
            probe.synthetic_answers(entry["decision_expected"], True, OPERATOR_QUESTIONS),
            OPERATOR_QUESTIONS,
        )
        self.assertEqual(advisory, expected)
        self.assertTrue(advisory.startswith(md.ADVISORY_PREAMBLE))
        body = json.loads(advisory[len(md.ADVISORY_PREAMBLE):])
        self.assertEqual(set(body), set(DECISION_IDS))
        for key in DECISION_IDS:
            self.assertEqual(body[key]["instructions"], OPERATOR_QUESTIONS[key].instructions)
        self.assertEqual(body[md.AUDIENCE]["criteria"], dict(OPERATOR_QUESTIONS[md.AUDIENCE].criteria))
        self.assertEqual(body[EMOJI]["criteria"], dict(OPERATOR_QUESTIONS[EMOJI].criteria))
        self.assertEqual(body[md.DESERVES_EMOJI]["answer"], {"type": "noul", "value": 0.9})
        self.assertEqual(body[md.RESPONSE_REQUESTED]["answer"], {"type": "noul", "value": 0.1})
        self.assertEqual(body[md.AUDIENCE]["answer"]["value"], md.AUDIENCE_ROOM)
        self.assertEqual(body[EMOJI]["answer"]["value"], "joy")
        # Operator file replaces the built-in deserves_emoji instructions.
        self.assertNotEqual(OPERATOR_QUESTIONS[md.DESERVES_EMOJI].instructions,
                            md.QUESTIONS[md.DESERVES_EMOJI].instructions)
        self.assertNotIn(EMOJI, md.QUESTIONS)
        bad = json.loads(probe.production_advisories([entry], "synthetic_bad")["x"][len(md.ADVISORY_PREAMBLE):])
        self.assertEqual(bad[md.DESERVES_EMOJI]["answer"]["value"], 0.1)
        self.assertEqual(bad[md.RESPONSE_REQUESTED]["answer"]["value"], 0.9)
        self.assertNotEqual(bad[md.AUDIENCE]["answer"]["value"], md.AUDIENCE_ROOM)
        self.assertNotEqual(bad[EMOJI]["answer"]["value"], "joy")
        with self.assertRaisesRegex(ValueError, "synthetic_good or synthetic_bad"):
            probe.production_advisories([entry], "fixed")
        path = Path("/tmp/custom-questions.json")
        with patch.object(probe, "decision_questions", return_value=OPERATOR_QUESTIONS) as load:
            probe.production_advisories([entry], "synthetic_good", questions_path=path)
        load.assert_called_once_with(path)

    def test_synthetic_bad_choices_are_wrong_for_every_option(self):
        for choice in OPERATOR_QUESTIONS[md.AUDIENCE].criteria:
            good = probe.synthetic_answers({md.AUDIENCE: choice}, True, OPERATOR_QUESTIONS)[md.AUDIENCE]
            bad = probe.synthetic_answers({md.AUDIENCE: choice}, False, OPERATOR_QUESTIONS)[md.AUDIENCE]
            self.assertEqual(good.value, choice)
            self.assertNotEqual(bad.value, choice)
            self.assertAlmostEqual(sum(bad.probabilities.values()), 1.0)
            self.assertEqual(set(bad.probabilities), set(OPERATOR_QUESTIONS[md.AUDIENCE].criteria))
        for choice in OPERATOR_QUESTIONS[EMOJI].criteria:
            good = probe.synthetic_answers({EMOJI: choice}, True, OPERATOR_QUESTIONS)[EMOJI]
            bad = probe.synthetic_answers({EMOJI: choice}, False, OPERATOR_QUESTIONS)[EMOJI]
            self.assertEqual(good.value, choice)
            self.assertNotEqual(bad.value, choice)
            self.assertEqual(set(bad.probabilities), set(OPERATOR_QUESTIONS[EMOJI].criteria))

    def test_system_prompt_carries_the_skill_and_only_the_reaction_task(self):
        prompt = probe.system_prompt({"bot_name": "Belle", "conversation_kind": md.KIND_PUBLIC}, SKILL.read_text())
        self.assertIn(SKILL.read_text(), prompt)
        self.assertIn("react_to_message", prompt)
        for word in ("NO_REPLY", "silence", "post_message", "not sent to anyone",
                     "posted to the conversation", "Slack"):
            self.assertNotIn(word, prompt.replace(SKILL.read_text(), ""))
        self.assertIn("public channel", prompt)
        self.assertIn("a participant in a public channel", prompt)
        self.assertTrue(set(probe.KIND_LABELS) >= {md.KIND_DIRECT, md.KIND_GROUP, md.KIND_PUBLIC, md.KIND_PRIVATE})

    def test_transcript_names_authors_and_the_new_message(self):
        text = probe.transcript({"sender": "Maya", "user": "Hello", "recent_messages": [{"author": "Sam", "text": "Hi"}]})
        self.assertEqual(text, "Recent messages:\nSam: Hi\n\nNew message from Maya:\nHello")
        self.assertEqual(probe.transcript({"sender": "Maya", "user": "Hello"}), "New message from Maya:\nHello")

    def test_reaction_emoji_requires_a_usable_adding_call(self):
        self.assertEqual(probe.reaction_emoji(tool_call({"emoji": ":tada:"})), "tada")
        self.assertIsNone(probe.reaction_emoji(tool_call({"emoji": ""})))
        self.assertIsNone(probe.reaction_emoji(tool_call({})))
        self.assertIsNone(probe.reaction_emoji(tool_call({"emoji": "eyes", "remove": True})))
        self.assertIsNone(probe.reaction_emoji({"function": {"name": "react_to_message", "arguments": "{oops"}}))

    def test_scoring_expect_react_needs_a_usable_reaction_and_ignores_text(self):
        c = case("react")
        for content in ("", "Ha!", "NO_REPLY", "(no reply)"):
            self.assertTrue(probe.score_case(c, observed(reactions=["joy"], content=content))["pass"], content)
        for obs in (observed(), observed(content="Ha, good one!"), observed(content="NO_REPLY"),
                    observed(calls=["react_to_message"])):
            result = probe.score_case(c, obs)
            self.assertFalse(result["pass"])
            self.assertEqual((result["failure_kind"], result["severity"]), ("missed_reaction", "medium"))
            self.assertEqual(result["impact"], "missing_acknowledgement")

    def test_scoring_expect_no_react_ignores_text(self):
        c = case("no_react")
        for content in ("", "NO_REPLY", "Port 5432.", "(no reply)", "Nothing to add."):
            result = probe.score_case(c, observed(content=content))
            self.assertTrue(result["pass"], content)
            self.assertIsNone(result["failure_kind"])
        for obs in (observed(reactions=["eyes"]), observed(reactions=["eyes"], content="NO_REPLY"),
                    observed(reactions=["eyes"], content="Priya will answer that.")):
            result = probe.score_case(c, obs)
            self.assertFalse(result["pass"])
            self.assertEqual((result["failure_kind"], result["severity"]), ("unnecessary_reaction", "low"))
            self.assertEqual(result["impact"], "unwanted_reaction")

    def test_failure_kinds_are_only_the_reaction_kinds(self):
        self.assertEqual(set(probe.FAILURE_SEVERITY), {"missed_reaction", "unnecessary_reaction"})
        self.assertEqual(probe.SUITES, ("react", "no_react"))

    def test_scoring_reports_observations_and_rejects_unknown_expectations(self):
        result = probe.score_case(case("react"), observed(reactions=["joy"], content="Ha " * 200))
        self.assertEqual(result["observed_tools"], ["react_to_message"])
        self.assertEqual(result["reactions"], ["joy"])
        self.assertEqual(len(result["final_text"]), 200)
        self.assertIsNone(result["failure_kind"])
        for gone in ("spoke", "silent", "reply"):
            self.assertNotIn(gone, result)
        for suite in ("react_to_message", "speak", "silence"):
            with self.assertRaisesRegex(ValueError, "unknown suite"):
                probe.score_case({"id": "x", "suite": suite}, observed())

    def test_provider_failure_is_unscored(self):
        result = probe.provider_failure(case("no_react"), "provider HTTP 500")
        self.assertEqual((result["failure_kind"], result["impact"], result["severity"]),
                         ("provider_error", "unscored", None))
        summary = probe.suite_summary([result, probe.score_case(case("no_react"), observed(content="Answer."))])
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failure_kinds"], {"provider_error": 1})
        self.assertEqual(summary["severities"], {})

    def test_request_feeds_reaction_results_back_and_grades_only_the_reaction(self):
        c = case("react", id="c", bot_name="Belle", conversation_kind=md.KIND_PUBLIC, sender="Sam",
                 user="We shipped!")
        sent = []

        def fake_post(payload, _config):
            sent.append(json.loads(json.dumps(payload)))
            if len(sent) == 1:
                return reply(None, [tool_call({"emoji": "tada"})])
            return reply("NO_REPLY")

        with patch("reactions_probe.post_chat", side_effect=fake_post):
            result = probe.request_case(c, "ADVICE", "SKILL", {}, CARD_FILES, "m")
        self.assertTrue(result["pass"])
        self.assertEqual(result["observed_tools"], ["react_to_message"])
        self.assertEqual(result["reactions"], ["tada"])
        self.assertEqual(result["final_text"], "NO_REPLY")
        first, second = sent
        self.assertEqual([m["role"] for m in first["messages"]], ["system", "user"])
        self.assertTrue(first["messages"][1]["content"].startswith("ADVICE\n\n"))
        self.assertEqual(second["messages"][-1]["role"], "tool")
        self.assertEqual(json.loads(second["messages"][-1]["content"])["emoji"], "tada")
        self.assertEqual(first["tools"], probe.tool_spec(CASE_TOOLS, CARD_FILES))
        self.assertEqual([t["function"]["name"] for t in first["tools"]], CASE_TOOLS)

    def test_request_reports_an_unusable_reaction_to_the_model(self):
        c = case("react", id="c", conversation_kind=md.KIND_PUBLIC, sender="Sam", user="lol")
        sent = []

        def fake_post(payload, _config):
            sent.append(json.loads(json.dumps(payload)))
            return reply(None, [tool_call({})]) if len(sent) == 1 else reply("ok")

        with patch("reactions_probe.post_chat", side_effect=fake_post):
            result = probe.request_case(c, "", "SKILL", {}, CARD_FILES, "m")
        self.assertEqual(result["failure_kind"], "missed_reaction")
        self.assertFalse(json.loads(sent[1]["messages"][-1]["content"])["ok"])

    def test_request_ignores_final_text_for_both_suites(self):
        for text in ("Port 5432.", "NO_REPLY", "(no reply)"):
            with patch("reactions_probe.post_chat", return_value=reply(text)):
                quiet = probe.request_case(case("no_react", id="c", conversation_kind=md.KIND_PUBLIC, sender="Sam",
                                          user="hi"), "", "SKILL", {}, CARD_FILES, "m")
                missed = probe.request_case(case("react", id="c", conversation_kind=md.KIND_PUBLIC, sender="Sam",
                                           user="hi"), "", "SKILL", {}, CARD_FILES, "m")
            self.assertTrue(quiet["pass"], text)
            self.assertEqual(missed["failure_kind"], "missed_reaction", text)

    def test_unnecessary_reaction_is_graded_even_when_the_model_then_stays_quiet(self):
        c = case("no_react", id="c", conversation_kind=md.KIND_PUBLIC, sender="Sam", user="hi")
        responses = iter([reply("thinking out loud", [tool_call({"emoji": "eyes"})]), reply("NO_REPLY")])
        with patch("reactions_probe.post_chat", side_effect=lambda *_: next(responses)):
            result = probe.request_case(c, "", "SKILL", {}, CARD_FILES, "m")
        self.assertEqual(result["failure_kind"], "unnecessary_reaction")
        self.assertEqual(result["final_text"], "NO_REPLY")

    def test_request_allows_one_continuation_after_a_reaction(self):
        c = case("react", id="c", conversation_kind=md.KIND_PUBLIC, sender="Sam", user="lol")
        with patch("reactions_probe.post_chat", return_value=reply(None, [tool_call({"emoji": "joy"})])) as post:
            result = probe.request_case(c, "", "SKILL", {}, CARD_FILES, "m")
        self.assertEqual(probe.MAX_TOOL_ROUNDS, 1)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(result["pass"])

    def test_request_paces_every_chat_call(self):
        c = case("react", id="c", conversation_kind=md.KIND_PUBLIC, sender="Sam", user="lol")
        pacer = Mock()
        with patch("reactions_probe.post_chat", return_value=reply(None, [tool_call({"emoji": "joy"})])):
            probe.request_case(c, "", "SKILL", {}, CARD_FILES, "m", pacer=pacer)
        self.assertEqual(pacer.wait.call_count, 2)

    def test_request_handles_provider_errors(self):
        c = case("react", id="c", conversation_kind=md.KIND_PUBLIC, sender="Sam", user="hi")
        with patch("reactions_probe.post_chat", return_value={"choices": []}):
            self.assertEqual(probe.request_case(c, "", "S", {}, CARD_FILES, "m")["failure_kind"], "provider_error")
        error = probe.urllib.error.URLError("down")
        with patch("reactions_probe.post_chat", side_effect=error):
            self.assertEqual(probe.request_case(c, "", "S", {}, CARD_FILES, "m")["failure_kind"], "provider_error")
        with patch("reactions_probe.post_chat", side_effect=json.JSONDecodeError("msg", "doc", 0)):
            self.assertEqual(probe.request_case(c, "", "S", {}, CARD_FILES, "m")["error"],
                             "provider response was not valid JSON")

    def test_runner_reports_each_case_and_suite_summaries(self):
        catalog = json.loads(CASES.read_text())
        react = next(entry for entry in catalog if entry["suite"] == "react")
        no_react = next(entry for entry in catalog if entry["suite"] == "no_react")
        cases = [{**react, "id": "a"}, {**no_react, "id": "b"}]
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("skill", "cases", "cards", "advisories", "models")]
            paths[0].write_text("skill")
            paths[1].write_text(json.dumps(cases))
            paths[2].write_text("[]")
            paths[3].write_text(json.dumps({"a": "", "b": ""}))
            paths[4].write_text('{"model_name":"test"}\n')
            output, progress = io.StringIO(), io.StringIO()
            with (patch.dict(os.environ, {"API_BASE": "https://example.test", "API_KEY": "test"}),
                  patch("reactions_probe.request_case",
                        side_effect=lambda c, *_, **__: {"case": c["id"], "suite": c["suite"], "pass": True}),
                  redirect_stdout(output), redirect_stderr(progress)):
                self.assertEqual(probe.run_inside(paths[0], paths[1], [paths[2]], paths[3], workers=2,
                                            chat_models_path=paths[4]), 0)
            self.assertEqual({json.loads(line)["case"] for line in output.getvalue().splitlines()}, {"a", "b"})
            self.assertIn("2/2 cases complete", progress.getvalue())
            self.assertIn('"react": {', progress.getvalue())
            self.assertIn('"no_react": {', progress.getvalue())

    def test_chat_and_decision_model_lists(self):
        models_dir = ROOT / "contrib/evals/models"
        names = probe.load_chat_models(models_dir / "chat_models.jsonl")
        self.assertEqual(names, [json.loads(line)["model_name"]
                                 for line in (models_dir / "chat_models.jsonl").read_text().splitlines()
                                 if line.strip()])
        models = probe.load_decision_models(models_dir / "decision_models.jsonl")
        self.assertIn({"model_name": "scaledown/classify-1", "api": "openrouter_text_state"}, models)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.jsonl"
            path.write_text('{"model_name":"a"}\n{"model_name":"a"}\n')
            with self.assertRaisesRegex(ValueError, "unique"):
                probe.load_chat_models(path)
            path.write_text('{"model_name":"a","api":"unknown"}\n')
            with self.assertRaisesRegex(ValueError, "unsupported decision api"):
                probe.load_decision_models(path)

    def test_decision_cases_build_group_state(self):
        decision_cases = probe.load_decision_cases(CASES)
        self.assertGreaterEqual(len(decision_cases), 100)
        by_id = {entry["id"]: entry for entry in decision_cases}
        state = probe.decision_state(by_id["answered_staging_backup"])
        self.assertEqual(state["conversation_kind"], md.KIND_PUBLIC)
        self.assertEqual(state["bot_name"], "Belle")
        self.assertEqual(state["last_message"]["author"], "Sam")
        self.assertEqual(state["recent_messages"][0]["author"], "Alex")

    def test_decision_scoring_covers_noul_and_choice_labels(self):
        c = {"id": "x", "expected": labels(
            **{md.DESERVES_EMOJI: True, md.AUDIENCE: md.AUDIENCE_PERSON, EMOJI: "joy"})}
        result = probe.score_decision_case(c, {md.DESERVES_EMOJI: 0.9, md.AUDIENCE: md.AUDIENCE_BOT,
                                         md.RESPONSE_REQUESTED: 0.2, EMOJI: "joy"})
        self.assertEqual(result["matches"], {md.DESERVES_EMOJI: True, md.AUDIENCE: False,
                                             md.RESPONSE_REQUESTED: True, EMOJI: True})
        self.assertFalse(result["all_correct"])
        with self.assertRaisesRegex(ValueError, "unknown audience"):
            probe.score_decision_case(c, {md.DESERVES_EMOJI: 0.9, md.AUDIENCE: "everyone",
                                    md.RESPONSE_REQUESTED: 0.2, EMOJI: "joy"})
        with self.assertRaisesRegex(ValueError, "unknown emoji"):
            probe.score_decision_case(c, {md.DESERVES_EMOJI: 0.9, md.AUDIENCE: md.AUDIENCE_PERSON,
                                    md.RESPONSE_REQUESTED: 0.2, EMOJI: "not_an_emoji"})
        with self.assertRaisesRegex(ValueError, "outside 0–1"):
            probe.score_decision_case(c, {md.DESERVES_EMOJI: 1.5, md.AUDIENCE: md.AUDIENCE_PERSON,
                                    md.RESPONSE_REQUESTED: 0.2, EMOJI: "joy"})

    def test_decision_probe_labels_results_for_each_model(self):
        class Client:
            name = "openrouter"

            async def decide(self, _state, questions):
                return fake_decision_answers(questions)

        entry = json.loads(CASES.read_text())[0]
        with tempfile.TemporaryDirectory() as directory:
            models, cases = Path(directory) / "models.jsonl", Path(directory) / "cases.json"
            models.write_text('{"model_name":"a","api":"openrouter"}\n{"model_name":"b","api":"openrouter"}\n')
            cases.write_text(json.dumps([entry]))
            output = io.StringIO()
            with (patch.dict(os.environ, {"DECISION_API_BASE": "https://example.test", "DECISION_API_KEY": "test"}),
                  patch("reactions_probe.decision_client_from_env", return_value=Client()),
                  redirect_stdout(output), redirect_stderr(io.StringIO())):
                probe.run_decision_probe(cases, models)
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([row["model"] for row in rows], ["a", "b"])
            self.assertEqual(set(rows[0]["values"]), set(DECISION_IDS))

    def test_pipeline_pairs_decision_and_chat_outcomes(self):
        c = case("no_react", decision_expected=labels(
            **{md.AUDIENCE: md.AUDIENCE_BOT, md.RESPONSE_REQUESTED: True}))
        good = probe.score_case(c, observed(content="Answer."))
        bad = probe.score_case(c, observed(reactions=["+1"]))
        values = {md.DESERVES_EMOJI: 0.9, md.AUDIENCE: md.AUDIENCE_BOT,
                  md.RESPONSE_REQUESTED: 0.9, EMOJI: "no_reaction"}
        result = probe.pipeline_outcome(c, values, {"chat_only": bad, "real": good,
                                              "synthetic_good": good, "synthetic_bad": bad})
        self.assertNotIn("relationship", result)
        self.assertEqual(result["comparison"], "with_only_correct")
        self.assertEqual(set(result["runs"]), {"chat_only", "real", "synthetic_good", "synthetic_bad"})

    def test_pipeline_probe_runs_four_arms_per_model_pair(self):
        class Client:
            name = "openrouter"

            async def decide(self, _state, questions):
                return fake_decision_answers(questions)

        entry = next(e for e in json.loads(CASES.read_text()) if e["suite"] == "react")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models, chats, cases = (root / n for n in ("models.jsonl", "chat_models.jsonl", "cases.json"))
            models.write_text('{"model_name":"a","api":"openrouter"}\n{"model_name":"b","api":"openrouter"}\n')
            chats.write_text('{"model_name":"one"}\n{"model_name":"two"}\n')
            cases.write_text(json.dumps([entry]))
            output = io.StringIO()
            progress = io.StringIO()
            with (patch("reactions_probe.decision_client_from_env", return_value=Client()),
                  patch("reactions_probe.request_case",
                        side_effect=lambda c, *_, **__: probe.score_case(c, observed(reactions=["joy"]))) as request,
                  redirect_stdout(output), redirect_stderr(progress)):
                self.assertEqual(probe.run_pipeline_probe(cases, SKILL, CARD_FILES, models, chats, workers=1), 0)
            self.assertEqual(Counter(call.args[-1] for call in request.call_args_list), {"one": 8, "two": 8})
            self.assertEqual(sum(not call.args[1] for call in request.call_args_list), 4)
            self.assertEqual([(r["decision_model"], r["chat_model"]) for r in map(json.loads, output.getvalue().splitlines())],
                             [("a", "one"), ("a", "two"), ("b", "one"), ("b", "two")])
            summary = json.loads(progress.getvalue().splitlines()[0])
            self.assertEqual(set(summary["runs"]), {"chat_only", "real", "synthetic_good", "synthetic_bad"})
            self.assertEqual(summary["runs"]["real"]["react"]["passed"], 1)

    def test_decision_and_pipeline_hosts_mount_the_questions_file(self):
        questions = ROOT / "examples/config/slack-decision-questions.json"
        base = dict(
            skill=SKILL, cards=None, env_file=Path("/nonexistent.env"),
            cases=CASES, source=ROOT / "src", questions=questions, suite="all",
            decision_image="img", decision_models=ROOT / "contrib/evals/models/decision_models.jsonl",
            chat_models=ROOT / "contrib/evals/models/chat_models.jsonl", pipeline_workers=1, max_rpm=0,
            image="img", workers=1, advice="none")
        env = {
            "API_BASE": "https://example.test", "API_KEY": "test",
            "DECISION_API_BASE": "https://decision.test", "DECISION_API_KEY": "test",
        }
        for kwargs in ({"decision_probe": True, "pipeline_probe": False},
                       {"decision_probe": False, "pipeline_probe": True}):
            with patch.dict(os.environ, env), patch("reactions_probe.run_container", return_value=0) as container:
                self.assertEqual(probe.host_main(argparse.Namespace(**{**base, **kwargs})), 0)
            mounts = {target for _, target in container.call_args.args[3]}
            self.assertIn("/questions.json", mounts)
            self.assertIn("--questions", container.call_args.args[4])
            self.assertEqual("/reaction_cards.json" in mounts, kwargs["pipeline_probe"])


if __name__ == "__main__":
    unittest.main()
