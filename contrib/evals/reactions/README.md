# Reactions model probe

Grades whether the model calls `react_to_message` when a reaction fits. Reply
text and silence are out of scope (see `contrib/evals/silence/`). Shared host
helpers live in `contrib/evals/common/host.py`.

`cases.json` has 121 scenarios (43 `react`, 78 `no_react`).

## Contract

Only tool: `react_to_message`. Pass/fail is based on usable reaction tool calls.
Assistant text is logged.

| Suite | Passes when |
| --- | --- |
| `react` | a usable add (`emoji` present, not `remove`) |
| `no_react` | no usable reaction |

| Input | Source |
| --- | --- |
| Skill | `src/belle_agent/skills/group-chat-participation/SKILL.md` |
| Tool card | `SlackReactionToolkit().get_tool_definitions()`, written to a temporary file at each run. `--cards FILE` evaluates other cards |
| Questions | `load_questions(examples/config/slack-decision-questions.json)` (`deserves_emoji`, `emoji`, `audience`, `response_requested`) |
| Advisories | `format_advisory` |

Chat pass ignores shortcode match to the `emoji` label; that match is a decision
metric. Connector marks and inbound reaction events are out of scope.

`react` cases have `deserves_emoji: true` and `emoji != no_reaction`. `no_react`
has the opposite. `thanksplus_*` are `react`; whether the model also answers is
out of scope.

Case history uses `{author, text}` lines folded into one user transcript.

## Cases

| Suite | Scenarios |
| --- | --- |
| `react` | jokes, thanks, celebrations, brief acknowledgements |
| `no_react` | asks/requests, corrections, bad news, actionable info, questions to others, already answered, logistics, opinions |

## Run

```sh
python contrib/evals/reactions/probe.py
python contrib/evals/reactions/probe.py --suite no_react --advice none
python contrib/evals/reactions/probe.py --decision-probe --source src --env-file ./.env
python contrib/evals/reactions/probe.py --pipeline-probe --env-file ./.env
python contrib/evals/reactions/probe.py --cards candidate-cards.json
PYTHONPATH=src pytest contrib/evals/reactions/test_reactions_probe.py
```

Model lists: `contrib/evals/models/`. Podman and credentials: see
`session-work`.

## Scoring

| Kind | Severity |
| --- | --- |
| `missed_reaction` | medium |
| `unnecessary_reaction` | low |
| `provider_error` | unscored |

Leave synthetic-bad out of rankings. Labels are provisional. Wrong synthetic
audience/emoji maps live in `WRONG_AUDIENCE` / `WRONG_EMOJI` in `probe.py`.
Aggregate from stderr JSON (`summary` / `runs`).
