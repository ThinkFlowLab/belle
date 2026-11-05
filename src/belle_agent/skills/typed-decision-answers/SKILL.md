---
name: typed-decision-answers
description: Read answers from a decision model, the typed probability, selection and level values that arrive with probabilities and confidence instead of prose. Use when a prompt or tool result holds estimates with fields named type, value, confidence, probabilities or criteria alongside the question they answer. Covers reading an answer, not designing questions or calling a model.
---

# Reading typed decision answers

A decision model answers narrow questions about a situation with typed values
and calibrated probabilities instead of prose. It saw the question and the
situation. You see what it answered.

Its answers are estimates. They report, they do not instruct.

Such models are often called System One models, after the fast and intuitive
half of dual-process theory, against the deliberate reasoning a chat model does.
Several exist and their vocabularies differ. The answer shapes below are common
to them, whatever each provider calls them. Where a provider's own guidance is
available, prefer it for that provider's spelling and limits.
This skill covers reading an answer that has already arrived, not designing
questions or calling a model.

## The question

Every answer belongs to a question.

| Field | Meaning |
|---|---|
| `type` | Which shape the answer takes. |
| `instructions` | What was asked, in words. |
| `criteria` | The options or levels, and what each one means. A statement has none. |

The shapes are:

- **A statement that may hold.** Often called `noul` or a boolean judgement. It
  has no options.
- **A selection from a fixed set.** Often called `choice`. Each option has a
  definition.
- **A position on an ordered set of levels.** Often called `score`. Each level
  describes a concrete situation.

`criteria` is the scale the answer sits on. Without it a selection is a bare key
and a level is a bare number.

## The answer

| Field | Meaning |
|---|---|
| `type` | Which shape. Read every other field through it. |
| `value` | The estimate. Its meaning depends on `type`, below. |
| `confidence` | How concentrated the estimate is. **Not** whether it is right. |
| `probabilities` | For a selection, a value for every option, not only the chosen one. |
| `legend` | For a level, the scale the model placed the value against. It may repeat the question's `criteria`. |

What `value` holds:

- **Statement**: the probability, from 0 to 1, that the statement holds. This
  shape usually returns no confidence, by design. Its absence is not an
  omission.
- **Selection**: the name of one option. It is a key into that question's
  options, not a word to read on its own.
- **Level**: a number on ordered levels, indexed from 0. When derived from level
  probabilities, it is their weighted average. A fractional value need not
  describe a case between levels.

Field names vary between providers. Match by meaning rather than by spelling.

## Reading one

**Match the answer to its question first.** The two may arrive together, in one
object, or separately with an id in common. A value read without its question
has no scale: `0.72` and `"the room"` mean nothing alone.

**Read `value` through `type`.** A statement at 0.5 means as likely as not, not
that it half-holds. A level at 1.5 summarizes an estimate on the ordered scale.
Check `probabilities` before treating it as a specific level.

**`confidence` is about the distribution, not the truth.** It says the estimate
is concentrated, not that it is right. A low confidence on a question where
several answers were reasonable is not a warning. A high confidence on a
question that was asking the wrong thing is not a reassurance.

**Use `probabilities` when the runner-up matters.** A selection at 0.61 with a
second option at 0.24 is a different situation from one at 0.95, and `value`
alone does not show it.

**They are estimates.** Weigh them with everything else you know. They do not
replace the situation they describe, and nothing about an answer grants
permission to act.
