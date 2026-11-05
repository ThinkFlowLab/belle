# ACP backend (experimental)

`backend: acp` runs Belle's turns on an agent harness over the Agent Client
Protocol (ACP), version 1. It is experimental.

Belle's OpenCode and Docker Agent integrations stay the defaults for those
harnesses, and one key switches a harness between its integration and ACP: see
[Switch a harness](../config.md#switch-a-harness-between-its-integration-and-acp).
The [feature matrix](../feature-matrix.md#harnesses) compares the routes.

Belle connects to the Unix socket of a relay and speaks ACP over it. Belle
starts no process. The relay runs beside the harness and starts one agent
process for each connection. When Belle closes a connection, the relay ends
that process.

The backend needs the `acp` extra, which pins the ACP Python SDK. A profile
with `tools_mount: session` or `agent` also needs the `mcp` extra for the tool
server:

```sh
pip install 'belle-agent[acp,mcp]'
```

A profile with `tools_mount: none` needs only `belle-agent[acp]`. The launcher
image holds both extras. Without an extra that the profile needs, the launcher
refuses `backend: acp` and names the extra.

## The relay

The relay is `socat`. It listens on the socket and runs the agent command for
each connection, with its standard input and output as two pipes:

```sh
socat "UNIX-LISTEN:/run/belle-acp/agent.sock,fork,unlink-early,umask=077" \
  "EXEC:opencode acp,pipes"
```

Any process that can open the socket can use the model and the harness's
tools. These rules apply:

- The relay creates the socket with a umask of `077`, so only its own user
  can open it. Run Belle under the same user ID as the relay.
- Give the socket to Belle and to the relay only.

For a per-conversation connection, the agent command starts with
[`relay-entry.sh`](../../containers/acp-trial/relay-entry.sh). It reads the
first line of the connection, the conversation's binding, and starts the
agent with it in `BELLE_TURN`.

## Settings

The keys under `backends.acp`:

| Key | Role |
| --- | --- |
| `profile` | The name of the profile in `profiles` that this process uses. |
| `profiles` | The profiles, by name. Each one describes one harness behind one relay. |
| `tools` | `bind` and `port` of Belle's tool server. The default is loopback on a free port. `url` is the address the harness uses to reach it. |
| `tools_token` | Optional. The tool server's bearer token, as an environment reference only, such as `${BELLE_TOOLS_TOKEN}`. Belle refuses a literal value: see [Secret fields](../config.md#secret-fields). When the value is absent or empty, Belle draws a new token at each start. |

The keys of a profile:

| Key | Role |
| --- | --- |
| `socket` | The absolute path of the relay's socket. |
| `cwd` | The absolute working directory of each ACP session. |
| `connection` | `shared`: one connection holds the session of each conversation. `per_conversation`: each conversation gets its own connection, and so its own agent process. Its first line is the conversation's binding. |
| `tools_mount` | `session`: Belle mounts its tool server over HTTP in each new session, with its token. The agent must advertise HTTP MCP. `agent`: the harness's own configuration mounts the tool server. It reads the token from its environment, so `tools_token` is required. `none`: Belle offers the agent no tool and starts no tool server. Use it for an agent that can neither take a mount nor bind a call to a conversation. |
| `binding` | Required with a mount, and refused with `tools_mount: none`. How a tool call names its conversation, with one key. `meta`: the `_meta` key of the call that holds the ACP session ID, which needs `connection: shared`. `header`: the request header that holds the connection's binding, which needs `connection: per_conversation`. With `tools_mount: session`, Belle sends that header in the mount. |
| `tool_name_format` | How the harness names one of Belle's tools to the model, with `{name}` once. The default is `{name}`. |
| `start_timeout_seconds` | How long the agent may take to answer `initialize`, its start included. The default is 60. |
| `cancel_timeout_seconds` | How long a turn may take to end after a stop. Past it, the turn ends with an unknown outcome, and Belle leaves its session: see [Turns](#turns). The default is 10. |
| `permission_timeout_seconds` | How long a permission request waits for a person. Past it, the request is answered `cancelled` and nothing runs. The default is 900. |
| `remembered_requests` | How many requests the state file keeps. Belle detects a duplicate, and recovers an outcome, among these last requests only. The default is 1024. |

Each error names the key and the form it expects.

## Bindings

A tool call counts for a conversation only after its prompt is submitted and
before its prompt completes or cancellation starts. Calls during session
setup, replay or cleanup are refused. The same limits apply to permission
requests and answers.
A binding never comes from the model's arguments. Session updates and
permission requests belong to their ACP connection and session together.
Independent agent processes can reuse a session ID before Belle learns it
from `session/new`. To prevent a call from reaching another conversation,
`binding.meta` requires `connection: shared`. Use `binding.header` with
`connection: per_conversation`. With
`connection: per_conversation`, each agent process holds its binding in its
environment. Another process of the same user can read it, so the binding
counts only during its turn. Docker Agent binds a call only in this way, so a
deployment of Docker Agent uses its integration.

## Profiles

OpenCode, with `opencode acp` behind the relay:

```yaml
profiles:
  opencode:
    socket: /run/belle-acp/agent.sock
    cwd: /home/belle/work
    connection: shared
    tools_mount: session
    binding: {meta: ai.opencode/sessionID}
    tool_name_format: belle_{name}
```

OpenCode shows the tools of a session mount through its code-mode `execute`
tool only. A per-session header does not bind a call, because a later session
replaces the headers of an earlier mount of the same name. Belle binds each
call by its `_meta` session ID.

Docker Agent, with `relay-entry.sh docker-agent serve acp <agent file>`
behind the relay:

```yaml
profiles:
  docker_agent:
    socket: /run/belle-acp/agent.sock
    cwd: /home/belle/work
    connection: per_conversation
    tools_mount: agent
    binding: {header: X-Belle-Turn}
    tool_name_format: belle_{name}
```

Docker Agent refuses an HTTP session mount at a private address. The agent
file of [`containers/acp-trial/`](../../containers/acp-trial/docker-agent.yaml)
mounts Belle's tools with `Authorization: Bearer ${env.BELLE_TOOLS_TOKEN}` and
`X-Belle-Turn: ${env.BELLE_TURN}`. Docker Agent asks the client before each
call of a tool from that mount, and Belle shows the request as an approval.

JiuwenSwarm, with `jiuwenswarm-acp` behind the relay, and JiuwenSwarm's
Gateway running in the same container:

```yaml
profiles:
  jiuwenswarm:
    socket: /run/belle-acp/agent.sock
    cwd: /work
    connection: shared
    tools_mount: none
```

JiuwenSwarm's ACP agent advertises no HTTP MCP, and a tool call names no
session, so Belle offers it no tool. It advertises neither `session/load` nor
`session/resume`: after a Belle restart, a conversation starts a new session
without its earlier context, and Belle logs the loss. A permission request of
JiuwenSwarm's own permission engine does not reach Belle over ACP: the turn
ends, and the tool does not run. Belle's JiuwenSwarm integration, which runs
inside JiuwenSwarm, keeps these features.

## Turns

Belle sends the message text as one text block. A file goes as an image,
audio or embedded resource block where the agent advertises that kind. Belle
refuses a turn with a file of another type before it sends anything, and the
failure names the file.

The answer streams from the agent's message chunks. A new message, and text
or a tool call after a tool result, starts the reply again: the final text
is the text since then. Thought chunks are not part of the answer.

A turn ends once, by the agent's stop reason:

| Stop reason | End |
| --- | --- |
| `end_turn` | `succeeded`, with the final text. |
| `cancelled`, after a stop or a person's refusal of an action | `interrupted`. |
| Any other | `failed`, with the stop reason. |
| The connection closed before the answer | `failed`, outcome unknown. |

A failed turn's Belle log names the stop reason, or the code of the agent's
JSON-RPC error such as `-32603`, as `error_type`. It names the turn's ACP
session as `backend_session`, once the turn has a session. It omits the
error's message and data, because they can contain model output or
credentials. Find the cause in the agent's own record of that session.

A stop sends `session/cancel`. When the turn does not end within
`cancel_timeout_seconds` after a stop, it ends with an unknown outcome. Belle
then leaves its session, so that the late messages of that session reach no
later turn:

- With `connection: shared`, the next turn of the conversation starts a new
  session without the earlier context, and Belle logs the loss. A turn that
  must have its conversation, such as a continuation, fails. Belle answers
  a late permission request of the old session `cancelled`. A late tool call
  of the old session counts for no conversation.
- With `connection: per_conversation`, Belle closes the connection, and the
  relay ends the agent process.

A request ID among the last `remembered_requests` requests starts nothing
again in its conversation. An older request ID can run again.

## Permissions

A permission request of the agent becomes an approval question. Approve
selects the agent's `allow_once` option, and reject selects `reject_once`.
Belle refuses a request that offers no such pair. A stop, the end of the turn,
`permission_timeout_seconds`, or the agent's withdrawal of the request answers
it `cancelled`. An answer that comes after that runs nothing.

Belle offers the agent no file system and no terminal, and refuses those calls.

## Restarts

Belle keeps each conversation's ACP session and the last
`remembered_requests` requests in `acp_<profile>.json` in its state directory.
The file is readable by its owner only. It holds the final text of each of
these turns, except a turn that withholds its final. It holds no token,
binding or prompt. The limit counts requests in admission order, including running turns.
A dropped request stays dropped when its turn ends. Recovery reports a
request with no record as failed, with an unknown outcome.
An unreadable state file or invalid state structure also makes earlier outcomes unknown.
Belle logs the cause and starts with no recorded sessions or requests.

A Belle restart closes each connection, and the relay ends each agent
process. A turn that ran then ends. The next Belle process reports it as
failed, with an unknown outcome, and never runs it again. The next turn of the
conversation restores its session with `session/resume` or `session/load`,
where the agent advertises one. A turn that must have its conversation fails
when neither works. Any other turn starts a new session, and Belle logs the
loss of the earlier context.

## Limits

ACP v1 gives Belle no way to apply a scope's `tools`, `skills`, `subagents`,
`expert`, `model` or `system_additions` to a turn. Belle lists none of these
fields, warns at start about a setting that needs one, and still enforces
`deny` and `ask` on its own tools. Belle refuses a call at `ask`, because no
hook holds it for a person. The backend has no catalog, no steering, no
question of its own, no history peek, no session deletion, no health check and
no usage report. Belle deletes no session because the pinned SDK has no client
method for `session/delete`. With a connection for each conversation, each
agent process lives until Belle closes its connection. No idle limit ends it.

At start, Belle logs a warning that ACP reports no usage and has no health
check. Belle also logs a warning for each limit that the agent or the profile
adds:

- The agent advertises neither `session/resume` nor `session/load`, so a
  conversation loses its earlier context when Belle restarts.
- The agent does not take every file type, so Belle refuses a message with a
  file of another type.
- The profile opens one agent process per conversation, and no idle limit ends
  one.

## Containers

[`containers/acp-relay/`](../../containers/acp-relay/README.md) puts the relay
in front of a harness in the harness's own container. Belle runs in another
container and reaches the relay through a socket volume that only the two
containers mount. The harness container holds no platform credential.

[`containers/acp-trial/`](../../containers/acp-trial/Containerfile) builds one
container with Belle, the relay and one harness, Docker Agent or OpenCode. The
harness shares a user and a file system with Belle's credentials and state,
so this container is for a trial only.
