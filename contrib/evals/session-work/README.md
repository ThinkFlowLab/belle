# Session work model probe

This model-in-the-loop probe checks whether a chat model follows the
`session-work` skill across research, coding, analysis, writing, planning,
short requests, interactive tasks, detached work, and retained-session
lifecycle choices. All probes use the 151 scenarios in `cases.json`. Each has
an expected session action and separate labels for the two decision questions.
The advisory block uses the
production questions and formatter from `belle_agent.message_decisions`, and
tool descriptions and schemas come from `scheduling/session_cards.json`. The
host renders advisories to a temporary JSON file and mounts it read-only into
Podman, so the `python:3.11-slim` model container does not need Belle's Python
dependencies. Tool calls are observations; they are never executed.

The probe grades only the selected session tool names. It does not validate
arguments, acknowledgement text, task quality, or the model's actual handling
of a detached session. A recent-message case supplies a short transcript in the
preceding chat messages to test whether prior context affects tool choice.
The host-side runner must be able to import the Belle source tree to render the
production advisories.

Run from the repository root after `session-work` is installed:

```sh
python contrib/evals/session-work/probe.py
```

The runner uses every model in `contrib/evals/models/chat_models.jsonl` by
default. Each line names one model, for example
`{"model_name":"meta/muse-spark-1.3"}`. Use
`--chat-models path/to/chat_models.jsonl` to select a different list. It sends
up to 16 requests at once per model and reports completed case counts on stderr.
Select `--suite start` for initial detachment decisions or
`--suite continue` for retained-session and follow-up decisions. The default
`--suite all` runs both. Use `--workers N` to select 1–32 requests at once. Use
`--max-rpm 30` to space request starts by at least two seconds while refilling free worker slots.
JSON results appear on stdout as each case finishes. `--advice synthetic_good`
(the default) uses 0.9 for true decision labels and 0.1 for false labels;
`--advice synthetic_bad` reverses both values. `--advice none` removes the
answers and explanatory block. The skill, tools, request, and history remain
the same in every arm.

The runner reads `API_BASE` and `API_KEY` from `.env` in the current
directory (or the corresponding environment variables). `MODEL_NAME` and
`MODEL_PROVIDER` do not select a model for this probe. It passes the connection
values to a rootless Podman container using the
OpenAI-compatible chat completions API. The container has a read-only root,
dropped capabilities, and read-only access to the probe, cases, skill, and
session cards.
Set `--skill` or `--env-file` to use different paths. Use `--image` to select an
already available Python 3.11 image.

Each JSON line reports the chat model, case, suite, expected and observed tool names,
`pass`, `failure_kind`, `impact`, and `severity`. Passing cases have null failure
fields. stderr ends with per-suite totals, pass counts, failure-kind counts,
impact counts, and severity counts. Provider errors fail the run and appear
separately with `impact: unscored` and null severity.

Severity is an operational priority for chat responsiveness and context use:

| Failure | Severity | Consequence |
| --- | --- | --- |
| Missed detach | high | Substantial work remains in the foreground |
| Missed continuation | high | Required retained context is unavailable |
| Unnecessary detach | medium; high when `interaction_required` is true | Background work starts unnecessarily; interactive work loses the user exchange |
| Unnecessary lookup | low | Session information is read unnecessarily |
| Intermediate lookup | low | A read happens before the expected action; this probe cannot see whether the action follows |
| Missed lookup or wrong tool | medium | Required information or the requested session action is missing |

`interaction_required: true` marks cases that require user input or a
turn-by-turn exchange. Severity tiers are review priorities, not measured harm
or numeric weights; the report has no combined score. A missing tool call is
classified as a miss. A different tool, repeated call, or extra call is a wrong
choice unless it is a read-only lookup before an expected `session_start` or
`session_continue`. An unexpected `session_start` takes precedence because it
starts background work. The exact first-tool-call pass rate still counts an
intermediate lookup as a miss.

Synthetic hints test how the chat model handles accurate and inaccurate
answers. They are not decision-model predictions. Decision correctness and
session-action correctness are different: a correct high `independent_work`
estimate does not imply a short request should be detached. Report missed and
unnecessary actions, intermediate lookups, and severity by arm. A result covers
one model run; repeat before attributing a difference to the advice.
When credentials or the skill file are absent, the command prints `SKIP` and
exits successfully. Deterministic expectation checks run with:

```sh
pytest contrib/evals/session-work/test_probe.py
```

To evaluate the decision model separately, run:

```sh
python contrib/evals/session-work/probe.py --decision-probe --source src --env-file ./.env
```

The decision and four-arm probes run every model in
`contrib/evals/models/decision_models.jsonl` by default. Each JSON line names a
model and its Belle decision adapter. Use `--decision-models path/to/models.jsonl`
to run a different list. For example:

```jsonl
{"model_name":"typesafe/jev-1.14","api":"openrouter"}
{"model_name":"scaledown/classify-1","api":"openrouter_text_state"}
```

Both probes use `DECISION_API_BASE` and `DECISION_API_KEY` for every listed
model. Set them to the OpenRouter endpoint and key for the default list.
`DECISION_MODEL_NAME` does not select a model for these probes. Decision-only
results and summaries include `model` so runs can be compared by case ID.
Choose one decision model and one chat model for an initial four-arm run.
The full default matrix makes one decision request per case and decision model,
then four chat requests per case, decision model, and chat model.

This probe uses Belle's production questions, state builder, adapter, and
parser. All 151 cases have separate labels for `independent_work` and
`continuation_context`. Treat aggregate agreement as agreement with the case
labels, not as independent validation of those labels. Each JSON line records
both estimates and whether each agrees with its case label at 0.5. stderr reports
agreement per question and the number of cases where both agree. The score
measures agreement with those labels.
It does not measure probability calibration. Edit `cases.json` to review or
extend the scenarios. No chat-model tool call is made.

For four-arm end-to-end cases, run:

```sh
python contrib/evals/session-work/probe.py --pipeline-probe --env-file ./.env
```

For each case, the probe asks the decision model once. It then sends the
same case to the chat model four times: chat-only, real decision advice,
synthetic good advice, and synthetic bad advice. The result records decision
agreement with the labels and the first tool choice, failure type, and severity
for each chat arm. The real-versus-chat-only comparison uses the same case ID.
Synthetic good and bad measure sensitivity to controlled answers, not the
quality of the real decision model. A bad answer can be irrelevant to the
expected action. Repeat runs before attributing a change to the advice.
Compare false negatives (missed `session_start` or `session_continue`) and
false positives (unnecessary detachment) separately. Keep read-only lookups
separate because a first lookup can precede the correct action. The target is
correct action choice in every case, but this first-call probe cannot establish
the outcome of a full turn.
The four-arm run defaults to two cases at once; set `--pipeline-workers N` to
change this without changing the chat-only probe's worker count.

## Report format

State the chat and decision models, run count, scored case count, and provider
errors first. Compare arms only on cases scored in all four arms. Keep provider
errors unscored. Use `passes/scored (percent)` in this table. `start` and
`continue` are separate suites.

| Arm | Start | Continue | All |
| --- | ---: | ---: | ---: |
| Chat only | n/N (%) | n/N (%) | n/N (%) |
| Real decision advice | n/N (%) | n/N (%) | n/N (%) |
| Synthetic good | n/N (%) | n/N (%) | n/N (%) |
| Synthetic bad (stress test) | n/N (%) | n/N (%) | n/N (%) |

Report first-action failures by kind and severity. Omit rows with zero counts.
Severity sets review priority. Do not aggregate it into a numeric score.
A read-only lookup can precede a correct later action that this probe does
not observe.

| Failure kind | Chat only | Real | Synthetic good | Synthetic bad |
| --- | ---: | ---: | ---: | ---: |
| Missed detach | n | n | n | n |
| Missed continuation | n | n | n | n |
| Unnecessary detach | n | n | n | n |
| Intermediate lookup | n | n | n | n |
| Unnecessary lookup | n | n | n | n |
| Missed lookup | n | n | n | n |
| Wrong tool | n | n | n | n |

| Failure severity | Chat only | Real | Synthetic good | Synthetic bad |
| --- | ---: | ---: | ---: | ---: |
| High | n | n | n | n |
| Medium | n | n | n | n |
| Low | n | n | n | n |

| Paired chat-only versus real advice | Cases |
| --- | ---: |
| Both passed | n |
| Only real advice passed | n |
| Only chat-only passed | n |
| Both failed | n |

For decision answers, use this table at the stated threshold. Report reviewed
and provisional labels separately only when the case data identifies them.
Otherwise, mark the whole agreement table provisional. Decision-label
agreement and chat-action accuracy are separate metrics. Explain cases where
decision labels and chat actions disagree.

| Decision question | True positive | False negative | False positive | True negative |
| --- | ---: | ---: | ---: | ---: |
| `independent_work` | n | n | n | n |
| `continuation_context` | n | n | n | n |

Cross-tab the real arm's decision-label agreement against its chat action.
This shows when chat passed despite disagreement with a decision label, or
failed despite agreement. Agreement alone does not show that chat followed
the decision answers.

| Decision labels versus real chat action | Cases |
| --- | ---: |
| Both labels agree, chat passes | n |
| Both labels agree, chat fails | n |
| A label disagrees, chat passes | n |
| A label disagrees, chat fails | n |

The synthetic-bad arm deliberately reverses the question labels. Expect more
wrong actions there **by design**. This arm measures the chat model's
susceptibility to bad advice. Exclude it from overall success rates and model
rankings. Synthetic-good advice uses case labels to probe sensitivity.
It does not represent decision-model predictions. Do not call a single run an
improvement or regression.

The cases cover short and interactive requests, research, coding, analysis,
writing, planning, and retained-session actions. Histories range from empty to
twelve messages and include unrelated, topic-relevant, and sparse-relevance
transcripts; some contain assistant replies and prior session results. The probe
sends each history before the current request and builds advisory hints with
Belle's production questions and formatter. Production requests up to twelve
prior Slack messages (`RECENT_MESSAGE_DEPTH`), including bot messages and
excluding the target and newer messages; Slack may return fewer.
