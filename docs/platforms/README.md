# Platforms

A platform is where conversations happen: a messaging service, or the
loopback platform that keeps its conversations in memory. A platform says
what arrived and performs what the conversation host decides. The host
decides who is answered and how.

`base.py` holds the platform port. Each platform has a section of the same
name under `platforms` in the configuration. A section turns its platform on:

| Section | The platform is on |
| --- | --- |
| Its credentials resolve, and `enabled` is not set | Yes |
| `enabled: false` | No, also with its credentials |
| `enabled: true` | Yes. A missing credential stops the start, and the error names it. |
| Only some of its credentials resolve | The start stops, and the error names each missing variable. |
| It has no credentials, such as `loopback` | Only with `enabled: true` |

Slack uses its token pair or a `workspaces` list. Mattermost uses `url` and
`MATTERMOST_BOT_TOKEN`, or a `servers` list; each enabled server turns the
platform on. Matrix uses `homeserver`, `user_id` and
`MATRIX_STORE_PASSPHRASE`. Discord uses `DISCORD_BOT_TOKEN` for one bot, or a
`bots` list whose `token_env` fields name each bot's token variable. When no
section is on, the start stops and the error names each section and why it is
off.

`create_platform` builds the connector of each section that is on, from the
package of that name under `belle_agent.platforms`. The host names no
platform, and the same host and the same backend run on each.

## Several platforms at once

One host serves every platform that is on, with one backend. Each record
names its platform. A reply, a mark, a question, a card, a stop and a file
read go to the platform of the record or of its turn. A stop or an answer
from another platform reaches no turn. The same conversation id on two
platforms gives two sessions.

Each platform keeps its own duplicate store, `allow_from`,
`allowed_channel_ids`, decision gate and scope section. A command surface
reads and changes only the conversations of its own platform.

A tool keeps one name on every platform, such as `post_message`. A turn's
tool list holds the tools of its own platform and the tools that every
platform shares, and a call runs the tool of the turn's platform. A call to
a tool that only another platform serves is refused with the code
`tool_of_another_platform`. The turn's scope also sets each such tool to
`deny`. A backend that removes denied tools from the model's tool list, such
as [OpenCode](../backends/opencode.md), then shows the model only the tools
of the turn. [Docker Agent](../backends/docker-agent.md) still shows them,
and its `tool_guard` hook refuses such a call with a reason in words.

The request id of a turn is the delivery id of its record. A backend keys
the requests it ran by conversation and request id, so two platforms that
give the same delivery id each get their turn.

| Package | Platform |
| --- | --- |
| `discord/` | A Discord bot application, over the REST API and the gateway. See [Discord](discord.md). |
| `loopback/` | Conversations in memory, for a backend run with no messaging service. See [Loopback](loopback.md). |
| `matrix/` | A Matrix homeserver, as one user account, through the helper process that holds its encryption. See [Matrix](matrix.md). |
| `mattermost/` | A Mattermost server, as a bot account, over the REST API and the WebSocket. See [Mattermost](mattermost.md). |
| `slack/` | Slack over Socket Mode. `connector.py` is the port on Slack. The rest of the package is the parts every Slack connection uses, on any harness. |

A platform package may export `validate_config(config, config_path)`. The loader
calls each validator after it places platform sections at the top level and
before it validates shared settings. Validators expand their own environment
values and resolve file paths relative to `config_path`.

## The port

| Member | Purpose |
| --- | --- |
| `start(on_inbound)`, `stop()` | Connect, and hand each inbound record to the host. |
| `InboundMessage` | One message. The platform says how it addressed the bot in `trigger`: a direct message, a mention, a reply to the bot, or none of these. The host decides what a trigger does. |
| `InboundAnswer` | A person's answer to a question the platform showed. |
| `InboundStop` | A person's request to stop the turn of one message. |
| `InboundDrop` | A person's request to remove a message that waits for its turn. |
| `bind(host)` | Take what the host answers a command surface. A platform with no commands keeps nothing. |
| `InboundEvent` | Something that is no message: a reaction, a pin, a change of members. It names its kind, who did it, the message it is about where there is one, and the record a turn is told. |
| `event_kinds`, `render_events(held, started_by)` | The kinds of event the platform reports, each with how many a conversation holds, whether it may start a turn, whether it is about a message and whether it waits. And the words a turn is told events in. |
| `post`, `react`, `ask` | Deliver text, a reaction, a question or an approval prompt. Each takes `workspace`, the workspace of the inbound record that led to it. A platform connected to several workspaces acts in the one named and in no other. `ask` raises `QuestionRefused` for a question the platform cannot show as it was put, with a message that names the limit: the host hands that reason to the turn where its backend can take it, and ends the turn with it otherwise. |
| `mark(inbound, state)` | Show what became of a message: received, queued, started, done, failed, stopped, refused or dropped. |
| `fetch_files(inbound)` | Load the files of a message. The host asks once the message has a turn or is selected for ingest, and once for a message with both. |
| `toolkits(metadata_provider)` | The platform's tools, each reading the request in flight. |
| `request_metadata(inbound)` | The metadata those tools read for one turn. |
| `posting_tool` | The tool a turn speaks with. Its result tells the host which messages it posted, and where. A post is the turn's reply only in the conversation the turn came from. The comment on `posting_tool` in `base.py` gives the fields of that result, and the forms that count. |
| `capabilities`, `public_chat_types` | What scopes may match and set, and which chat types are open to a whole workspace. |

### A running turn

Four more members are optional. A platform that shows nothing of a running
turn has none of them, and the host asks it for nothing.

| Member | Purpose |
| --- | --- |
| `shows` | Each kind of progress event the platform shows as configured, with the setting that turns it on. |
| `begin(inbound)` | A turn owed a reply has started; a platform may leave it out. A platform whose stop control is on the activity card starts the card here, so a turn that calls no tool can be stopped too. The host does not call it for a turn whose reply is optional, which posts only through a tool. |
| `progress(inbound, event)` | One event of a kind the platform shows: `text_delta` and `step` for the reply as it is written, `tool_called` and `tool_result` for what the turn is doing. |
| `conclude(inbound, state, final)` | End what was shown, once, before the message is marked. `state` is `done`, `failed`, `stopped`, or `silent` for a turn that said nothing where no reply was owed. `final` is the reply the host is about to post. A platform that was showing that reply ends it as `final` and returns true, and the host then posts nothing. |

A backend declares the kinds it yields in `BackendCapabilities.reports`. The
host hands over a kind only where the platform shows it and the backend
reports it, and names at start a setting whose kind the backend does not
report.

The host decides what is shown from its delivery rules:

- A reply is shown as it is written only while its final is what will be
  posted. Under `delivery.reply: optional` the final is withheld, so no
  delta is handed over. The host knows this when the first delta arrives,
  because whether a reply is owed is settled when the message is admitted.
- Once the posting tool has posted, no further delta is handed over, and
  `conclude` carries no final. What the platform showed is then no part of
  what the turn said.
- The deltas since the last `step` are the reply so far. A `step` starts the
  reply again: text a turn writes before a tool call is no part of its
  final.

Four fields of `InboundMessage` hold what only a platform knows.

- `reply_thread` is where an answer goes, where the platform's own placement
  rule says other than the message's thread.
- `envelope` is what the platform adds to the turn's text, such as the ids
  its tools take.
- `files` may hold content by reference. The platform loads it in
  `fetch_files`, so nothing is downloaded for a message nobody answers.
- `encrypted` says that the conversation is end-to-end encrypted. The second
  brain then ingests from it only where its ID is selected.

- `side` names a conversation of its own beside the one the message sits
  in: a question put privately from elsewhere, and its follow-ups.
- `skills_required` and `subagent_required` are what a person's command
  requires of this one turn. The host refuses the message where the scope of
  its place does not allow them, or where the backend cannot apply them.

## Commands

A platform may give people commands of its own. `belle_agent/host/commands.py`
holds what a command asks of the host, so a command means the same on every
platform and backend.

| Operation | Answer |
| --- | --- |
| `running`, `waiting` | The turns that run and the messages that wait in one conversation. |
| `may_stop`, `may_drop` | Whether a person may stop a turn or remove a waiting message. `InboundStop` and `InboundDrop` are held to the same rule. |
| `model_choices` | The model a person chose for a conversation. A scope's `agent.model_name` comes first. A restart clears every choice. |
| `catalog` | What the backend lists, or nothing where it does not answer within 8 seconds. |
| `backend_capabilities` | The catalog kinds the backend lists and the scope fields it applies. A command that needs another says so. |
| `settled`, `scoped_model`, `mid_turn` | What the scopes settle for a person in a place. |
| `admits`, `serves` | `allow_from` and `allowed_channel_ids`. |

A stop is an `InboundStop`. A private turn is an `InboundMessage` in the
person's direct message. Both pass the host's receiver, so admission, the
scopes, the queue and the bounds of a turn apply to them.

The sender of a waiting message may remove it. Anyone else needs a
`clicks.queue_drop` rule that names them. Whoever started a turn may stop
it. Anyone else is held to `allow_from` and to `clicks.stop`.

The backend session keeps the message of a stopped turn. So the host adds
one line to the next message of that conversation. The line names the
stopped message and tells the model not to answer it unless a person asks.
A newer message under `delivery.mid_turn: cancel` stops a turn in the same
way. The next turn takes the line once.

The host keeps these lines, and the held-call lines below, in its state
directory, in `turn_notes.json`. So a restart does not drop them. The file
keeps the lines of at most 64 conversations of each kind, each line at most
600 characters. The host ignores a file that it cannot read and logs its
path and the fault. Where it cannot write the file, it logs that once and
keeps the lines in memory only.

While a turn runs, the host keeps a record of it in `attached_sessions.json`
in its state directory, until the turn ends. A host that stops keeps the
record of each turn that runs. The record holds the message that started the
turn. It holds no history. With
[`attached_sessions.expire_after_idle_hours`](../config.md#attached-sessions), the
record stays after the turn with the time that the turn ended, for the idle
expiry of the attached session. At start and every 30 seconds, the host
looks for the record of a turn from a process that stopped. A process on
another host counts as stopped 90 seconds after its last heartbeat. The host
acts by backend:

- A backend that can recover a turn, such as OpenCode, finishes the turn,
  and the host delivers its answer once.
- A backend that lists its running turns and does not list the turn ran it
  in the stopped process, such as Docker Agent. The turn ended there, and
  the host logs it.
- Another backend gets a stop request for the turn.
- The host logs a turn whose answer it was delivering when it stopped, and
  does not deliver it again.

Except where the backend finishes the turn, the turn's answer is lost. The
host then ends the turn as failed once its platforms start. It posts a
notice where the answer would have gone, which asks the person to send the
message again, or says that part of the answer may be missing, and marks
the message as failed. A turn whose reply is optional gets the mark only. A
turn that an event started has no message to tell, and the host only logs
it.

The host looks before its platforms start, so a message that arrives at the
start waits for a recovered turn. A message on the key of a turn that
another process runs, or that a stopped process left, waits in the queue
until a later look settles that turn. A turn that a look cannot settle,
because the backend or the store fails, stays for the next look.

When the host stops, it tells the backend first, and then leaves the stream
of each running turn. OpenCode continues each turn, and the next host delivers
its answer once, as after a crash. A Docker Agent run ends with its stream
after a stop and after a crash. This is a limit of Docker Agent. A stop of
one turn by a person still ends the turn.

The turn timeout counts from the turn's start, so the next host stops a
recovered turn that reaches it, and the turn fails. The turn budget does not
apply to a recovered turn. The host counts the steps and the cost in the
events of the turn's stream, and a backend's recovery check reports only
whether the request runs or how it ended.

A recovered answer is a new message. On a platform that streams a reply,
the message that the stopped process streamed stays as it was. The state of
that stream was in the stopped process, and the record does not keep it.

## What the host decides

The host reads two settings from a platform's own section, whatever the
platform:

| Setting | Meaning |
| --- | --- |
| `allow_from` | Who may ask for a turn, answer a question or stop somebody else's turn. Empty admits everyone. |
| `allowed_channel_ids` | The shared conversations served. Empty serves every one. A scope written for one conversation opts it in. A direct message is not held to the list. |

It also decides the triggers of `delivery.mode` (`mention`, `reply`, `all`,
`url`, `has_file`), duplicates, the queue, the conversation a message belongs to,
whether a reply is owed, and who may answer or stop.

Under `delivery.mid_turn: steer`, a message that arrives during a turn of
its conversation joins that turn, where the backend declares the `steer`
operation. The message joins only when all of these are true: its sender may
stop the turn, it has no file, no message waits before it, and the scopes
give it the same turn settings as the running turn. Otherwise it waits for a
turn of its own, as under `queue`. The platform marks it `steered`, and then
marks it as the running turn ends. A backend without `steer` queues the
message. If the backend took the message but the turn ended before it read
it, the message runs as the next turn of its conversation, before the
messages that wait. The platform then marks it `queued`, `started` and
done, as a message that waited.

It decides what becomes of an event from `delivery.events`: nothing,
context for the next turn of the conversation the event happened in, or a
turn of its own (`host/events.py`). Context is kept under that conversation
and told once, so no turn elsewhere is told it.

When Belle selects a decision model, the host puts each admitted
message to the decision gate (`host/decisions.py`, `message_decisions.py`)
and logs the answers, or tells them to the message's turn when the
platform's `decision_forward` is on. The answers decide nothing. A platform
may add `decision_facts(inbound)` and `recent_messages(conversation, limit,
before)` to say more of a message than its record holds.

## Tool results with content

A tool returns text, or a `backend.ToolContent`: its text and parts that are
not text, each a `ContentPart` with a media type and bytes. A backend says
which types it hands to the default model in `BackendCapabilities.tool_content`
and to named models in `tool_content_by_model`. The host reads the relevant
types for each turn.

The tool host returns a result whose every part the backend takes. It
refuses any other with `tool_content_not_passed`. The refusal names the
type, names the types the backend takes, and keeps the tool's own text in
`result_without_content`. A model is therefore never told that a file was
read when its content went nowhere. A backend that declares no type gets
text and that refusal only.

`tools/server.py` renders a part as the MCP block of its kind: `image`,
`audio`, or an embedded `resource` with a `blob`. The loopback platform has
`read_file`, which returns a file a check gave it, so the conformance suite
holds every backend to this.

## Held calls

A toolkit may have a question about one call: `widening_question(name,
arguments)` returns what a person must agree to before that call runs. The
tool host asks it before every call of that toolkit. The host then does
four things, on every platform and every backend.

1. It registers the held call before `Platform.ask` shows its approval
   controls in the conversation the turn came from. An answer can arrive
   before the platform finishes asking.
2. It answers the tool call at once with `awaiting_confirmation`. The call
   is not kept open. The turn goes on and ends.
3. When a person who may answer agrees, it makes the call once, with the
   arguments and the request metadata of the turn that made it. When they
   refuse, it makes no call.
4. It tells the conversation's next turn what was decided, in one line of
   that turn's system additions. A restart keeps that line, as the line of
   a stopped turn above.

Who may answer is the rule of every approval: `allow_from`, then a
`clicks.approve` rule, and with no rule anyone in the conversation. A held
call stays answerable for 3,600 seconds (`CONFIRMATION_TIMEOUT_SECONDS`, the
time one turn may take). A later answer sends nothing. A turn that is
stopped takes its held calls with it. Held calls are kept in memory, so a
restart drops them and a click on the old message takes its buttons off.

If `Platform.ask` raises before an answer arrives, the call is refused
with the toolkit's sentence. A failed or cancelled ask removes its pending
call and withdraws its controls. An answer already accepted remains valid
if the ask later raises. Existing approvals stay pending when the new ask
fails; capacity eviction follows a successful ask. A call that expires
before the ask finishes is refused.

A turn that ends with a call held ends as any finished turn. What the
platform showed of it is concluded then: on Slack the streamed reply
becomes the final and the activity card is settled as "Turn finished". The
card counts the held call as finished and not as failed. The call made
later, on approval, is shown on no card.

The wait is the host's own for one reason. A tool call cannot wait for a
person on every backend: an MCP call into OpenCode fails after about 357
seconds. A scope's own `ask` level is held by the backend's permission
state, because the scope is known before the call. A toolkit's question
depends on the arguments of one call, so no backend can be told of it in
advance.

## Connectors

The [feature matrix](../feature-matrix.md#platforms) compares what each
platform supports.


- [Slack](slack.md) connects through Socket Mode.
- [Loopback](loopback.md) keeps conversations in memory for tests and local runs.
