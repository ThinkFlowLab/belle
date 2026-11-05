# Loopback platform

A platform that keeps its conversations in memory. It exists so that a
backend can be run and tested with no messaging service and no credential.
The backend conformance suite in `tests/conformance/` talks to the
conversation host through it.

Tests can give Loopback named conversations, audiences, and members. Belle uses those places to check scheduled delivery without a messaging service. See [Scheduled jobs](../scheduling.md).

It is a platform like any other. It has no credentials, so only
`enabled: true` turns it on:

```yaml
platforms:
  loopback:
    enabled: true
```

`belle_agent.platforms.base.create_platform` then builds it from that
section. No core module imports this package, and a test guards that.

## Control socket

A running loopback platform has no screen and no keyboard. With one setting
it listens on a local socket, so that a person can show a backend at work:

```yaml
platforms:
  loopback:
    control: /var/lib/belle/loopback.sock    # ${NAME} is read from the environment
```

```sh
export BELLE_LOOPBACK_CONTROL=/var/lib/belle/loopback.sock
python -m belle_agent.platforms.loopback dm U1 "hello"
python -m belle_agent.platforms.loopback channel C1 U2 "anyone seen the build?"
python -m belle_agent.platforms.loopback channel --mention C1 U2 "what broke it?"
python -m belle_agent.platforms.loopback log
```

`dm` and `channel` inject one message and print the host's admission
outcome. `log` prints every outbound action so far, one JSON object on each
line. A turn runs after its message is admitted, so its post is in a later
`log`. The socket carries messages in and the log out. It answers no
question and stops no turn. With no `control` setting the platform listens
nowhere.

Control messages use workspace `local`. The `dm` command registers the sender's
direct conversation as a readable delivery target for scheduled jobs. A control
DM keeps that address across a restart.

## What it does

- **Inbound.** `LoopbackPlatform.inject(record)` hands an `InboundMessage`, an
  `InboundAnswer`, an `InboundStop` or an `InboundEvent` to the host and returns the host's admission outcome.
  The caller writes the record: a direct message, a channel message, a
  mention, a thread reply, an edit, or a second delivery of the same ids.
- **Outbound.** Every action the host or a tool asks for is appended to
  `LoopbackPlatform.log` as an `Outbound`: `post`, `ephemeral`, `reaction`,
  `question`, `approval`. Silence is an empty log beside the host's own
  record that the turn ended (`ConversationHost.ended`). An action the host
  asked for carries the `workspace` the host named.
- **Events.** Four kinds, named as `delivery.events` names them: `reaction`,
  `pin`, `member` and `app_home`. A turn is told them as JSON under `[event]`
  and `[events]`.
- **Marks.** What the host says became of a message is appended to
  `LoopbackPlatform.marks` as `(message id, state)`. A mark is no outbound
  action and is not in the log.
- **A running turn.** Every progress event the host hands over is appended
  to `LoopbackPlatform.progressed` as `(message id, event)`, and how the
  host ended a turn's showing to `LoopbackPlatform.concluded` as
  `(message id, state, final)`. A reply that was handed over while it was
  written becomes the post of its final.
- **Files.** A message carries its files as `data:` URIs, and `fetch_files`
  returns them as they are.
- **Tools.** One toolkit provides `post_message`, `react_to_message`, and
  `read_file` in the shape of the Slack toolkits. Posts and reactions target
  the turn's conversation by default. A `chat_id` that names another conversation is refused
  before any action, unless a test has added its workspace and conversation to
  `LoopbackPlatform.confirmable`. A post into such a conversation is one the
  toolkit has a question about, so the host asks a person first.
  Reply IDs must belong to the destination conversation in the same workspace.
  A reaction target must belong to the originating conversation and workspace.
  Belle refuses an unknown or foreign message ID without logging a reaction.
  The toolkit returns the posted message ID and thread root.
  `read_file` returns a file a test put in `LoopbackPlatform.files`, as
  content beside its text.
- **Scopes.** It declares `ChannelCapabilities(channel="loopback")` with the
  chat types `dm` and `channel`, so scopes match it as they match Slack.

## What it does not model

Rendering, Block Kit, message splitting, rate limits, retries, history,
search, files held by reference, public conversations, streaming and status surfaces.
It checks no scope value beyond what the shared loader
checks. It is not a Slack simulator, and a check that needs Slack behaviour
belongs with the Slack connector.
