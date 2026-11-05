# Mattermost platform (experimental)

Belle connects to a Mattermost server as a bot account. It calls the REST
API v4 and reads events from the server's WebSocket. The
`platforms.mattermost` section turns the platform on when `url` and
`MATTERMOST_BOT_TOKEN` resolve. With the example configuration, set `MATTERMOST_URL` and
`MATTERMOST_BOT_TOKEN`. See [Platforms](README.md) for the `enabled` key.
One Belle can also connect to several servers: see [Several servers](#several-servers).

The connector checks the bot token, reads the server's events over the
WebSocket and answers in text. A turn has the tools of [Tools](#tools). With
an action listener, the connector asks questions with buttons, shows a stop
button on a turn's activity card, and takes the `/belle` slash command. It can
show a reply as the model writes it, and it hands reactions and changes of
members to the host as events.

## Messages

| Message | What Belle does |
| --- | --- |
| In a direct channel | Answers in the direct channel, in the thread where the message is in one. |
| A mention of the bot, `@<bot name>`, in a channel | Answers in the thread of the message. The leading `@<bot name>` is not part of the text the model reads. The turn is told `[trigger: mention]` instead. Only the bot's name in the text is a mention: `@channel`, `@all` and `@here` are not. |
| In a thread whose first post is the bot's | A reply to the bot, told to the turn as `[trigger: reply]`. Belle answers in that thread where the scope's `delivery.mode` takes `reply`. The default mode takes only mentions. |
| Any other channel message | Takes part as the `delivery.mode` of the scopes says. |
| The bot's own post, a system message | Ignored. |
| An edit | Not a new request. |

A thread that a person started does not become the bot's when the bot
answers in it. A later message in that thread needs a mention, as on Slack.

A message that arrives twice, such as after a reconnect, starts one turn.
Belle keeps the ids of the messages it handled in its state directory.

After a dropped connection, the connector opens the WebSocket again with the
connection's id and its next sequence number. The server then sends the
events that the drop missed. The server resumes only a connection that it has
already found dead, which takes it up to 100 seconds, so after a short drop it
usually starts a new connection instead. A new connection does not send the
events of the gap. The connector logs a warning with the start, the end and
the length of the gap. It then reads back, from the REST API, what the gap
held in every channel that the bot is a member of, and handles it oldest
first, as if it had just arrived:

- The posts created in the gap.
- The members who joined or left a channel, from the system post that
  Mattermost adds for each change. A team's own joins and departures count
  for its default channel, where Mattermost posts them.
- The reactions added in the gap to the latest posts of the channel, as many
  pages of 200 as `gap_reaction_scan_max_pages` says.

The read starts 60 seconds before the gap, to allow for a difference between
the clocks of Belle and the server. A post, a member change or a reaction that
the connector already handled is not handled again.

Mattermost keeps no record of some changes, so these are lost in a gap:

- A reaction that was removed.
- A reaction added to a post older than the pages that Belle reads.
- A member who left the team, in the team's channels other than its default
  one. Mattermost posts nothing in them.

Belle reads the posts page by page. Mattermost's listing of the posts changed
since a time stops at 1000 posts and, right after many posts, leaves some out,
both without saying so.

## Install

From the repository root, install the extra that brings the WebSocket client:

```sh
pip install '.[mattermost]'
```

## On JiuwenSwarm

JiuwenSwarm can serve Mattermost too, as a channel of its Gateway. Belle's
conversation host then runs in the Gateway with this connector, and
JiuwenSwarm's AgentServer runs each turn with its own agent and Belle's
tools. See [Mattermost channel](../integrations/jiuwenswarm.md#mattermost-channel-experimental)
for the link, the switch and the settings. That guide also lists what the
channel does not do there: the tool levels of scopes, second-brain ingest
and cron jobs.
The [JiuwenSwarm image](../../containers/jiuwenswarm/README.md#run-with-mattermost)
serves Mattermost with no config change, as the launcher image does.

## Quick start with containers

The launcher image runs Belle on Mattermost with no change: it holds the
example configuration, whose `platforms.mattermost` section turns on when
`MATTERMOST_URL` and `MATTERMOST_BOT_TOKEN` are set. This quick start uses
the OpenCode harness. Build both images, and write `model.env` and
`pair-opencode.env`, as in
[the OpenCode quick start](../../containers/opencode/README.md). First do
the steps of [Set up on an existing Mattermost server](#set-up-on-an-existing-mattermost-server).

`belle.env` (Belle only), mode `0600`, with literal values:

```dotenv
MATTERMOST_URL=https://chat.example.com
MATTERMOST_BOT_TOKEN=replace-with-the-bot-access-token
MATTERMOST_ACTIONS_URL=http://belle:18400
MATTERMOST_COMMAND_TOKEN=replace-with-the-slash-command-token
```

`MATTERMOST_URL` is the address of the server, as Belle reaches it.
`MATTERMOST_ACTIONS_URL` is the address of Belle's action listener, as the
server reaches it, and `MATTERMOST_COMMAND_TOKEN` is the token of the
`/belle` slash command. Without these two, Belle answers messages but shows
no buttons and takes no command.

Start OpenCode, then Belle. Where the server runs in a container on the
same host, connect it to Belle's network first, as the commented line shows:

```sh
podman network create belle-net
# podman network connect belle-net <server container>
podman run -d --name belle-opencode --network belle-net \
  --env-file model.env --env-file pair-opencode.env \
  localhost/belle:opencode-local
podman run -d --name belle --network belle-net \
  --env-file pair-opencode.env --env-file belle.env \
  localhost/belle:launcher-local
podman logs belle
```

The log ends its start with `belle is serving: platform=mattermost
backend=opencode`, after `mattermost action listener on port 18400`. Send
the bot a direct message, or mention it in a channel where it is a member.

The server must reach the listener at `MATTERMOST_ACTIONS_URL`. On a
shared network, `http://belle:18400` is that address. Otherwise publish
the port with `-p 18400:18400` on the `belle` container, and set
`MATTERMOST_ACTIONS_URL` to an address of the host that the server
reaches. A private address also needs
[the server setting of the listener](#4-the-servers-reach-to-belles-listener).

The `compose.yaml` of each harness starts the same two containers with one
command, with this `belle.env`, `model.env` and a `.env` that names the
images in its directory. See
[the OpenCode quick start](../../containers/opencode/README.md#one-command).
Its network is `belle-opencode_default`, or `belle-docker-agent_default`
for Docker Agent. Connect a server container on the same host to it with
`podman network connect`.

Docker Agent can take the place of OpenCode. Build its image, and write
`model.env` and `pair-docker-agent.env`, as in
[the Docker Agent quick start](../../containers/docker-agent/README.md).
Use the same `belle.env`. Start Docker Agent, then Belle:

```sh
podman network create belle-net
# podman network connect belle-net <server container>
podman run -d --name belle-docker-agent --network belle-net --read-only \
  --env-file model.env --env-file pair-docker-agent.env \
  localhost/belle:docker-agent-local
podman run -d --name belle --network belle-net \
  --env-file pair-docker-agent.env --env-file belle.env \
  localhost/belle:launcher-local
```

The log ends its start with `belle is serving: platform=mattermost
backend=docker_agent`. Direct messages, mentions, buttons, the activity
card and its stop button, `/belle` and the files of a message work as
with OpenCode. The model of the shipped agent YAML reads text only: Belle
gives it the name of an image or a PDF of a message, and not the content.
See [the Docker Agent guide](../backends/docker-agent.md#files) to declare
a model that reads them. The shipped agent YAML declares no subagent and no
expert, so `/belle subagents` and `/belle experts` are not available. For
a persistent deployment, mount the volumes that the Docker Agent quick
start names.

The commands above keep the state in the containers. For a persistent
deployment, mount a volume on OpenCode's home and on each of Belle's two
directories. In place of the two `podman run` commands above:

```sh
podman run -d --name belle-opencode --network belle-net \
  --env-file model.env --env-file pair-opencode.env \
  -v belle-opencode-home:/home/opencode:U \
  localhost/belle:opencode-local
podman run -d --name belle --network belle-net \
  --env-file pair-opencode.env --env-file belle.env \
  -v belle-state:/home/belle/.local/state/belle-agent:U \
  -v belle-data:/home/belle/.local/share/belle-agent:U \
  localhost/belle:launcher-local
```

| Directory | Holds |
| --- | --- |
| `/home/opencode`, in the OpenCode container | OpenCode's sessions: each conversation and its history |
| `/home/belle/.local/state/belle-agent` | The ids of the messages Belle handled, `mattermost_seen_events.json`, which keep a message from starting two turns. What each conversation's next turn is told of its stopped turns and held calls, `turn_notes.json` |
| `/home/belle/.local/share/belle-agent` | The second brain's corpus, in `second_brain/` |

A recreated container with the same volumes goes on with each conversation
in its session, and keeps the corpus, the handled ids and the notes for each next turn. Belle reads the env files at its start: recreate the
container after a change, since `podman restart` does not read them again.
The listener's port is `platforms.mattermost.actions.port`, 18400 in the
example configuration. To change it or another setting, mount your
configuration file at `/home/belle/.config/belle-agent/config.yaml`.

The example configuration gives Mattermost the scopes that it gives Slack.
Its `agent.skills.unavailable` removes the skills for Slack
(`slack-app-home`, `slack-block-kit-rendering`, `slack-daily-update`) from Mattermost turns.
A direct message always gets an answer. In a channel or a group, every
message starts a turn, and the turn speaks only through `post_message`, so
Belle decides when to answer, also to a mention. Each conversation has one
session, and a message that comes while a turn runs waits for it.

## Set up on an existing Mattermost server

Belle needs a bot account and its token on the server. Buttons and the
`/belle` command also need a slash command and a route from the server to
Belle's listener. A system administrator does these steps once.

### One command

The [setup image](../../containers/mattermost-helper/README.md) does the
steps of [the bot account and its token](#2-the-bot-account-and-its-token), [team and channels](#3-team-and-channels) and [the slash command](#5-the-slash-command)
with one command. It calls the server's REST API with an admin's personal
access token, and needs no access to the server's host. It makes the bot
account, its token, the team and channel memberships and the `/belle`
command. It keeps each one that exists, so a second run changes nothing.
It writes `out/mattermost.env` with `MATTERMOST_URL`,
`MATTERMOST_BOT_TOKEN`, `MATTERMOST_COMMAND_TOKEN` and
`MATTERMOST_ACTIONS_URL`.

Put the admin's token in `admin.env`, one line
`MATTERMOST_ADMIN_TOKEN=<token>`, mode 600. Build the image from the
repository root:

```sh
podman build --format oci -t localhost/belle:mattermost-helper-local -f containers/mattermost-helper/Containerfile \
  --ignorefile containers/mattermost-helper/.containerignore .
```

Run it from the directory of `admin.env`:

```sh
install -d -m 700 out
podman run --rm --userns=keep-id:uid=10001,gid=10001 \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --memory=128m --cpus=1 --pids-limit=64 \
  -v "$PWD/admin.env:/secrets/admin.env:ro" -v "$PWD/out:/out:rw" \
  localhost/belle:mattermost-helper-local \
  --url https://chat.example.com --team my-team --channel town-square \
  --listener http://belle:18400
```

Give Belle's container `--env-file out/mattermost.env`. A failure names
the step, the server's error id, and the setting or the permission that is
missing. The bot account and its token need a system admin. A team admin
can do the memberships and the command. The server must still reach the
listener: see [Action listener](#action-listener).

### In the web app

Each server setting below names its page in the **System Console**, and its
key in the server's configuration. A system administrator can also change
a setting with the REST API, `PUT /api/v4/config/patch`. A setting that an
environment variable sets, such as `MM_SERVICESETTINGS_ENABLECOMMANDS`, is
read-only in the System Console.

#### 1. Server settings

| System Console page | Setting | Value | Why |
| --- | --- | --- | --- |
| **Integrations > Bot Accounts** | **Enable Bot Account Creation** (`ServiceSettings.EnableBotAccountCreation`) | true | The bot account |
| **Integrations > Integration Management** | **Enable Personal Access Tokens** (`ServiceSettings.EnableUserAccessTokens`) | true | The bot's access token |
| **Integrations > Integration Management** | **Enable Custom Slash Commands** (`ServiceSettings.EnableCommands`) | true | `/belle` |
| **Integrations > Integration Management** | **Enable integrations to override usernames** (`ServiceSettings.EnablePostUsernameOverride`) | true | The name on the answers to `/belle` |

The answer to `/belle` is a message that only the person who typed the
command sees. With **Enable integrations to override usernames** false,
the server shows it under that person's name, with a `BOT` tag. With true,
the server shows it under the command's response username, such as the
bot's. The picture stays that of the person. The setting also lets every
other webhook and slash command of the server post under a name of its
choice, which the System Console warns of.

#### 2. The bot account and its token

From the product menu, open **Integrations > Bot Accounts** and add a bot
account, such as `belle`. Then select **Create New Token** on it. The server
shows the token once: set it in `MATTERMOST_BOT_TOKEN`. The administrator
who adds the bot owns it. With the System Console setting **Disable bot
accounts when owner is deactivated** true, the default, a deactivation of
that administrator's account stops the bot.

#### 3. Team and channels

Add the bot to the team, and to each channel where Belle is to take part.
In a channel, a member types `/invite @belle`. Belle reads and answers only
where the bot is a member, and in direct messages.

#### 4. The server's reach to Belle's listener

The server sends a click and a slash command to Belle's listener, at
`MATTERMOST_ACTIONS_URL`. Where that address is a private one, such as
the name of a container on the server's network, the server calls it only
when **Environment > Developer > Allow untrusted internal connections to**
(`ServiceSettings.AllowedUntrustedInternalConnections`) names its host.
Separate several hosts with spaces. Without a route to the listener, a
click and a command fail on the server, and Belle never sees them: the
person who types `/belle` reads `Command with a trigger of 'belle'
failed.`

#### 5. The slash command

From the product menu, open **Integrations > Slash Commands** and add a slash
command:

| Field | Value |
| --- | --- |
| Trigger word | `belle` |
| Request URL | `<MATTERMOST_ACTIONS_URL>/command`, such as `http://belle:18400/command` |
| Request method | `POST` |
| Response username | The bot's name, such as `belle` |
| Autocomplete | On, with the hint `[text]` |

The server shows the command's token: set it in
`MATTERMOST_COMMAND_TOKEN`. One team holds one command for each trigger
word. Add the command to each team where people use `/belle`.

#### 6. Private messages of the bot

The answer to a slash command and to a click needs no permission. Any
other message that only one person sees, such as a post with
`visible_only_to_user_id`, needs the permission `create_post_ephemeral`.
By default only a system administrator holds it, and Belle refuses such a
post with an error that names the permission. Team Edition has no custom
role. Two grants work, each with `PUT /api/v4/roles/{role_id}/patch` or
`mmctl permissions add <role> create_post_ephemeral`:

- To the role `system_user_access_token`, then give the bot that role with
  `PUT /api/v4/users/{bot_user_id}/roles` and the roles
  `system_user system_user_access_token`. Only the holders of the role can
  then post such a message. The role also lets the bot make and revoke
  access tokens of its own.
- To the role `system_user`. Every user of the server can then post a
  message that only one person sees.

### Without admin rights

With the server's default permissions, a member of a team cannot create a
bot account, an access token or a slash command, cannot change a server
setting, and cannot post a message that only one person sees. A member can
add a bot account to an open team, to a public channel, and to a private
channel where the member is. With a bot token from an administrator, a
member runs Belle with direct messages and mentions. Buttons and `/belle`
need the slash command and, on a private network, the listener setting of
an administrator.

## Settings

```yaml
platforms:
  mattermost:
    enabled:
    url: ${MATTERMOST_URL}
    actions:
      bind: 0.0.0.0
      port: 18400
      url: ${MATTERMOST_ACTIONS_URL}
    servers: []
    allow_from: []
    allowed_channel_ids: []
    decision_questions_file: ""
    decision_forward: true
    history_max_total_chars: 200000
    directory_scan_max_pages: 25
    audience_member_max_pages: 25
    gap_reaction_scan_max_pages: 1
    file_download_max_bytes: 31457280
    file_upload_max_bytes: 31457280
    acknowledgement_emoji: eyes
    rejected_emoji: no_entry_sign
    queued_emoji: hourglass_flowing_sand
    completed_emoji: disabled
    failed_emoji: x
    stopped_emoji: black_square_for_stop
    brain_react_pending: open_book
    brain_react_success: brain
    brain_react_declined: x
    brain_react_failure: warning
    enable_streaming: false
    activity_card: true
    activity_card_delay_seconds: 5
    activity_card_min_edit_seconds: 10
    typing_indicator: true
```

| Key | Meaning |
| --- | --- |
| `enabled` | Empty: on when `url` and the token resolve. `true` or `false`: see [Platforms](README.md). |
| `url` | The address of the server, `http` or `https`, such as `https://chat.example.com`. The connector adds `/api/v4`. |
| `actions` | Belle's action listener: the address and the port it listens on, and `url`, the address at which the server reaches it. Empty `url`: no listener, so no buttons and no slash command. See [Action listener](#action-listener). |
| `servers` | Several servers, each with its own address, tokens and listener. Empty: the server of `url`. See [Several servers](#several-servers). |
| `allow_from` | The user ids that may start a turn. Empty: anybody. |
| `allowed_channel_ids` | The channel ids where Belle takes part. Empty: every channel. |
| `decision_questions_file`, `decision_forward` | The questions of the decision gate, and whether its answers reach the turn. See [Decision questions](../config.md#decision-questions). |
| `history_max_total_chars` | Positive integer limiting the text in one result. `download_file` refuses a longer text file. The key has the name and the meaning of the Slack key. |
| `directory_scan_max_pages` | Maximum pages per directory kind that `find_by_name` scans. Default `25`; increase it for a large server. Incomplete coverage names this setting. |
| `audience_member_max_pages` | Maximum member pages used to prove a channel's audience. Default `25`. If the channel is larger, cross-channel reads and writes refuse until the operator raises this setting. |
| `gap_reaction_scan_max_pages` | Pages of 200 of each channel's latest posts whose reactions Belle reads back after a gap. Default `1`. Each page is one request per channel that the bot is a member of. A reaction added during the gap to an older post is lost. See [Messages](#messages). |
| `file_download_max_bytes` | Maximum bytes in one inbound attachment, `download_file`, or content-tool file read. Default `31457280` (30 MiB). A larger file is refused whole and the error names this setting. |
| `file_upload_max_bytes` | Maximum bytes sent by one `send_file` call. Default `31457280` (30 MiB). The server may impose a lower upload limit. |
| `acknowledgement_emoji` | The reaction on a message that starts a turn, or waits for one. The end of the turn removes it. |
| `rejected_emoji` | The reaction on a message from a sender that `allow_from` does not list. |
| `queued_emoji` | The reaction on a message that waits for a turn. Its turn's start removes it. |
| `completed_emoji`, `failed_emoji`, `stopped_emoji` | The reaction on a message whose turn ended so. |
| `brain_react_pending` | The reaction on a message while the second brain ingests its documents. The end of the ingest removes it. |
| `brain_react_success` | The reaction on a message with a document that the corpus now holds, or held already. |
| `brain_react_declined` | The reaction on a message whose documents the second brain declined, with no error. |
| `brain_react_failure` | The reaction on a message whose documents the second brain could not ingest. |
| `enable_streaming` | `true`: a reply appears in a post as the model writes it. See [A running turn](#a-running-turn). |
| `activity_card` | `true`: a turn that runs longer than the delay shows a card of its tool calls, with a stop button. |
| `activity_card_delay_seconds` | How long a turn runs before its card appears. |
| `activity_card_min_edit_seconds` | The shortest time between two edits of a card. |
| `typing_indicator` | `true`: while a turn runs, the web client shows `<bot> is typing...` where the answer goes. See [A running turn](#a-running-turn). |

Each reaction key takes an emoji name without colons, or `disabled` for no
reaction.

The bot token comes from the environment variable `MATTERMOST_BOT_TOKEN` and
from nowhere else. The connector refuses a `token` key in the section. A
section with an address and no token, or a token and no address, stops the
start, with an error that names the key or the variable. A token
that the server refuses stops the start too. No error and no log line holds
the token.

## Several servers

One Belle can serve several Mattermost servers at once. List them under
`servers`, with a bot account on each server:

```yaml
platforms:
  mattermost:
    servers:
      - name: main
        url: https://chat.example.com
        bot_token_env: MATTERMOST_MAIN_BOT_TOKEN
        command_token_env: MATTERMOST_MAIN_COMMAND_TOKEN
        actions: {bind: 0.0.0.0, port: 18401, url: "http://belle:18401"}
      - name: lab
        url: https://lab.example.org
        bot_token_env: MATTERMOST_LAB_BOT_TOKEN
        command_token_env: MATTERMOST_LAB_COMMAND_TOKEN
        actions: {bind: 0.0.0.0, port: 18402, url: "http://belle:18402"}
```

| Key of a server | Meaning |
| --- | --- |
| `name` | The server's name in Belle: 1 to 32 lower-case letters, digits, `-` and `_`. Workspaces, scopes and log lines use it. |
| `url` | The address of the server, as the section's `url`. `${NAME}` reads it from the environment. |
| `bot_token_env` | The name of the environment variable that holds the bot token on this server. The file never holds a token. |
| `command_token_env` | The name of the environment variable that holds this server's `/belle` token. Empty: this server refuses every command. |
| `actions` | This server's action listener, as the section's `actions`. |
| `enabled` | Empty: on when `url` and the bot token resolve. `false`: off. `true`: a missing one stops the start. |

A non-empty list takes the place of the section's `url` and `actions`. A
`url` or an `actions.url` of the section that resolves beside the list stops
the start. Every other key of the section applies to each server. The scopes
and `allow_from` hold user ids and channel ids of every server.

A server is on when its `url` and its bot token resolve, and off when
neither does. A server with only one of the two stops the start, and the
error names the missing variable or key. The platform is on when one server
is on. Two servers with the same `name`, `url`, `bot_token_env`,
`actions.port` or `actions.url` stop the start too.

At the start, Belle connects each server that is on. An error log names a
server that refuses its token as NOT SERVED, and the other servers serve. A
server that does not answer, or answers with a server error or a rate limit,
is logged as NOT CONNECTED and connected again in the background, after 1 s
and then twice as long each time, up to 30 s, until it serves or refuses.
Until then, Belle refuses every action on that server: nothing is posted
there. When no server serves at the start, the start stops. Two entries whose bot is
the same user, or whose servers have the same `DiagnosticId`, are one server:
only the first entry serves. Both would otherwise answer the same message.

Each server keeps its own connection, its own REST client and its own
listener:

- A team id names a team of one server only, so the workspace of a
  conversation starts with the server's name: `main:<team id>`, or
  `main:direct` for a direct or group channel. Sessions, the window of seen
  messages and the scopes' `workspace` axis use that workspace.
- A reply, a mark, a card and a question go to the server of their
  message. A tool call runs on the server of its turn. An id from another
  server names nothing there, and the tool refuses it with `not_found`.
- Each server sends its clicks and its slash command to its own listener,
  at its own `actions.url`. Give each server's
  `AllowedUntrustedInternalConnections` Belle's host, and add the `/belle`
  command on each server with its own listener's address.

A move from `url` to `servers` changes the workspace of each conversation.
The sessions of the earlier workspaces are then not continued, and a scope
that names a workspace needs the server's name in front of the team id.

## Action listener

The server reaches Belle only by a request to Belle's action listener: a
click on a button, and the slash command. Set `MATTERMOST_ACTIONS_URL` to
the address at which the server reaches the listener, such as
`http://belle:18400`. Belle listens on `actions.bind` and `actions.port`.

A server calls an address on a private network only where the server's
setting **AllowedUntrustedInternalConnections** names its host. See
[The server's reach to Belle's listener](#4-the-servers-reach-to-belles-listener).

The server sends no credential with a click. Each button holds a secret
that Belle makes new at each start, and a click with another secret, such
as a click on a button of an earlier start, changes nothing.

## Questions and approvals

A question or an approval is a post in the turn's thread, with a button per
option. An approval has **Approve** and **Reject**. The host decides who may
answer, by `allow_from` and the scope's `clicks.approve`. The person who
clicks reads what became of the click in a message that only that person
sees:

| Click | What happens |
| --- | --- |
| By a person who may answer | The answer reaches the turn. The post keeps its text and shows `Answered by @<name>: <option>` in place of its buttons. |
| By a person who may not | The question stays open. The person reads that they are not permitted to answer. |
| On a question that no longer waits, such as after a restart | The buttons go, and the post says that the request no longer waits. Nothing is answered. |

A call held for a person's agreement, such as a post that reaches people who
cannot read the turn's channel, is asked the same way. Without the action
listener, such a call is refused with the code `approval_required`. A
question with no options cannot be asked with a button. A harness that can
be told, such as OpenCode, hands the model the reason, and the turn goes on.
On another harness the turn ends, and its failure names the reason.

## A running turn

Mattermost has no stream, so the connector edits posts in place:

- With `enable_streaming`, the reply appears in one post as the model writes
  it, edited at most once a second, and ends as the final reply. A reply
  that is no part of the final reply comes down. A stopped reply keeps its
  text and ends with *Turn stopped*.
- With `activity_card`, a turn that runs longer than
  `activity_card_delay_seconds` shows a card of its tool calls in its
  thread. The card is one message attachment. Its colour bar and title show
  how the turn stands. It has one line for each subagent and tool call, with
  the `description` argument of the call where the call has one. It has a
  **Stop** button while the turn runs, where the action listener is on. The
  host decides who may stop, by the scope's `clicks.stop`. Where the action
  listener is on, a turn owed a reply that has called no tool yet shows the
  card with its title and the button alone; a turn whose reply is optional
  gets its card with its first tool call. The card settles with how the turn
  ended. The card of a turn that said nothing, or that was stopped, or that
  held only the button, comes down. The server does not search the text of
  an attachment, so a search does not find a card.
- A reply longer than the server's `MaxPostSize`, and the host's own final
  reply, go on in a new post in the same thread.
- Where an operator sets the server's `PostEditTimeLimit`, the server can
  refuse an edit of an older post. A reply then keeps what it shows and goes
  on in a new post. A card keeps what it shows.
- With `typing_indicator`, the bot shows as typing from the start of the
  turn to its end. The notice goes where the answer goes. For a message at
  the top of a direct channel, the web client shows it below the channel.
  The answer to any other message goes in a thread, and the web client shows
  the notice only below that thread, when a reader opens it. A web client
  shows each notice for the server's
  `TimeBetweenUserTypingUpdatesMilliseconds` (5000 ms by default), so the
  connector sends a new notice at half that interval. A post of the bot in
  that place removes the notice at once. No call removes a notice, so a turn
  that ends with no post leaves it for up to that interval. Where the server
  sets `EnableUserTypingMessages` to `false`, the connector sends no notice
  and logs that once. The connector reads both settings once, and keeps them
  until Belle starts again.

An edit that adds a mention does not notify the person: a reply shown as it
is written mentions nobody until a new post does.

## Events

The connector hands these to the host as events. The scope's
`delivery.events` decides whether each is dropped, kept as context for the
next turn of its channel, or given a turn of its own:

| Kind | Event |
| --- | --- |
| `reaction` | A reaction added to or removed from a message. Its turn answers in that message's thread. |
| `member` | A person who joins or leaves a channel. |

What the bot did itself is no event. A pin is no event: the server reports a
pin as an edit of the post, and names nobody as the one who pinned it.

## Slash command

A system administrator adds the slash command as in
[The slash command](#5-the-slash-command), and sets its token in
`MATTERMOST_COMMAND_TOKEN`. The server sends the token with each command in
the header `Authorization: Token <token>`, and Belle refuses a command
without that header. Without `MATTERMOST_COMMAND_TOKEN`, every command is
refused.

| Command | What it does |
| --- | --- |
| `/belle help` | Lists the commands. |
| `/belle status` | Says whether the backend answers, the model, and what runs and waits in the channel. |
| `/belle stop [message]` | Stops the turn that runs in the channel. With several, give the id or the link of the message whose turn to stop. |
| `/belle queue list` | Lists the messages that wait for a turn in the channel. |
| `/belle queue drop <message>` | Removes a waiting message. |
| `/belle models list`, `current`, `use <name>`, `reset` | Lists the models, shows the one for new requests here, selects one for the channel, or removes the selection. |
| `/belle skills list`, `subagents list` | Lists the skills or the subagents that the scope lets a turn use here. |
| `/belle experts list` | Lists the installed experts and the one that the scope selects here. |
| `/belle ask <question>` | Asks privately. The answer comes in your direct message with the bot. |
| `/belle skills use <skill> <task>` | Asks privately, with the skill loaded for that one task. |
| `/belle subagents use <name> <task>` | Gives one task to a subagent, privately. |
| `/belle schedule list`, `show`, `pause`, `resume`, `delete`, `run` | Manages Belle scheduled jobs. See [Scheduled jobs](../scheduling.md). |

The answer appears only to the person who typed the command. Belle answers
within the request, so it needs no route to the command's `response_url`.
A list, a skill and a subagent need a backend that lists them. Where the
backend does not, the command says so.

A private question goes as a post of the bot in its direct message with
the person who asked. Asked in another channel, the post names that
channel, and its thread is a conversation of its own: a reply in that
thread continues it, and nothing is posted in the channel. Belle copies
no message of the channel into it. Asked in the direct message itself, the
question joins that conversation. The scope of the direct message decides
whether the turn can use the skill or the subagent.

## Ephemeral messages

A message that only one person sees takes one of three paths:

- The answer to a slash command, and the answer to a click, need no
  permission.
- Any other, such as `visible_only_to_user_id` or a notice of the host,
  needs the bot to hold `create_post_ephemeral`. Without it, the server
  refuses, and the error names the permission. A refused notice of the
  host, such as the one for a message refused because its queue is full,
  is logged as a warning, and the message keeps its mark. See
  [Private messages of the bot](#6-private-messages-of-the-bot) and
  [Tools](#tools).

## Identifiers

Every Mattermost id is 26 letters and digits. The connector checks each tool
argument that holds an id before the tool does anything else. A value of another form,
the string `null` among them, gets the code `<kind>_id_malformed`. The kind
is `chat` for a channel, `message` for a post, `user`, `file` or `team`. An
argument that Slack's tools take too gets the same code as on Slack. The
detail names the argument and the form.

## Scopes

Scopes match the channel `mattermost`. The `workspace` axis matches the
channel's team id, or `direct` for a direct or group channel. With
`servers`, it matches `<server name>:<team id>` or `<server name>:direct`.
Quote it. The chat types are Mattermost's own:

| `chat_type` | Channel |
| --- | --- |
| `O` | Open: every member of the team may join. The second brain counts it as public. |
| `P` | Private. |
| `D` | Direct, between two people. |
| `G` | Group: a direct channel of three or more people. |

## Tools

| Tool | What it does |
| --- | --- |
| `post_message` | Posts in the turn's channel, or in the channel that `chat_id` names. A user id in `chat_id` opens that person's direct channel. `reply_to_message_id` answers in the thread of that message, which can be the thread's first message or any reply in it. |
| `react_to_message` | Adds or removes the bot's reaction on a message of the turn's channel, by default the message that started the turn. |
| `read_conversation` | Reads a channel, or the thread of `message_id`, oldest first. It returns the newest `max_messages` messages, 50 by default and 200 at most. |
| `download_file` | Reads one file, by the `file_id` that `read_conversation` gives in a message's `files` list. |
| `edit_message` | Replaces the text of a message that the bot posted. |
| `delete_message` | Takes down a message that the bot posted. |
| `pin_message` | Pins or unpins a message of the turn's channel. |
| `send_file` | Uploads one file and shares it in a post of its own. |
| `search_messages` | Searches messages, newest first, and names the open channels that the search did not cover. |
| `search_mattermost_files` | Searches indexed filenames, extensions and extracted file content. Returns readable file IDs for `download_file`. |
| `find_by_name` | Finds people, channels and custom emoji by name. |
| `read_mattermost_pinned_messages` | Lists the pinned messages of a channel, newest first. |

Each tool result is JSON. A refusal has `ok: false`, a code in `error`, and
a `detail`. Where the server refused, the detail ends with the server's own
error id.

`post_message` splits a text longer than the server's `MaxPostSize` into
parts in one thread and keeps whitespace at part boundaries. The result
lists the ID of each part. If the server states no size, the tool sends the
whole text and reports the server's refusal.
Belle refuses a split that would create a whitespace-only post. It sends no
parts in that case.

For a single persistent post, `post_message` also accepts native priority,
acknowledgement, persistent notifications, silent delivery, safe post `props`,
and up to five existing `file_ids`. Belle checks each file against the source
conversation. Priority, props and files require one post; shorten long text
or send separate posts. Server licensing can refuse some priority options.

The `post_message` result reports `parts` and the actual `thread_id` of its
first part; an empty thread ID means that part was top-level. Split posts
return every `part_message_ids` entry in send order. A later send failure
returns those IDs with `ok: false`; retrying the entire message can duplicate
the parts already visible. Requester-only ephemeral results have no archive
permalink.

`visible_only_to_user_id` shows a post to the turn's requester only. The
server lets a bot do this only with the permission `create_post_ephemeral`,
which only a system administrator holds by default. Without it, the tool
gets the code `forbidden` and a detail that names the permission. To grant
it to every user, a system administrator runs:

```sh
mmctl permissions add system_user create_post_ephemeral
```

`react_to_message` takes an emoji name, with or without colons, or a common
emoji character. A character with one skin tone gets the Mattermost name of
that tone, such as `+1_medium_light_skin_tone`. A repeated add or remove
succeeds. Belle's own marks use reactions too: the end of a turn removes the
`acknowledgement_emoji` reaction, also where the turn added it with this
tool.

`read_conversation` gives each message's `message_id`, `root_message_id`,
author, text, `created_iso_utc`, reactions and permalink. A message with
files has a `files` list, each file with its `file_id`, `name`,
`mimetype` and `size_bytes`. A message that the
server wrote, such as a join, has a `type`. The `coverage` block says
whether older messages remain, the `next_before_message_id` to page back
from, and the span read. `inaccessible_before_iso_utc`, where present, says
that the server keeps older messages out of reach.
For a channel or thread, `after_iso_utc` includes only posts created strictly
after that instant. It may omit the thread root. `before_message_id` pages
within the same time window.
Thread pages use message IDs to order posts that have the same creation time.

`download_file` returns the text of a text file in the result's `content`.
It returns any other file, such as an image, as content beside the result,
where the backend hands that type of content to the model. Otherwise, the
tool host refuses the result and names the types that the model takes. The
tool refuses a file larger than `file_download_max_bytes` (30 MiB by default), and a text longer than
`history_max_total_chars` characters (200,000 by default), and returns no
part of either. The result names where the file
was shared: `chat_id`, `message_id` and `created_iso_utc`.

`edit_message` and `delete_message` change only a message that the bot
posted. Belle refuses another author's message with `message_not_own`, even
if the bot has permission to change others' posts. The message must be in
the turn's channel, or in the channel that `chat_id` names.
An edit must fit in one message: the tool refuses a longer text with
`edit_text_too_long` and the server's `MaxPostSize`.

The server setting `PostEditTimeLimit` limits how long after the post a
message can change, a pin included. It is off by default. Past the limit,
`edit_message` and `pin_message` refuse with `edit_time_limit_passed` and
the limit. `delete_message` has no such limit. `pin_message` acts on a message of
the turn's channel only. A repeated pin or unpin succeeds.

`send_file` takes the file in exactly one form: `content` (text),
`content_base64` (bytes), or `file_id` (a file that the turn's channel may
read, sent again as a new copy). `filename` is necessary with `content` and
`content_base64`. `comment` is the text of the post, and
`reply_to_message_id` puts the post in a thread. The bot uploads only into
a channel that it is a member of. The tool refuses a file larger than
`file_upload_max_bytes` (30 MiB by default). The server refuses a file larger than its `MaxFileSize`, 100 MiB by
default, before it reads the file. The upload asks the server to accept the
body first (`Expect: 100-continue`), so that this refusal arrives as an
answer, and the tool names the server's limit. A server that gives no
answer within 1 s gets the body anyway, as curl does. A proxy in front of
the server must pass that header and its answer.
If the upload succeeds but the post fails, the error includes
`orphaned_file_ids`. The files remain on the server without a post.

`search_messages` takes the server's search syntax: words, a quoted phrase,
`-word`, a trailing `*`, and the filters `from:`, `in:`, `before:`,
`after:` and `on:`, with dates as UTC days. Without paging, the tool searches
each of the bot's teams, or only `team_id` when given. The server searches channels the bot joined,
and its direct and group messages. The result's `coverage` names each open
channel that the search did not cover (`channels_not_searched`, with
`channels_not_searched_count`), so an `in:` filter on such a channel gives
nothing and says why. `read_conversation` reads such a channel. The server
returns the newest matches, up to a server limit. Use
`is_or_search` to match any term, `time_zone_offset` for date filters, and
`include_archived_channels` to include archived channels the bot can read.
Set `page: 0` to try paging, with optional `team_id`. Belle checks whether
the server honors it. If it does, use `coverage.next_page_token` with the
same query and options to continue. A backend without search paging refuses
the request. Narrow the unpaged search with `before:` and
`coverage.oldest_result_iso_utc`. Unpaged results cannot establish that
the server returned all older matches. Each message has the fields of
`read_conversation` with its `chat_id`, `chat_name` and `chat_type`.
Paging without `team_id` uses Mattermost's all-team route. Mattermost 11.7.12
returned no matches from that route for an `in:` filter that
worked on the team route. Set `team_id` when paging a channel-filtered query.

`search_mattermost_files` uses the server's file index. Filename and
extension search depend on that index. Content matches require the server
to extract text from that file type. Each returned `file_id` can be passed
to `download_file`. The source conversation must pass the same audience
check as a message read. `coverage.results_withheld` counts hits Belle did
not reveal. It does not name their conversations. File paging has the same
backend condition and continuation rule as message search. The all-team
file route requires Mattermost 10.2 or later. On an older server, set
`team_id` to use its team route.

`find_by_name` has the contract of the Slack tool: every match, its
`match_type` and the field that `matched`, `match_count`, and the
`coverage` of each kind. Its kinds are `person`, `chat` and `emoji`.
This tool does not search server groups or group messages. Their availability
depends on separate server routes and permissions. `chat` covers the open
channels of the bot's teams and the private channels that the bot is a
member of. A private channel without the bot is never found, and the
result says so. The result holds no email address.
The operator can raise `directory_scan_max_pages` if the server's user or channel directory exceeds one scan.
Set `max_matches` to return more than the default 20 matches from a lookup.
When a directory scan ends at that budget, `find_by_name` also uses the server's native user and public-channel name search. Its coverage remains partial because native search does not prove all names were returned.
`search_messages` counts all public channels it could not search. It lists 50 names by default; set `max_unsearched_channels` to see more names. Coverage states when that name list is shorter than the count.

`read_mattermost_pinned_messages` gives each pinned message with the fields
of `read_conversation`. The server does not say who pinned a message or
when, so this tool differs from the Slack tool `read_pinned_messages` and
has a platform name.

## Files of a message

A message's files reach the turn that the message starts, as content that
the backend gives the model. The connector reads them when the turn starts,
not when the message arrives. It does not read a file larger than 30 MiB.
The bot can read only the files of a channel that it can read. For each
file that it does not read, the connector posts a notice in the message's
thread with the reason, and the turn starts without the file.

## Second brain

The second brain ingests the documents and links of a message in each
channel that `second_brain.ingest.channels` selects. The entry `public`
selects every open channel (`O`). A direct message is never ingested. The
ingest marks its message with reactions: `brain_react_pending` while it
runs, then `brain_react_success`, `brain_react_failure` or
`brain_react_declined`. The corpus is in Belle's data directory, unless
`second_brain.corpus_dir` names another. See
[Configuration](../config.md).

### Who reads what

The tools keep the audience rule of the Slack tools:

- A post in another channel that reaches people who cannot read the turn's
  channel needs the requester's agreement. The host asks the turn's
  conversation with buttons, and makes the post once if a person agrees.
  Without the action listener, the host refuses the post with the code
  `approval_required`, and the detail is the question. A turn with no
  requester gets `write_target_widens_audience`.
- `read_conversation` reads another channel only where everybody who reads
  the turn's channel, and the requester, can read that channel too.
  Otherwise it refuses, and the detail counts the people who block the read.
- An open channel (`O`) is readable by every member of its team. A bot that
  is a team member reads an open channel that it has not joined. For a
  channel of another team, only the members of that team count as its
  readers.
- A private channel that the bot is not in cannot be read. The tool reports
  the server's refusal.
- `download_file` reads a file of another channel only where
  `read_conversation` may read that channel.
- The content tools `summarize`, `extract` and `classify` take a file id as
  `attachment_ref`, and read the file by the rule of `download_file`. See
  [Content tools](../tools.md).
- `edit_message` and `send_file` in another channel follow the rule of
  `post_message`.
- `read_mattermost_pinned_messages` reads another channel's pins only where
  `read_conversation` may read that channel.
- `search_messages` leaves out each match of a channel that
  `read_conversation` may not read. The result counts these matches and
  their channels, and does not name the channels.

The bot posts only in a channel that it is a member of. A post elsewhere
gets the code `forbidden`.

### Groups and bookmarks

`list_mattermost_groups` lists one page of groups associated with a channel
or its team. It returns group names and counts, not member identities. The
server may require a license or a bot grant for these routes. A refusal means
the resource is unavailable; an empty successful page means no groups were
found. Follow `coverage.next_page` when present.

`list_mattermost_bookmarks` reads one channel's bookmarks. The create, edit
and delete tools require the requester's channel membership and matching
Mattermost bookmark permission. Belle also asks for approval during the
current turn. A file bookmark can use a file from another channel only if
the requester can read that channel and every target reader can read the
file's source channel. The server's bookmark feature and bot grants must
also be available.

## Ambient channels and the decision gate

A scope with `delivery: {mode: [all], reply: optional}` gives each message
of a channel a turn, and the turn speaks only through `post_message`. A turn
that does not call it posts nothing.

```yaml
scopes:
- match: {channel: mattermost, chat: <channel id>}
  delivery:
    mode: [all]
    reply: optional
```

Where `models.decision` names a decision model, the host asks it about each
admitted message before the turn starts, and gives the turn its answers as
advice. The connector tells the model the earlier messages of the channel,
the bot's own among them, each with its author's user name, and the kind of
the channel. The model is told that a message mentions the bot as
`@<bot name>`. See [Decision questions](../config.md#decision-questions).
