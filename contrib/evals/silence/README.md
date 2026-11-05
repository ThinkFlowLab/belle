# Silence model probe

Optional-reply speak vs silence with `group-chat-participation` and production's
`post_message` tool. Shared host helpers live in `contrib/evals/common/host.py`.

`cases.json` has 130 scenarios (87 `silence`, 43 `speak`). Silence is always
offered. Required-reply turns are out of scope.

## Contract

The model gets `post_tool_instruction("post_message")` and may stay silent:

- `speak`: a `post_message` call with non-empty `text`
- `silence`: no `post_message` call

Final assistant text is withheld from grading.

| Input | Source |
| --- | --- |
| Skill | `src/belle_agent/skills/group-chat-participation/SKILL.md` |
| Instruction | `post_tool_instruction("post_message")` in `belle_agent/silence.py` |
| Tool card | `_post_tool_definition()`, written to a temporary file at each run. `--cards FILE` evaluates another card |
| Questions | `audience`, `already_answered`, `response_requested`, `needs_work`, `claim` in `message_decisions` |
| Advisories | `format_advisory` |

Decision answers are advisory only. Shadow disposition is not sent.

Case history uses chat-shaped `{role, content}` turns (optional `author`).

## Cases

- `speak`: direct ask to the bot; work whose result must be reported; material
  correction; unanswered room question the bot can answer.
- `silence`: question to another person; already answered; joke/thanks/status;
  room logistics; waived reply; claim to check later; human-to-human thread.

Histories are 0–12 messages. Labels are provisional; review before treating
small gaps as model faults.

## Run

```sh
python contrib/evals/silence/probe.py
python contrib/evals/silence/probe.py --decision-probe --source src --env-file ./.env
python contrib/evals/silence/probe.py --pipeline-probe --env-file ./.env
python contrib/evals/silence/probe.py --cards candidate-cards.json
PYTHONPATH=src pytest contrib/evals/silence/test_silence_probe.py
```

Model lists: `contrib/evals/models/`. Podman and credentials: see
`session-work`. Chat arms: `--advice synthetic_good|synthetic_bad|none`.
Pipeline arms: chat-only, real, synthetic good, synthetic bad.

## Scoring

| Kind | Meaning | Severity |
| --- | --- | --- |
| `missed_silence` | posted when silence expected | high if audience is another person or already answered; else medium |
| `missed_reply` | no post when speech expected | high |
| `empty_post` | `post_message` without usable text | medium |
| `provider_error` | HTTP/timeout/malformed | unscored |

Grade the first response's tool calls only. Keep speak and silence suites
separate. Leave synthetic-bad out of rankings. Decision agreement and chat
action are separate metrics. Aggregate from stderr JSON (`summary` / `runs`).
