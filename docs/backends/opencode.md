# OpenCode backend (experimental)

Belle can run model turns on an OpenCode v2 server. Belle owns conversation
admission, scopes, delivery, platform tools, and second-brain retrieval.
OpenCode runs the model, stores its sessions, and provides its built-in tools.
The processes communicate over HTTP.

The adapter is pinned to OpenCode **2.2.5**. It checks the server version at
startup and refuses a different version. The experimental MCP mount and
session-instruction endpoints, plugin hooks, and permission names must be
checked before changing the pin.

## Start

For a trial, [`compose.yaml`](../../containers/opencode/compose.yaml) starts both
containers with one command, `podman compose up -d` or `docker compose up -d`.
Build the images from a clone for a local trial. See
[One command](../../containers/opencode/README.md#one-command).

Use the [two-container guide](../../containers/opencode/README.md) for a quick
Slack deployment. For an existing OpenCode server, start Belle with a config
that selects `backend: opencode` and turns on a platform section, such as
`slack` or `loopback`:

```sh
export OPENCODE_SERVER_PASSWORD=replace-me
python -m belle_agent.launcher --config belle.yaml
```

The server needs a configured model and a route back to Belle's tool server.
See the [configuration guide](../config.md) for `backends.opencode` keys and the
[loopback guide](../platforms/loopback.md) for testing without Slack credentials. Keep
provider credentials in OpenCode's environment. Keep its server password in
`OPENCODE_SERVER_PASSWORD`, outside Belle's YAML.

```yaml
platforms:
  slack:
    enforce_audience: true
backend: opencode
backends:
  opencode:
    url: ${OPENCODE_URL}
    tools:
      bind: 0.0.0.0
      port: 18300
      url: ${BELLE_TOOLS_URL}
    relay:
      bind: 0.0.0.0
      port: 18200
```

`url` names the OpenCode server. `tools.url` is the address OpenCode uses to
reach Belle's `/mcp` endpoint. The HTTP `Host` header must match its host and
port. Set `tools.url` when `tools.bind` is `0.0.0.0` or `::`. Omit it when both
processes share loopback. The relay endpoint is needed only when OpenCode loads Belle's plugin. Set
`BELLE_RELAY_URL` in that process to point at Belle's relay endpoint. A
`namespace` under `backends.opencode` separates derived session IDs when
deployments share an OpenCode server. `username` is the user name that goes with
the password, `opencode` by default. `ready_timeout_seconds` is how long Belle
waits for the server at start, 120 by default.

The optional Belle relay plugin lives in
`src/belle_agent/integrations/opencode/plugin/relay.ts`. Without it, Belle uses
OpenCode session instruction entries. Changes to instructions after the first
turn can enter stored user-role history. With the plugin, Belle supplies the
current turn's instructions through a system-message hook. Set the same
`BELLE_RELAY_TOKEN` in both processes when using the plugin. Keep the relay
endpoint reachable only by OpenCode. The plugin sends each file's length to the
relay endpoint, not its contents, so the plugin adds no limit on file size. The
endpoint refuses an event over 8 MiB with HTTP 413. When a hook fails a turn,
the turn's error names the endpoint's refusal, or says that the endpoint
refused nothing.

OpenCode refuses a file over 20 MiB. It scales down an image over its own
image limits, 5 MiB by default, and can send it to the model as JPEG.

## Tool access

OpenCode mounts Belle's tool server for the whole OpenCode server, not for one
conversation. Its built-in file and shell tools run as the OpenCode server
user. A process with those tools can reach other sessions, the provider key,
and the tool-server token stored by OpenCode. One OpenCode server must
therefore contain only conversations in the same trust domain when such tools
are allowed. For shared deployments, deny the built-in file, shell, web,
subagent, and Code Mode tools with `deny_tools`:

```yaml
backends:
  opencode:
    deny_tools: [
      shell, execute, edit, read, grep, glob, webfetch, websearch, subagent, external_directory,
      opencode_session_rename, opencode_session_move, opencode_worktree_create, opencode_worktree_list,
      opencode_models, opencode_list_mcp_resources, opencode_read_mcp_resource,
    ]
```

`deny_tools` lists OpenCode tool or permission names. Belle refuses a name
that it does not know. The refusal names each such name and lists the names
that Belle accepts. By default Belle denies none of them. A scope's
`permissions.tools` that sets a level for one of these tools replaces the deny
for its conversations.

The Code Mode tools, such as `opencode_worktree_create`, are not in the
model's tool list. The model calls them inside `execute`. A deny on `execute`
does not deny them: OpenCode then offers them as direct tools. A deny on the
name of one Code Mode tool denies that tool alone, except for
`opencode_worktree_remove`. One permission,
`edit`, gates `edit`, `write` and `opencode_worktree_remove`, so a level on
any of these three tools applies to all three. OpenCode offers `todowrite`
only when its `experimental.todo` setting is on.

OpenCode never asks for approval of the tools below, so it applies only a
`deny` to them. Belle warns at start about any other level on one of them,
because that level enforces nothing.

- `execute`
- `opencode_session_rename`
- `opencode_session_move`
- `opencode_worktree_create`
- `opencode_worktree_list`
- `opencode_models`

`opencode_session_rename` and `opencode_session_move` accept any `sessionID`
and never ask for approval. So a model can rename or move the session of
another conversation. A deny on both names prevents this.

A tool that a scope denies is not in the model's tool list for that turn. On
a host with several platforms, each tool that only another platform serves is
denied, so the model sees only the tools of the turn's platform.

A tool result can hold media for the model. OpenCode gives the model only
PNG, JPEG, GIF, WebP and PDF media, and Belle passes only those types to a
model whose input includes them. Belle refuses media of any other type, and
the refusal names the types that it accepts.

The backend supports per-turn model and tool selection, required skills,
available and unavailable skills, questions, approvals, and interruption. A
question that the platform cannot show, such as one with more options than
the platform shows, is cancelled with the reason, and the model reads it as
the result of its question tool. An approval that cannot be shown is
rejected with the reason. A
skill that `agent.skills.available` does not list, or that
`agent.skills.unavailable` names, is not in the model's skill list for that
turn, and OpenCode refuses a call that loads it. The [scope guide](../scopes.md) describes
the shared rules. A setting that the OpenCode backend cannot apply produces a
warning at startup. The [feature matrix](../feature-matrix.md#harnesses)
compares the harnesses.

## Continued responses

OpenCode continues a response that reaches the output token limit:

- If the response called no tool, or only tools that the provider runs
  itself, OpenCode tells the model to go on without repeating itself. Belle
  joins the text of the next response to the cut text with no separator,
  because a cut can fall in the middle of a word. A streamed reply runs on
  with no paragraph break.
- If the response called a tool, OpenCode does not prompt the model. The next
  response answers the result or the failure of the call, and Belle reads the
  cut response as one that called tools. OpenCode may not run the calls of a
  cut response. With an OpenAI-compatible provider, it runs none of them.

After three responses in a row that end at the output limit, with or without
a tool call, OpenCode fails the execution. The turn fails with OpenCode's
message after the cut text.

OpenCode also continues a response that stops after it starts to answer, for
example a stream that ends with no finish reason or with a finish reason that
OpenCode does not know. OpenCode fails the response and keeps its text. After
its retry wait, OpenCode tells the model to go on without repeating completed
content. Belle reads these parts as it reads a cut response and its
continuation.

A turn can fail after the model wrote text, for example when OpenCode makes
no more retries. The reply then holds that text, and a line that starts with
"The model's response stopped:" gives OpenCode's message. The text of a
response that called a tool is not in the reply. A turn that fails before the
model writes text shows OpenCode's message alone.

## Second brain

Belle ingests and recalls in its own process. Belle reads files, extracts
pages, and calls the summarizer and embedding endpoints directly.
The index is the same as on JiuwenSwarm: SQLite full text with BM25 ranking,
and a vector for each chunk when `second_brain.retrieval.use_embeddings` is on
and `second_brain.retrieval.embeddings` names an endpoint. Embeddings are off
by default. See the [configuration guide](../config.md#second-brain).

## Lost connections

Belle reads OpenCode's events on one long-lived connection. OpenCode 2.2.5
sends a heartbeat on it every 15 seconds. When nothing arrives for 45 seconds,
Belle closes the connection and opens a new one. A dropped network can stop
the connection without an error, so this limit is the only signal. After it
connects again, Belle reads each running turn from OpenCode. A turn that
continued on the server continues in Belle. A turn that ended meanwhile ends
with the result that OpenCode stored. A turn that OpenCode no longer runs
ends with a failure.

A failed turn's Belle log names the backend, a broad failure kind, and a
numeric HTTP status when OpenCode supplies one. It also names OpenCode's error
type, such as `provider.invalid-request` or `output-limit`, as `error_type`,
and the turn's OpenCode session as `backend_session`, once that session
exists. It omits the error message because that text can contain model output
or credentials. Find the cause in that session's messages in OpenCode.

OpenCode does not replay missed SSE events. After a gap, Belle stops forwarding
live `session.text.delta` for that turn (parent and routed child sessions alike)
so later deltas are not shown as if they continued earlier text. The final reply
is read whole from stored session messages. Live peek catch-up
(`peek_history`) can seed text OpenCode has already stored for the prompt and
for active child sessions; anything that existed only as deltas during the gap
is gone.

## Belle restarts

When Belle stops, it leaves each running turn to OpenCode, which continues it.
The next Belle process recovers the turn, and delivers its answer once. A
detached session run continues in the same way. Between the two processes,
no Belle serves the turn's tool calls or questions. A stop of one turn by a
person still interrupts it. The turn timeout counts from the turn's start,
and the next process stops a recovered turn that reaches it. The turn budget
does not apply to a recovered turn: Belle counts the steps and the cost in
the turn's events, and the recovery check of OpenCode reports only whether
the request runs or how it ended.

## Subagent child sessions

A `subagent` tool call runs in a child session. Its text and tool events arrive
on the same global event stream under the child's `sessionID`. Belle keeps one
SSE subscription: once `session.tool.progress` or a background
`session.tool.success` names the child, later child events are routed onto the
parent turn with `call_id` set to the parent subagent call so Activity peek can
attribute them to the child row. Belle does not open a second live subscribe
for the child.

## Harness health

Background samples call `GET /api/model/default`, then `GET /api/mcp` for the
Belle mount, then use the live event subscription flag. Before start, health
is `disconnected` with MCP `unknown` (`not_started`). Unreachable servers and
authentication failures report `disconnected` with MCP `unknown`
(`backend_unreachable`); the MCP probe does not run when the model probe
fails. A reachable server with no named default model, or without a live
subscription, reports `degraded`.

When the model and stream are otherwise ready, these MCP outcomes degrade
the sample with the named reason: `mcp_mount_missing`, `mcp_mount_failed`,
`mcp_mount_disconnected`, `mcp_mount_disabled`, or `mcp_mount_needs_auth`
(including OpenCode `needs_client_registration`). An unrecognised mount
status keeps overall `ready` and sets MCP to `unknown`
(`mcp_mount_not_connected`).

Probe timeout, HTTP error, or malformed `/api/mcp` leaves harness state
unchanged and sets MCP to `unknown` (`mcp_probe_timeout` or
`mcp_probe_failed`). The probe never registers or reconnects a mount.

## Checks

Belle can schedule turns on OpenCode. Each detached session keeps its OpenCode session until the detached session expires. Then Belle deletes the OpenCode session with its child sessions. A detached session can be continued until it expires. The expiry of a detached session never deletes an attached session. See [Scheduled jobs](../scheduling.md).

Run the ordinary Python tests from the repository root. The integration
checks under `tests/opencode/` run in the test image beside an OpenCode
container, and skip without it. The [test image guide](../../containers/test/README.md)
gives the commands.
