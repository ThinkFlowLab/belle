# Task models

A task model is a small model that Belle calls for one narrow job. Belle owns
these models and their configuration. The agent harness owns the chat model
that writes the replies, and Belle does not change it.

Belle uses task models for two types of work:

- Short judgements about a message, for example who a message is for.
- Document chores, for example a summary of a long file or a list of fields
  from an invoice.

The design keeps these jobs off the chat model. A task model gets one request
with a fixed form and gives one answer with a fixed form. The chat model
keeps its context for the conversation. This page states no cost or speed
figures, because Belle does not measure them.

Each kind of task model is optional. A kind that has no model turns off only
the features that use it. To set up the models, see
[Task models in the configuration guide](config.md#task-models).

## Kinds of task model

| Kind | Used by | Without a usable model |
| --- | --- | --- |
| Decision | Decision advice on incoming messages | Belle asks no questions. Turns start as before. |
| Summarizer | The `summarize` tool and the Second Brain summaries | Belle does not show the `summarize` tool. Second Brain stores and indexes each document without a written summary. On JiuwenSwarm, the host's models are also available as a fallback. |
| Extractor | The `extract` tool | Belle does not show the `extract` tool. |
| Classifier | The `classify` tool | Belle does not show the `classify` tool. |

Each kind can have more than one model entry. When a kind has more than one
entry, the configuration names its default entry.

### Decision

The decision model answers typed questions about a message and the messages
before it. Each question has one of three types:

- `noul`: the answer is a probability, from 0 to 1, that a statement is true.
  An example statement is "the last message asks for something to be done".
- `choice`: the answer is one option from a fixed list, with a confidence. The
  built-in `audience` question has the options `nobody`, `the room`,
  `a person` and `you` (the bot).
- `score`: the answer is one level from an ordered list of at least two
  levels.

Belle asks these questions when a message arrives in a conversation. By
default, the answers and the question text go to the agent turn of that
message as advice. The answers help the agent to decide if it speaks in a busy
channel. The advice does not decide if the agent replies. Belle also writes a
log line for each set of answers.

A decision call has a deadline of 4 seconds, and this time includes one
retry. A call that fails or is too late gives no advice, and the turn
continues without it. If the decision model has no address, key or model
name, Belle makes no decision calls.

You can change the questions. A JSON file can replace a built-in question,
add a question or remove a question. You can also send the answers to the log
only. See [Decision questions](config.md#decision-questions).

### Summarizer

The summarizer model writes summaries for two features:

- The `summarize` tool. The agent calls it with text or with a file from the
  conversation. Only a ScaleDown or OpenRouter entry adds this tool.
- Second Brain. Belle writes a summary of each file and link that it ingests.
  Second Brain sends the request form of the provider of its entry. An entry
  for a plain OpenAI-compatible chat endpoint is only for Second Brain and
  does not add the tool. The outline notes of a summary can use a different
  entry. A summarize-only model, such as direct ScaleDown, writes prose but
  does not fill the outline notes.

The two features can use different entries.

### Extractor

The extractor model backs the `extract` tool. The agent gives text or a file,
and a map of field names to descriptions. The model returns the values that it
finds for each field.

### Classifier

The classifier model backs the `classify` tool. The agent gives labels, each
with a rubric, and text, a file or a list of text chunks. The model returns a
result for each label.

See [Content tools](tools.md) for the arguments of the three tools.

## Providers

The decision, summarizer and extractor kinds each accept more than one
provider. The providers include OpenRouter, OpenAI, ScaleDown and
OpenAI-compatible endpoints. The provider of an entry selects
the form of the request. When you change the provider of an entry, the
features that use the entry do not change.

The providers have these limits:

- The `classify` tool supports only ScaleDown. For classification with an
  OpenRouter key, use a decision model.
- ScaleDown accepts a `score` question with at most 10 levels.
- A decision model can be a self-hosted server that implements the OpenAI
  Decisions API.
- A Second Brain summarizer can be a self-hosted OpenAI-compatible chat
  endpoint.
- Belle does not limit the host of an OpenRouter entry for `summarize` and
  `extract`. Belle has no test with a self-hosted server for these two tools.

For the providers of each kind and the fields of an entry, see
[Task models](config.md#task-models). The
[example configuration](../examples/config/belle-config.yaml) shows each kind.
