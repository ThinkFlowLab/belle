# Use Belle with JiuwenSwarm

Belle plugs into JiuwenSwarm through application extensions and its YAML
config. It replaces the built-in Slack connector when those extensions load.
Optional extensions add [Mattermost](#mattermost-channel-experimental) and
[Matrix](#matrix-channel-experimental) channels. Each stays off until linked and enabled.
Gateway events must carry the session ID of their request. Belle matches both
the session ID and request ID before it delivers an event to a conversation.

## Build branches

Use the matching public `belle/20261013` branches:

- [JiuwenSwarm `belle/20261013`](https://github.com/harenome/jiuwenswarm/tree/belle/20261013)
- [agent-core `belle/20261013`](https://github.com/harenome/agent-core/tree/belle/20261013)

Install JiuwenSwarm from the matching branch, then install Belle into the same
environment (`pip install '.[slack,openjiuwen]'` from this repository, or an equivalent
path to the package). Install the selected agent-core (`openjiuwen`) last and
run `pip check` to check its dependencies. Run Gateway and AgentServer from
that environment.

Belle's log lines go to JiuwenSwarm's console and `gateway.log`, from the
level that JiuwenSwarm's `logging` section sets.

## First-time setup

### 1. Slack app

Choose a tiered Slack app manifest from
[`src/belle_agent/platforms/slack/manifests/`](../../src/belle_agent/platforms/slack/manifests/)
(installed packages: `belle_agent/platforms/slack/manifests/`). Tier 1
(`slack-app-manifest-1-connector.yaml`) is the usual starting point. See
[Slack connector](../platforms/slack.md) for what each tier grants.

Create the app from that manifest, in one of two ways:

1. **Web UI:** [Slack API Apps](https://api.slack.com/apps) → **Create New App** →
   **From a manifest** → paste the YAML.
2. **Slack CLI:** `slack login`, then call `apps.manifest.create` with the
   manifest as a JSON string (`apps.manifest.validate` first if you want a
   check). The CLI creates the app. It does not mint Belle’s runtime tokens.

Then finish the steps Slack does not put in the manifest:

1. **Install to Workspace** and save the bot token (`xoxb-...`).
2. Under **Basic Information → App-Level Tokens**, create a token with
   `connections:write` and save it (`xapp-...`).
3. Invite the bot into each channel where it should run.

Put the tokens in the service environment (for example `SLACK_BOT_TOKEN` and
`SLACK_APP_TOKEN`). Treat them as secrets.

### 2. Link Belle extensions

JiuwenSwarm loads extensions from `~/.jiuwenswarm/application_plugins/` in both
Gateway and AgentServer. Link the three standard packages once. Resolve their
paths from the Python environment with `belle-agent` installed:

```sh
mkdir -p ~/.jiuwenswarm/application_plugins
EXT="$(python -c 'from importlib.resources import files; print(files("belle_agent.integrations.jiuwenswarm").joinpath("extensions"))')"
ln -sfn "$EXT/agent_plugin" ~/.jiuwenswarm/application_plugins/belle-agent-plugin
ln -sfn "$EXT/slack" ~/.jiuwenswarm/application_plugins/belle-slack
ln -sfn "$EXT/second_brain" ~/.jiuwenswarm/application_plugins/belle-second-brain
```

| Link | Role |
| --- | --- |
| `belle-agent-plugin` | Rails, skills, turn fields, silence filter, Slack cron hooks |
| `belle-slack` | Replaces the built-in Slack channel (`replaces: slack`) |
| `belle-second-brain` | Gateway ingest adapter (optional until you enable second brain) |
| `belle-mattermost` | Optional: the Mattermost channel and its tools. See [Mattermost channel](#mattermost-channel-experimental). |
| `belle-matrix` | Optional: the Matrix channel. See [Matrix channel](#matrix-channel-experimental). |

### 3. JiuwenSwarm config

Allow Belle’s Slack override and enable Slack:

```yaml
extensions:
  allow_overrides: [channels.slack]
channels:
  slack:
    enabled: true
    bot_token: '${SLACK_BOT_TOKEN}'
    app_token: '${SLACK_APP_TOKEN}'
```

`allowed_channel_ids`, `allow_from`, and related connection keys stay under
JiuwenSwarm’s `channels.slack` as described in [Slack connector](../platforms/slack.md).

### 4. Belle config

Place Belle's YAML at `~/.config/belle-agent/config.yaml`, or set
`BELLE_AGENT_CONFIG_FILE` to another path. Start from
[examples/config/belle-config.yaml](../../examples/config/belle-config.yaml).

Belle's Slack settings are under `platforms.slack`. Belle refuses a file
that has them at the top level, as `slack:`. Leave `platforms.slack.enabled` in
Belle's file empty or `true`. The JiuwenSwarm
channel serves Slack only and refuses `false`. JiuwenSwarm's
`channels.slack.enabled` turns its Slack channel off.

Install the [required skills](../skills.md#install-skills) before relying on
`agent.skills.required` in that file. See [Conversation scopes](../scopes.md) for
match rules and precedence.

#### Tool permissions of a scope

Belle applies `permissions.tools` at each tool call. It reads the conversation
of the call from JiuwenSwarm's tool permission context
(`TOOL_PERMISSION_CHANNEL_ID` and `TOOL_PERMISSION_CHAT_ID`). If the context
holds neither value and a scope sets `permissions.tools`, Belle refuses the
tool call. The
tool result says that the conversation is unknown, so the scoped tool
permissions cannot be applied. Belle logs a warning once per process that
names the two values. If no scope sets `permissions.tools`, Belle does not
refuse a tool call for this reason.

JiuwenSwarm sets these values in the task of the request. The DeepAgent runs
tools in the tasks of its scheduler, and these tasks do not get the values.
Thus, while a scope sets `permissions.tools`, Belle refuses every tool call of
every Slack and Mattermost turn. Remove `permissions` from the scopes to use
tools on these turns.

Example scope block:

```yaml
platforms:
  slack:
    enforce_audience: true
    bot_name: Belle
    command_name: belle
    group_chat_mode: all
    decision_forward: true
    decision_questions_file: /path/to/your-clone/examples/config/slack-decision-questions.json

scopes:
  - match: {channel: slack}
    agent:
      skills:
        required: [messaging-context]
  - match: {channel: slack, chat_type: [channel, group, mpim]}
    delivery:
      mode: [all]
      reply: optional
    agent:
      skills:
        required: [messaging-context, group-chat-participation]
  - match: {channel: slack, chat_type: im}
    agent:
      skills:
        required: [messaging-context, belle-command-help, slack-app-home]
  - match: {channel: slack, chat: "C0WELCOME"}
    agent:
      skills:
        required: [welcome-onboarding]
    delivery:
      events: {member: turn}
      reply: optional
      prompt_append: |-
        This conversation's events are membership changes.
        Read the event block: act only on member_joined_channel.
        Otherwise, always be silent for any other message or any other event unless you are explicitly directly mentioned.

        For each member_joined_channel event, write the welcome as the welcome-onboarding skill describes
        and send it with post_message, using that event's author_user_id as chat_id.
```

Replace `C0WELCOME` with an allowed conversation ID.
Belle also reads `people`, `roles`, `models`, and `second_brain` from that file.

`platforms.slack.group_chat_mode: all` (global) or `delivery.mode: [all]` (per scope) lets
the bot read all messages in matching rooms.
`delivery.reply: optional` lets the bot stay silent when appropriate.
In this example only multi-person conversations may stay silent. DMs require
answers.
Each narrower `agent.skills.required` list replaces the broader list, so both
rules repeat `messaging-context`.

`platforms.slack.decision_questions_file` customizes questions asked before a chat turn.
See [Decision questions](../config.md#decision-questions).

### 5. Restart and check

Restart Gateway and AgentServer. Check that Slack Socket Mode connects, the three
plugin links resolve, and a DM or mention in an allowed channel gets a reply.

## Mattermost channel (experimental)

The `belle-mattermost` extension adds the channel `mattermost` to
JiuwenSwarm. It replaces no built-in channel, and it does not change the
Slack channel. Without the link and `channels.mattermost`, nothing of it
runs.

The channel runs Belle's conversation host in the Gateway, over Belle's
[Mattermost platform](../platforms/mattermost.md). Each message that the
scopes admit is one `chat.send` request to the AgentServer, which runs the
turn. Thus direct messages, mentions, replies to the bot and
`delivery.mode: [all]`, the marks, the streamed reply,
questions as buttons, files and `/belle` behave as the Mattermost guide
says. The AgentServer runs the turn with its own agent, skills and tools,
and Belle's Mattermost tools.

### 1. Server and bot

Set up the bot, its token, the action listener and the slash command as
[Set up on an existing Mattermost server](../platforms/mattermost.md#set-up-on-an-existing-mattermost-server)
says. The action listener runs in the Gateway process. The server must
reach `platforms.mattermost.actions.url` there.

Install Belle with the required extras: `pip install '.[slack,mattermost,matrix,openjiuwen]'`.

The [bundled image](../../containers/jiuwenswarm/README.md#run-with-mattermost)
does steps 2 and 3 itself, with the channel on when the container has `MATTERMOST_URL`,
and its Belle config has the section of step 4. It needs only the server's
environment variables and a network that the server reaches.

### 2. Link the extension

Link it beside the other three, in the same directory:

```sh
ln -sfn "$EXT/mattermost" ~/.jiuwenswarm/application_plugins/belle-mattermost
```

The Gateway loads the channel from it. The AgentServer loads Belle's
Mattermost tools from it.

### 3. JiuwenSwarm config

Turn the channel on:

```yaml
channels:
  mattermost:
    enabled: true
```

The channel replaces nothing, so `extensions.allow_overrides` needs no entry
for it. JiuwenSwarm's shipped config templates have no `channels.mattermost`.
JiuwenSwarm's config merge keeps the key, because it removes a key that is
absent from the template only when it is told to prune.

### 4. Belle config

Write `platforms.mattermost` in Belle's file, with one server or several, as
[Settings](../platforms/mattermost.md#settings) and
[Several servers](../platforms/mattermost.md#several-servers) say. Every
setting of the channel is in that section: JiuwenSwarm's
`channels.mattermost` only turns the channel on. Scopes match
`channel: mattermost`.

Give the bot token, and the command token if the server uses one, to both Gateway
and AgentServer: the Gateway reads the server's events, and the AgentServer
runs the tools. Belle's file also needs its `platforms.slack` section, as in
the example config, because Belle's JiuwenSwarm extensions read it for each
request.

When `channels.mattermost` is on and Belle's section is off, or is on in
part, the Gateway logs Belle's reason and does not start the channel.
When the channel cannot start, for example because a server does not
answer, the Gateway logs `mattermost cannot start:` and the reason, then runs
without the channel.

### Tools and approvals

The AgentServer mounts Belle's Mattermost tools for the requests of the
Mattermost channel. JiuwenSwarm offers them as deferred tools: the model
finds a tool with `tool_search`, then calls it with `tool_call`. With several
servers, a call acts on the server of its turn, with that server's bot. The
Gateway connects the servers and names each turn's server, so the AgentServer
connects none: a server that went down since the turn began answers the call
with its own error. Before a post
reaches people who cannot read the turn's channel, the channel asks the
person who asked for the turn, with buttons, as on Slack. It asks a question
of JiuwenSwarm's permission engine with buttons too.

### Limits

With `scheduling.engine: auto`, Belle uses JiuwenSwarm's scheduler and its `cron_*` tools. The Belle scheduler is unavailable on this backend. See [Scheduled jobs](../scheduling.md).

The [feature matrix](../feature-matrix.md) compares JiuwenSwarm with the
other harnesses.

| Feature | What happens |
| --- | --- |
| `permissions.tools` in a scope | Not applied per conversation. A request of a channel extension carries no tool levels, and the tool call does not know its conversation. While a scope sets `permissions.tools`, Belle refuses every tool call of a Mattermost turn. See [Tool permissions of a scope](#tool-permissions-of-a-scope). |
| Second-brain ingest | The channel ingests nothing. Retrieval works, because the AgentServer's second-brain rail serves the turns of every channel. |
| Cron jobs | A job cannot have `mattermost` or `matrix` as its target. A target needs a delivery address, and JiuwenSwarm's extension SDK has no address type for an extension's channel. JiuwenSwarm would take such a job and push it to another channel. Belle refuses `cron_create_job`, `cron_update_job`, and the `add` and `update` actions of `cron` with `cron_target_unavailable` when the job's delivery channel (`delivery.channel`, else `targets`) names Mattermost or Matrix. In a Mattermost or Matrix turn, it also refuses a new job whose delivery channel is left out or empty, because that means the turn's conversation. A job may still target another channel that delivers jobs. |
| Heartbeat follow-ups | A Mattermost or Matrix conversation gets none. A heartbeat run is a turn that the AgentServer starts, and these channels post only the turns that they started. In a Mattermost or Matrix turn, Belle refuses `heartbeat_create_job` with `heartbeat_unavailable`. |
| `post_message` to the turn's own channel | The channel posts the turn's final text after it. The host does not see the AgentServer's tool calls. |
| A question with no options | The turn fails, because a button cannot give a typed answer. |
| Slack, Mattermost, and Matrix in one JiuwenSwarm | Untried. Each tool name, such as `post_message`, belongs to the first channel whose request mounts it. |

## Matrix channel (experimental)

The `belle-matrix` extension adds Matrix to JiuwenSwarm. It uses Belle's
[Matrix platform](../platforms/matrix.md) in the Gateway and runs turns in the
AgentServer. Link the extension beside Belle's other extensions:

```sh
ln -sfn "$EXT/matrix" ~/.jiuwenswarm/application_plugins/belle-matrix
```

Turn the channel on in JiuwenSwarm's config:

```yaml
channels:
  matrix:
    enabled: true
```

Configure the account, store and helper under `platforms.matrix` in Belle's
config. See [Matrix setup](../platforms/matrix.md#set-up-on-a-matrix-server) for the account and
recovery key. Install the Matrix helper where `platforms.matrix.helper_path`
names it. Keep its crypto store persistent and its recovery key outside the
store. JiuwenSwarm's config holds only the channel switch. It keeps the switch
across template merges unless you ask it to prune unknown keys.
When the channel cannot start, the Gateway logs `matrix cannot start:` and
the reason, which [Failures and their fixes](../platforms/matrix.md#failures-and-their-fixes)
explains, then runs without the channel.

Install Belle with the `matrix` extra and `belle-matrix-helper`. The
[bundled image](../../containers/jiuwenswarm/README.md#run-with-matrix)
includes both. Use `MATRIX_*_FILE` variables for secrets: JiuwenSwarm's shell
tool inherits the process environment.

**Tool limit:** Matrix tools are not mounted in the AgentServer. The Matrix
helper and its exclusive crypto store run in the Gateway, and JiuwenSwarm's
extension API has no AgentServer-to-Gateway tool request path. Matrix refuses
a second helper against the same store. Belle can answer through the
channel, but a model cannot call Belle's Matrix posting, reading or file tools
on this host. Do not use this bridge for tasks that require those tools.

**Job limit:** No cron job or heartbeat follow-up reaches a Matrix room, as on
Mattermost, and Belle refuses one in a Matrix turn. See
[Limits](#limits).

## PR tracker trigger

JiuwenSwarm's `auto_link` trigger can use per-channel
`auto_link_prompt_overrides` for the [PR tracker](../skills.md#reusable-skills).
Key the override by channel ID and check the trigger's behavior on the deployed
version. The skill's `--via auto_link` records the trigger source. It does not
install the trigger or a schedule.

## Related docs

- [Install the Python package](../installation.md)
- [Slack connector](../platforms/slack.md) (tiers, settings, troubleshooting)
- [Mattermost platform](../platforms/mattermost.md)
- [Belle configuration](../config.md)
- [Conversation scopes](../scopes.md)
- [Skills](../skills.md)
