# Docker Agent backend (experimental)

Belle can run model turns on a `docker-agent serve api` server that loads
Belle's agent YAML. Belle owns conversation admission, scopes, delivery,
platform tools and second-brain retrieval. Docker Agent runs the model and
stores its sessions. Docker Agent reaches Belle's tools over MCP. The relay
hooks of the agent YAML reach Belle's hook endpoint.

## Start

For a trial, [`compose.yaml`](../../containers/docker-agent/compose.yaml) starts both
containers with one command, `podman compose up -d` or `docker compose up -d`.
Build the images from a clone for a local trial. See
[One command](../../containers/docker-agent/README.md#one-command).

Use the [two-container guide](../../containers/docker-agent/README.md) for a
quick Slack deployment. The same two containers run Belle on Mattermost, with
the `belle.env` of the [Mattermost guide](../platforms/mattermost.md#quick-start-with-containers), and on Matrix, with the `belle.env` and the
two volumes of the [Matrix guide](../platforms/matrix.md#quick-start-with-containers). On Matrix, a
question of Belle's is a poll. Belle does not support the `user_prompt` toolset of Docker Agent yet, so the shipped agent has no
tool that asks a question, and only an approval of a tool call is a poll there. The image in `containers/docker-agent/` pins a Docker
Agent release and holds the agent YAML and Belle's skills. For a server of your
own, load `src/belle_agent/integrations/docker_agent/agent/belle.yaml`, and
start Belle with a configuration that selects `backend: docker_agent`:

```sh
export DOCKER_AGENT_AUTH_TOKEN=replace-me
export BELLE_RELAY_TOKEN=replace-me
python -m belle_agent.launcher --config belle.yaml
```

The [loopback example](../../examples/config/belle-docker-agent-loopback.yaml)
runs with no messaging service and no platform credential.

## Settings

The keys under `backends.docker_agent`:

| Key | Role |
| --- | --- |
| `url` | The http address of the server, with no credentials. |
| `namespace` | Optional. Separates the sessions of deployments that share one server. |
| `tools` | `bind` and `port` of Belle's tool server. The default is loopback on a free port. Set `url` to the address the server uses to reach `/mcp`. A wildcard bind (`0.0.0.0` or `::`) requires `url`. The HTTP `Host` header must match its host and port. The agent YAML reads that address from `BELLE_TOOLS_URL` in the server's environment. |
| `relay` | `bind` and `port` of Belle's hook endpoint. The agent YAML reads its address from `BELLE_RELAY_URL` in the server's environment. |
| `max_request_bytes` | Optional. The largest request the server takes, as its `--max-request-size` sets it. The default is the server's default, 1048576. The example reads `DOCKER_AGENT_MAX_REQUEST_BYTES`, which the image also reads, so one value sets both. |

Belle reads two secrets from its environment and from nowhere else. `DOCKER_AGENT_AUTH_TOKEN` is the
server's bearer token. `BELLE_RELAY_TOKEN` is the token of the agent YAML's relay hooks. Set both to the same
values in the server's environment. Belle refuses to start when either is missing, or when the configuration holds either.

## Sessions and stops

Each conversation is one session on the server, titled from `namespace` and the conversation. Belle keeps
each conversation's session, and the request ids that ran, in two files in its state directory:
`docker_agent_sessions.json` and `docker_agent_requests.json`. A request id that ran in a conversation
starts nothing there after a restart. The same request id in another conversation is another request. Without the sessions file, Belle finds the sessions again by their titles. The server reads every
message of every session for that list, so Belle asks for it only then.

The server can lose a session that the file names, for example when its database is reset or replaced.
When the server refuses a turn's first calls, Belle asks the server for the session. If the session is gone,
Belle runs the turn in a new session and logs a warning. The model then has none of the earlier messages
of the conversation. A continuation of a lost session still fails, because it asks for that session only.

Under `delivery.mid_turn: steer`, Belle sends a message that joins a running turn to the session's
steer queue. The server gives it to the model before the next model call of that turn. The agent YAML relays
`user_steering_messages_submit`, so the turn's system text stays in the model calls after it. An agent YAML
whose entry agent or experts do not relay that hook does not steer: the message waits for a turn of its own.
A message that arrives after the last model call of the turn stays in the session's steer queue. When the
turn ends, Belle reads the depth of that queue. The last messages it steered, as many as the depth, then run
as the next turn of the conversation, with a reply of their own. Belle does not send them again in that turn:
the server gives them to the model from its queue.

A stop closes the turn's stream, which stops the turn in the server. So does the end of the Belle process,
also when a signal kills it. This is a limit of Docker Agent: the server runs a turn in the context of the
HTTP request that streams it, and ends the turn when that request closes. See `RunSession` in
`pkg/server/server.go` and `pkg/server/session_manager.go` of Docker Agent 1.152.0. So the next Belle process
delivers no answer for a turn that ran when Belle stopped. It posts a notice that the answer is lost, and
marks the message as failed. See [A running turn](../platforms/README.md#a-running-turn). A turn whose stream
breaks, for example when the server restarts, fails once with a reply that says so. The next turn of the
conversation runs in the same session.

A failed turn's Belle log names the code of Docker Agent's error, such as `hook_blocked`, as `error_type`, and
the turn's Docker Agent session as `backend_session`, once the turn has a session. It omits the error message,
because that text can contain model output or credentials. Find the cause in that session in Docker Agent.

The server keeps the message of a turn that ends before its answer. Some providers join that message to the
next one, and the model then reads the two as one request. So `docker_agent_sessions.json` also names each
conversation whose last turn did not succeed, from before that turn runs, so a turn cut short by the end of
the Belle process counts too. Before the next turn of such a conversation, Belle reads the session. When it
ends on a person's message, Belle adds an answer to it in the session that says the turn was interrupted.
A sessions file without that record, rebuilt from titles or written by an older Belle, names every
conversation, so each session is read once.

## Files

The files of a message go to the model in the turn request, with their content. Text files always reach the
model. An image or a PDF reaches a model that declares `capabilities` with `image: true` or `pdf: true` in the
agent YAML's `models`. This also applies to the images and PDFs that Belle's tools return. A model with no
`capabilities` reads text only. Belle does not send a file of another type, and tells the model its name. Belle
refuses a request larger than `max_request_bytes` and submits no prompt. The error names the request size
and limit. Reduce the message or files, or increase both `backends.docker_agent.max_request_bytes` and the
server's `--max-request-size`. Docker Agent replaces a text result
of a tool that is larger than 50 KiB or 2000 lines with its end and a notice. The model can then ask for less,
for example with a smaller `max_chars` of `brain_read`.

## Tool parameters

Belle lists a tool parameter as required only when the tool needs it. For a model of an OpenAI type, Docker
Agent changes each tool schema before it sends it: every parameter becomes required, and each optional
parameter also accepts `null`. No setting turns this off. Docker Agent drops a `null` argument before it
calls Belle, so `null` means "omitted". A model that writes the text `"null"` in an optional parameter gets a
refusal from Belle, because that text is not a valid value.

## Scopes

The [feature matrix](../feature-matrix.md#harnesses) compares the harnesses.
A scope applies on this backend as follows:

- `permissions.tools`: each tool at `deny` or `ask` becomes a session permission of that kind. Belle also
  checks every tool call, Docker Agent's own tools included, through the agent YAML's `tool_guard` hook. A
  call of a tool at `ask` waits for a person. Belle asks in the conversation of the turn, and the `clicks`
  rule says who can answer. An approval runs the call. A refusal tells the model that a person refused the
  call, and the turn continues. The call waits on the open stream of the turn, for as long as the turn can
  run. If the stream breaks while the call waits, the turn fails and the call does not run.
- Tools of another platform: on a host with several platforms, the scope of a turn sets each tool that
  only another platform serves to `deny`. The model still sees these tools listed, because a session
  permission does not change the tool list of the model request. The list comes from the toolsets of
  the agent YAML. The `tool_guard` hook refuses a call to such a tool before it reaches Belle's tool
  server, so the model reads the hook's reason in place of the code `tool_of_another_platform`:
  `<tool> is a tool of <platform>, and this conversation's turn runs on <platform>`.
- `agent.skills.available`, `agent.skills.unavailable` and `agent.subagents`: a skill or a subagent
  that the scope does not let a turn use is refused when the model asks for it. The model still sees it listed. Belle decides the tool calls of a
  subagent under the scope of the turn that gave it its task.
- `agent.model_name` and `/belle models`: the name of a model in the agent YAML's `models`. Every turn names
  its model, so a model chosen for one turn does not stay for the next.
- `agent.expert`: an agent of the YAML that the entry agent `root` routes to. The YAML lists it in
  `routing.allowed_agents` of `root`, and relays `before_agent_run` from `root`.

## Agents and models

The agent YAML declares the models, the experts and the subagents. To declare more, copy
`src/belle_agent/integrations/docker_agent/agent/belle.yaml`, add them, and mount the directory that holds the
copy, as `belle.yaml`, on `/etc/docker-agent` in the server's container. Each added agent needs the same hooks
as `root`. Belle refuses to start when the entry agent, an expert or a subagent does not relay the hooks it
needs. A subagent gets no system text of the turn: Docker Agent sends its prompt hook for the person's
message only.

Every hook of the agent YAML runs the same relay command under `sh`, which posts the hook's event to
`BELLE_RELAY_URL` with curl. When the relay fails, the command exits 2 with curl's reason, and Docker Agent
blocks the hook with it: `The relay to Belle failed: curl: (7) Failed to connect to belle port 18200 …`.
A blocked prompt hook ends the turn before the model is asked, and the reply says that reason. Where
Belle's hook endpoint refused a hook while the turn ran, the reply names that refusal on a second line,
because curl gives only the HTTP status. A blocked `tool_guard` refuses the call, and the model reads the
same reason. An agent YAML of your own copies the relay command unchanged.

## Harness health

Background samples use authenticated `GET /api/ping`. Before start, health is
`disconnected` with MCP `unknown` (`not_started`). Once started, MCP is
`unknown` (`mcp_status_unavailable`); Docker Agent has no read-only status for
the active `belle` toolset. The sample does not probe Belle's MCP listener.

## Trust domains

Belle can schedule turns on Docker Agent. Each detached session keeps its Docker Agent session until the detached session expires. Then Belle deletes the server session and its entry in `docker_agent_sessions.json`. A detached session can be continued until it expires.

Docker Agent cannot recover a turn after a Belle restart, so a running run ends as `interrupted` (see [Sessions and stops](#sessions-and-stops)). The expiry of a detached session never deletes an attached session. See [Scheduled jobs](../scheduling.md).

The shipped agent has no shell or file tool. A toolset that adds one can read
the server's environment, which holds the provider key and the relay token,
and every session on the server. Run one server per trust domain, or deny
those tools in a scope. `--session-workingdir-root` confines the working
directories of sessions. It is not a security boundary.
