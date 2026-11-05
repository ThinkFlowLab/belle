# Belle configuration

Belle reads one YAML file. Start from the [example](../examples/config/belle-config.yaml).
`belle_agent.paths.resolve_paths()` selects paths without creating directories.
`belle_agent.config.load_config(path)` reads a named file. `load_config(None)`
returns `{"platforms": {"slack": {"enforce_audience": True}}}`. The settings
keep the layout of the file. `belle_agent.config.platform_section(config, name)`
returns the section `platforms.<name>`.

| Override | Default |
| --- | --- |
| `BELLE_AGENT_CONFIG_DIR` | `$XDG_CONFIG_HOME/belle-agent`, else `~/.config/belle-agent` |
| `BELLE_AGENT_CONFIG_FILE` | `<config dir>/config.yaml` |
| `BELLE_AGENT_DATA_DIR` | `$XDG_DATA_HOME/belle-agent`, else `~/.local/share/belle-agent` |
| `BELLE_AGENT_STATE_DIR` | `$XDG_STATE_HOME/belle-agent`, else `~/.local/state/belle-agent` |
| `BELLE_AGENT_CACHE_DIR` | `$XDG_CACHE_HOME/belle-agent`, else `~/.cache/belle-agent` |
| `BELLE_AGENT_RUNTIME_DIR` | `$XDG_RUNTIME_DIR/belle-agent` when `XDG_RUNTIME_DIR` is absolute. Otherwise, unavailable. |

The config file selection order is `BELLE_AGENT_CONFIG_FILE`, an explicit
`BELLE_AGENT_CONFIG_DIR/config.yaml`, the older `BELLE_AGENT_CONFIG` file path,
then the XDG/default config directory. A missing file selected by an explicit
override raises `BelleConfigError`. If only the XDG/default config path applies
and no file exists, Belle uses defaults. `BELLE_AGENT_CONFIG` remains a temporary
fallback and still accepts a relative path.

New `BELLE_AGENT_*` path overrides must be absolute or start with `~`. Empty or
relative values raise `BelleConfigError`. Belle ignores relative `XDG_*` values.
The runtime path is `None` when neither a valid `XDG_RUNTIME_DIR` nor an explicit
runtime override exists. None of these paths creates a directory until a caller
uses it. Invalid YAML and invalid top-level types also raise `BelleConfigError`.

Keep `scopes`, `people`, `roles`, `second_brain`, and `models` in this file. Connector UX keys (`group_chat_mode`, acknowledgements, streaming, Block Kit, and related settings) also live here under `platforms.slack`. See [Slack connector](platforms/slack.md) for their effects. For a full JiuwenSwarm setup, use the matching [`belle/20261013` branches](integrations/jiuwenswarm.md#build-branches).

## Top-level keys

| Key | Type | Role |
| --- | --- | --- |
| `platforms` | Mapping or null | The section of each platform, by its name: `slack`, `mattermost`, `matrix`, `discord` and `loopback`. See [Platform sections](#platform-sections). |
| `scopes` | List or null | Conversation and tool rules. Omitted or null is fine. Scope compilation runs after `load_config`. |
| `people` | Mapping or null | Named people and platform IDs. |
| `roles` | Mapping or null | Named groups of people for scope rules. |
| `models` | Mapping or null | Belle-owned task models: `decision`, `summarizer`, `extractor` and `classifier`. Chat models remain with the backend. |
| `second_brain` | Mapping or null | Corpus ingestion and recall. |
| `scheduling` | Mapping | Scheduled jobs and their delivery policy. See [Scheduled jobs](scheduling.md). |
| `control` | Mapping | The local status socket. See [Local status socket](#local-status-socket). |
| `attached_sessions` | Mapping or null | Attached sessions on the harness. See [Attached sessions](#attached-sessions). |
| `backend`, `backends` | String, mapping | The launcher expands `${BELLE_BACKEND}` and reads only that backend's settings: `opencode`, `docker_agent` or `acp`, all three experimental. JiuwenSwarm ignores both. See [OpenCode](backends/opencode.md), [Docker Agent](backends/docker-agent.md) and [ACP](backends/acp.md). |

Belle refuses any other top-level key, such as a misspelt or retired section.
The error names the key and lists the sections. A key of the wrong type stops
the load with an error that names the key and the type.

Belle also refuses these retired keys. The error names the place of the
setting:

| Retired key | Place of the setting |
| --- | --- |
| `slack`, `mattermost`, `matrix`, `loopback` | `platforms.slack`, `platforms.mattermost`, `platforms.matrix`, `platforms.loopback` |
| `platform` | The `enabled` key of each platform section, such as `platforms.loopback.enabled` |
| `decision` | `models.decision.entries` |
| `second_brain.ingest.summarizer.api_base`, `.api_key`, `.client_provider` | `models.summarizer.entries` |

See [Conversation scopes](scopes.md) for matching and composition.

### Switch a harness between its integration and ACP

OpenCode and Docker Agent each run on two routes: Belle's integration for that
harness, and ACP, an experimental one. One deployment configuration holds both sections,
and `backend` alone selects the route. The example configuration reads it from
`BELLE_BACKEND`:

| Harness | Its integration | ACP |
| --- | --- | --- |
| OpenCode | `BELLE_BACKEND=opencode` reads `backends.opencode` | `BELLE_BACKEND=acp` reads `backends.acp`, with `profile: opencode` |
| Docker Agent | `BELLE_BACKEND=docker_agent` reads `backends.docker_agent` | `BELLE_BACKEND=acp` reads `backends.acp`, with `profile: docker_agent` |

```yaml
backend: ${BELLE_BACKEND}       # opencode, or acp
backends:
  opencode:
    url: ${OPENCODE_URL}
    tools: {bind: 0.0.0.0, port: 18300, url: ${BELLE_TOOLS_URL}}
  acp:
    profile: opencode
    tools: {bind: 0.0.0.0, port: 18300, url: ${BELLE_TOOLS_URL}}
    profiles:
      opencode:
        socket: ${BELLE_ACP_SOCKET}
        cwd: /work
        connection: shared
        tools_mount: session
        binding: {meta: ai.opencode/sessionID}
```

The harness container serves both routes when its relay runs beside its
server (see [the relay image](../containers/acp-relay/README.md)). The two
routes do not apply the same settings. At start, the host warns about each
setting that the selected route does not apply. For Docker Agent, read
[Bindings](backends/acp.md#bindings) before you select ACP. Metadata tool
bindings require `connection: shared`. Separate agent processes require
`connection: per_conversation` with `binding.header`, because their session
IDs can collide. See the
[feature matrix](feature-matrix.md#harnesses) and [ACP](backends/acp.md).

JiuwenSwarm hosts Belle in its own process, so its integration is not a value
of `backend`. See [ACP](backends/acp.md) for JiuwenSwarm over ACP.

## Secret fields

A secret field takes only an environment reference, such as
`${DECISION_API_KEY}`, or no value. Belle refuses a secret that the file
writes as a literal value. The error names the field and the reference form.
It does not show the value. Belle checks each value as the file writes it,
before it expands a reference.

| Field | Example reference |
| --- | --- |
| `models.<group>.entries[].api_key` | `${DECISION_API_KEY}` |
| `second_brain.retrieval.embeddings.api_key` | `${EMBEDDINGS_API_KEY}` |
| `backends.acp.tools_token` | `${BELLE_TOOLS_TOKEN}` |
| `platforms.slack.bot_token`, `platforms.slack.workspaces[].bot_token` | `${SLACK_BOT_TOKEN}` |
| `platforms.slack.app_token`, `platforms.slack.workspaces[].app_token` | `${SLACK_APP_TOKEN}` |

A reference is the full value: `${`, a name of capital letters, digits and
`_` that starts with a letter, then `}`. An empty string or null leaves the
field empty.

The other secrets have no field in this file. Belle reads them from the
environment only:

- Mattermost: `MATTERMOST_BOT_TOKEN` and `MATTERMOST_COMMAND_TOKEN`, or the
  variables that `bot_token_env` and `command_token_env` of a server name.
- Matrix: `MATRIX_STORE_PASSPHRASE`, `MATRIX_PASSWORD` and
  `MATRIX_RECOVERY_KEY`, or the variables that the `*_env` keys of an account
  name.
- Discord: `DISCORD_BOT_TOKEN`, or the variable that the `token_env` of a
  bot names.
- OpenCode: `OPENCODE_SERVER_PASSWORD` and `BELLE_RELAY_TOKEN`.
- Docker Agent: `DOCKER_AGENT_AUTH_TOKEN` and `BELLE_RELAY_TOKEN`.

The Mattermost, OpenCode and Docker Agent sections refuse a key that holds
one of these secrets.

## Scheduling

`scheduling.engine: auto` uses Belle's scheduler on OpenCode and Docker Agent. On JiuwenSwarm, it keeps the host scheduler. `disabled` serves no Belle schedule tools. See [Scheduled jobs](scheduling.md) for time forms, permissions, delivery, and recovery.

The scheduler stores jobs and runs in `<state dir>/sessions.json`. It uses a sibling lock file so two Belle processes can share the state. The `delivery` list allows target addresses. An empty list allows no target. Keep the state directory on persistent storage.

The store has version 4. Belle refuses a store of an earlier version at start. See [Store and failures](scheduling.md#store-and-failures).

| Key | Default | Meaning |
| --- | --- | --- |
| `engine` | `auto` | `auto`, `belle`, `host`, or `disabled`. |
| `max_jobs` | `50` | Maximum jobs in the deployment; at least 1. |
| `min_interval_seconds` | `300` | Minimum gap between runs of one job; at least 1. |
| `max_concurrent_runs` | `4` | Maximum runs at once; at least 1. |
| `max_pending_sessions` | `64` | Maximum requested runs that wait for a run slot, at least 1. The runs of `session_start`, `session_continue` and `schedule_run_now` count. At the limit, each of them refuses a new run. Runs that the clock starts do not count. |
| `job_visibility` | `conversation` | `conversation` or `all`. |
| `operator_role` | empty | A name in `roles`; empty gives no operator. |
| `default_zone` | empty | IANA zone for wall times without a zone; empty requires a job zone. |
| `delivery` | `[{to: {same: [platform, workspace]}}]` | Allow rules with `to` and optional `from` address patterns. |
| `kept_runs_per_job` | `20` | Ended runs kept for each job, at least 1. A `fresh` job keeps this many ended sessions. A `shared` job keeps this many ended runs in its session. |
| `session_retention_hours` | `72` | Hours to keep a detached session after its last run ends, at least 1. Then Belle deletes its conversation on the harness, and then its record. A detached session with a pending or running run, a notice to deliver, or a `shared` job stays. |
| `disable_after_failures` | `3` | Consecutive failed scheduled runs before a pause; 0 never pauses. |
| `question_timeout_seconds` | `900` | Seconds to wait for a target question; at least 1. The host also limits this to its turn timeout. |

Each delivery rule has a `to` mapping and may have a `from` mapping. Each mapping accepts string `platform`, `workspace`, and `conversation` patterns. `to.same` lists address fields that must match the origin. The default allows delivery on the same platform and workspace.

## Attached sessions

```yaml
attached_sessions:
  expire_after_idle_hours: disabled
```

| Key | Default | Meaning |
| --- | --- | --- |
| `expire_after_idle_hours` | `disabled` | `disabled`, or a whole number of hours, 1 or more. With a number, Belle deletes an attached session on the harness that number of hours after its last turn ended. The next message in that thread, channel or direct message starts with an empty conversation. A message that arrives during the deletion waits until it ends. Belle refuses a number at start when the backend cannot delete a conversation. Belle does not yet delete the session of a JiuwenSwarm chat place. So each JiuwenSwarm process that loads a Belle extension refuses a number at start, and the extension does not load. Write `disabled` in quotes or as the word: YAML reads an unquoted `off` or `no` as false, and Belle refuses false. |

An attached session is the harness session of a thread, of a channel with `session: channel`, or of a direct message. Every turn there uses it. Belle keeps no history of its turns. Belle deletes the conversation of a detached session when the detached session expires: see [Store and failures](scheduling.md#store-and-failures).

## Platform sections

Each platform has its section under `platforms`, by the platform's name, as
`backends` and `models` group theirs:

```yaml
platforms:
  slack:
    enforce_audience: true
  mattermost:
    url: ${MATTERMOST_URL}
```

`platforms.slack` must include `enforce_audience` as a YAML boolean. A name
under `platforms` that is not a platform stops the load with an error that
names it.

`python -m belle_agent.launcher` serves every platform whose section is on,
from one process. A section is on when its credentials resolve. Its
`enabled` key overrides that: `false` turns the section off, and `true` makes
a missing credential an error. A section with only some of its credentials
stops the start, and the error names each missing variable. The start also
stops when no section is on. See [Platforms](platforms/README.md).

The example configuration lists `slack`, `mattermost`, `matrix` and `discord` under `platforms`. The
variables of a platform turn it on: `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`
for Slack, `MATTERMOST_URL` and `MATTERMOST_BOT_TOKEN` for Mattermost,
`MATRIX_HOMESERVER`, `MATRIX_USER_ID` and `MATRIX_STORE_PASSPHRASE` for Matrix.
`platforms.slack.workspaces`, `platforms.mattermost.servers`,
`platforms.matrix.accounts` and `platforms.discord.bots` list several
installations of one platform. See
[Tokens and workspaces](#tokens-and-workspaces),
[Several Mattermost servers](platforms/mattermost.md#several-servers) and
[Several Matrix accounts](platforms/matrix.md#several-accounts), and
[Discord](platforms/discord.md).

### Discord (experimental)

`platforms.discord` connects one or more bot applications over Discord's REST API and gateway. Each bot
serves the guilds it belongs to and its direct messages. Without `bots`, its token comes from
`DISCORD_BOT_TOKEN`.
For several applications, set `bots` to a nonempty list of `{name: belle, token_env: DISCORD_BELLE_TOKEN}`
entries. Names are stable and unique, using letters, digits, underscores or hyphens. Each entry needs a
separate environment variable and a distinct nonempty token. Token values in YAML are refused.
Other Discord settings apply to every bot. Named bots use `name:guild_id` or `name:direct` as their
workspace; use that qualified value in `match.workspace`. Channel and user scope IDs remain raw Discord IDs.
The `/belle` command is registered globally. See [Discord](platforms/discord.md)
for the developer portal setup and current capabilities.

| Key | Default | Meaning |
| --- | --- | --- |
| `bots` | omitted | Named bot identities, each with `name` and `token_env`. Omit for the single-bot environment variable. |
| `message_content` | `true` | Ask for the privileged Message Content intent. Without it a guild message carries text only where it mentions the bot. The portal must grant it, or the start stops naming the toggle. |
| `guild_members` | `false` | Ask for the privileged Server Members intent: member events. |
| `member_event_channels` | `{}` | Optional map of guild IDs to fallback channel IDs for member events when the system channel is missing or unusable. |
| `reply_in_thread` | `true` | Answer a channel message in a thread started on it. `false`: in the channel. |
| `typing_indicator` | `true` | Show the bot as typing where its answer goes while a turn runs. |
| `enable_streaming` | `false` | Show a reply as it is written, in a message Belle edits about once a second, then ends as the final. |
| `activity_card` | `true` | Show a card of the tools a running turn calls, with a Stop button. A turn owed a reply gets the card with the Stop button alone until it calls a tool. A card of a turn that said nothing or was stopped, or that held only the Stop button, is deleted. |
| `activity_card_delay_seconds`, `activity_card_min_edit_seconds` | `5`, `10` | How long a turn runs before its card is posted, and the least time between two edits of the card. |
| `pending_events_max` | `1024` | Maximum Gateway events waiting per bot, excluding interactions. Use a whole number of at least 1. Overflow stops the connector and reports terminal health failure. |
| `file_download_max_bytes` | `31457280` | Maximum bytes fetched for one Discord attachment, including inbound files and `download_discord_attachment`. A larger file is refused whole. |
| `pending_questions_max`, `question_expiry_seconds` | `256`, `86400` | Maximum unanswered questions held in memory and their lifetime in seconds. Belle closes the oldest question at capacity and closes each question at expiry. A stale control cannot answer. |
| `allow_role_mentions`, `allow_everyone_mentions`, `allow_tts` | `false` | Permit these privileged Discord post forms only when the bot has the destination permission. Role pings also require the role to be mentionable or the bot to have `MENTION_EVERYONE`. |
| `allow_from`, `allowed_channel_ids` | empty | User ids answered, and channel ids served. Empty: everybody, and every channel. |
| `decision_questions_file`, `decision_forward` | `""`, `true` | The decision gate. See [Decision questions](#decision-questions). |
| `acknowledgement_emoji`, `rejected_emoji`, `queued_emoji`, `completed_emoji`, `failed_emoji`, `stopped_emoji` | 👀, 🚫, ⏳, `disabled`, ❌, ⏹️ | The reaction of each mark: a nonempty bare Unicode value (no spaces, at most 64 characters), a custom emoji as `name:id`, or `disabled`. Discord checks whether a bare value is an emoji when reacting. |
| `brain_react_pending`, `brain_react_success`, `brain_react_declined`, `brain_react_failure` | 📖, 🧠, ❌, ⚠️ | The reactions of a second brain ingest, in the same form. Discord checks whether each bare Unicode value is an emoji when reacting. |

### Matrix (experimental)

`platforms.matrix` connects one bot account, or several under `accounts`. It answers direct messages,
mentions and replies to the bot in encrypted and unencrypted rooms. A helper
process, `belle-matrix-helper`, holds the account's encryption. The launcher
image holds the helper. The section is on when `homeserver`, `user_id` and
`MATRIX_STORE_PASSPHRASE` resolve. See [Matrix](platforms/matrix.md) for the
setup, the first start and the operator commands.

A direct room with two members has the chat type `direct`. A room whose join
rule is `public` has `public`, and every other room `private`. A message
mentions the bot where its `m.mentions` names the bot's user ID. `@room` and
display names do not. Answers are notices. An answer larger than one event
goes in parts, cut between paragraphs and never inside a code block or a
table: at most 60,000 bytes each in an unencrypted room, and 44,000 in an
encrypted room.

A turn's tools are `post_message`, `react_to_message`, `read_conversation`,
`download_file`, `edit_message`, `delete_message`, `send_file`,
`search_messages`, `find_by_name`, `pin_message` and
`read_matrix_pinned_messages`. A post
that reaches people who cannot read the turn's room needs the requester's
approval, which Belle asks with a poll: a room whose join rule is `public`
reaches anybody. `read_conversation` lists a message that the bot holds no key for,
such as one sent before it joined, as `undecryptable`, and its coverage
counts them.

The file of a message (`m.image`, `m.file`, `m.audio` or `m.video`) reaches
the turn that the message starts. The helper downloads it and decrypts it
in an encrypted room. The bot reads at most `file_download_max_bytes` of one
file, 30 MiB by default. The notice for a larger file
names the limit. The second brain's `public` entry selects a public room
without encryption only. `*` also leaves out encrypted rooms. Only the
room's ID in `second_brain.ingest.channels`, in a list with no `*`, selects
an encrypted room. See
[Matrix](platforms/matrix.md#second-brain).

| Key | Default | Meaning |
| --- | --- | --- |
| `homeserver` | none | The URL of the homeserver's client API, `http` or `https`. |
| `user_id` | none | The bot account's full user ID, such as `@belle:example.org`. |
| `device_name` | `belle` | The name of a new device. |
| `store_dir` | `<state dir>/matrix/default` | The state directory is the launcher's `--state-dir`, else `BELLE_AGENT_STATE_DIR`. The session and the encrypted crypto store. Keep it on a volume. Never copy it back from a backup: the platform stops on a store that is an old copy. |
| `recovery_key_output` | empty. The example sets `${MATRIX_RECOVERY_KEY_OUTPUT}`. | The file that the first start of an account, and the operator command `reset-identity`, write the new recovery key to. It must not exist, and it must be outside `store_dir`. Put it on another volume than the store. |
| `accounts` | empty | Several accounts in one process, one entry each. A non-empty list takes the place of `homeserver`, `user_id`, `store_dir` and `recovery_key_output`, and each entry names the variables of its secrets. See [Several accounts](platforms/matrix.md#several-accounts). |
| `helper_path` | `belle-matrix-helper` | The helper's executable, as a path or a name on `PATH`. |
| `allow_from` | empty | Full user IDs of the people Belle answers. The bot also joins only the rooms that these people invite it to, and declines other invites. Empty: everybody. |
| `allowed_channel_ids` | empty | The room IDs Belle answers in. Empty: every room the bot is in. |
| `bot_user_ids` | empty | Full user IDs whose messages and reactions count as a bot's. Their reactions start no turn, and their ⏹️ stops none. A notice (`m.notice`) always counts, and so do the other accounts of `accounts`. |
| `catch_up_seconds` | `300` | A message that reaches Belle later than this after it was sent, such as after downtime, starts no turn. Time a message waits while Belle handles earlier ones does not count. A message owed an answer gets the `late_emoji` reaction, and Belle logs how many it skipped. |
| `typing_indicator` | `true` | Send typing notices in a room while a turn of it runs. |
| `history_max_total_chars` | `200000` | The most characters of text that one tool result holds. `download_file` refuses a longer text file whole. |
| `membership_history_max_events` | `20` | Maximum membership events followed to determine which Matrix history a requester may read. If the chain continues, the read is refused; raise this setting to retry. |
| `file_download_max_bytes` | `31457280` | Maximum bytes fetched for one Matrix media file, including inbound files and `download_file`. The homeserver's upload limit does not constrain reads. |
| `file_upload_max_bytes` | `31457280` | Maximum bytes sent in one `send_file` call; the homeserver may impose a lower upload limit. |
| `command_name` | `belle` | The word of the text command, after `!` or `/`: `!belle help`. A command is the first word of a message, or the first after a mention of the bot. |
| `enable_streaming` | `false` | Put a reply in a notice as the model writes it, and edit the notice in place. A notice holds at most 30,000 bytes of text and HTML, or 22,000 in an encrypted room, and the reply goes on in the next. |
| `activity_card` | `true` | Post a card of the turn's tool calls, as a notice in its thread. The bot adds ⏹️ to the card. A person's ⏹️ on the card stops the turn. |
| `activity_card_delay_seconds` | `5` | Post the card only once the turn has run this long. |
| `activity_card_min_edit_seconds` | `10` | Edit the card no more often than this. A rate limit of the server makes each edit of the card and of a streamed reply wait, so the edits become less frequent. |
| `acknowledgement_emoji`, `rejected_emoji`, `late_emoji`, `queued_emoji`, `completed_emoji`, `failed_emoji`, `stopped_emoji` | `eyes`, `no_entry_sign`, `alarm_clock`, `hourglass_flowing_sand`, `disabled`, `x`, `black_square_for_stop` | The reaction of each mark: a shortcode of `GLYPH_SHORTCODES` in `belle_agent/emoji.py`, an emoji character, or `disabled`. Matrix has no message for one person, so the rejected reaction is the only sign of a refused message, and the late reaction the only sign of a message owed an answer that came later than `catch_up_seconds`. |
| `brain_react_pending`, `brain_react_success`, `brain_react_declined`, `brain_react_failure` | `open_book`, `brain`, `x`, `warning` | The reactions of a second brain ingest, in the same form. |

Belle reads each secret from its variable, or from the file that the
variable of the same name with `_FILE` names. Belle refuses both at once.

| Variable | Use |
| --- | --- |
| `MATRIX_STORE_PASSPHRASE` | Encrypts the store. Always required. |
| `MATRIX_PASSWORD` | The account's password, for the first start of a device and for the operator commands `delete-devices` and `reset-identity`. |
| `MATRIX_RECOVERY_KEY` | Recovers a new device of an account that has its encryption identity. Prefer `MATRIX_RECOVERY_KEY_FILE` where a shell tool runs in Belle's container. |

With `accounts`, each entry names its own variables in `store_passphrase_env`,
`password_env` and `recovery_key_env`, and Belle reads neither of the three
variables above.

The platform starts only with a device that its owner signed (cross-signed).
A refused start names the variable or key to set. The helper locks its store
and refuses a second process on it. A helper that exits starts
again after 1, 2, 4 ... 60 s. After 5 exits within 5 minutes, Matrix stays
down and logs why, and the other platforms of the host go on. When it was
the last platform that serves, the launcher exits with 1.

## JiuwenSwarm host vs Belle `platforms.slack`

When Belle runs as the JiuwenSwarm Slack channel extension, connection admission stays under JiuwenSwarm `channels.slack`. Belle-owned connector keys stay under Belle `platforms.slack`. Overlap raises at merge time.

| Location | Keys |
| --- | --- |
| JiuwenSwarm `channels.slack` | `enabled`, `default_channel_id`, `allow_from`, `allowed_channel_ids`, `session`, `send_file_allowed`, and usually the token pair |
| Belle `platforms.slack` | `workspaces`, `command_name`, `app_messages`, `app_messages_from`, `optional_reply_mode`, `group_chat_mode`, `acknowledge_mode`, acknowledgement and status strings/emojis, `session_surface`, `decision_*`, streaming and activity-card keys, Block Kit / table render keys, digital avatar and naming keys, `enable_memory`, `home_tab_welcome`, history limits, `search_enabled`, `enforce_audience`, and the rest of the example file |

Both places take an `enabled` key, with different meanings. JiuwenSwarm's
`channels.slack.enabled` turns its Slack channel on and off. Belle's
`platforms.slack.enabled` turns Belle's Slack section on and off. The JiuwenSwarm
channel serves Slack only, so Belle's key may be absent or `true`. With
`false`, the merge raises an error that names the key.

`reply_in_thread` may appear in either place. A Belle value overrides the host boolean (`true` → `multi`, `false` → `none`). Full key tables live in [Slack connector](platforms/slack.md).

## Tokens and workspaces

`${NAME}` in string fields expands from the environment. Unset variables become empty strings.

For one Socket Mode connection, nonempty values resolve in this order for each of `bot_token` and `app_token`:

1. Belle `platforms.slack.bot_token` / `platforms.slack.app_token`
2. JiuwenSwarm `channels.slack` pair (when the channel extension is loaded)
3. `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` on the single-workspace path (`workspaces` empty or absent)

A nonempty `platforms.slack.workspaces` list takes precedence over the single token pair. Each list entry keeps only `bot_token`, `app_token`, `enabled`, and `default_channel_id`. Belle drops other keys in an entry with a warning. Each entry supplies its own tokens as `${NAME}` references. See [Secret fields](#secret-fields).

Typical JiuwenSwarm deploys keep tokens under `channels.slack` (or the env vars it references) so the built-in connector still has credentials if the Belle extension is absent. Standalone Belle reads the pair from the environment, through references in this file or through the variables.

| Key | Type | Value |
| --- | --- | --- |
| `platforms.slack.bot_token` | String | Bot token for one Socket Mode connection. |
| `platforms.slack.app_token` | String | App token for that connection. |
| `platforms.slack.workspaces` | List | Separate credentials for multiple installations. |

## Audience, search, and history

| Key | Type | Value |
| --- | --- | --- |
| `platforms.slack.enforce_audience` | Boolean | Required in an explicit file. `true` enforces Slack audience bounds on reads and posts. |
| `platforms.slack.search_enabled` | Boolean | Enables Slack search for addressed messages with an action token. Defaults to `true`. |
| `platforms.slack.history_max_messages` | Integer | Maximum messages returned by one history call. Defaults to `2000`. |
| `platforms.slack.history_max_total_chars` | Integer | Maximum combined message text. Defaults to `200000`. |
| `platforms.slack.canvas_read_max_chars` | Positive integer | Maximum characters returned by `read_slack_canvas`. Defaults to `60000`. Larger canvases are refused whole. |
| `platforms.slack.canvas_sections_max_items` | Positive integer | Maximum matching canvas section IDs returned by one read. Defaults to `500`; coverage reports the full match count if this limit binds. |
| `platforms.slack.lists_export_max_bytes` | Positive integer | Maximum bytes fetched for a List export. Defaults to `4194304`. A larger export returns its job ID and a size refusal, without partial content. |
| `platforms.slack.lists_export_max_chars` | Positive integer | Maximum characters returned for a List export. Defaults to `60000`. A larger export is refused whole. |
| `platforms.slack.file_download_max_bytes` | Positive integer | Maximum bytes fetched for one Slack file, including inbound attachments and `download_slack_file`. Defaults to `31457280`. The transfer stops at this limit. |
| `platforms.slack.directory_scan_max_pages` | Positive integer | Maximum Slack pages scanned per resource kind by `find_by_name`. Defaults to `25`; increase it for larger directories. |
| `platforms.slack.history_max_message_chars` | Integer | Maximum characters from one message. Defaults to `4000`. |
| `platforms.slack.history_max_roots_scanned` | Integer | Maximum thread roots scanned. Defaults to `10000`. |
| `platforms.slack.history_max_api_calls` | Integer | Maximum Slack API calls per history call. Defaults to `200`. |
| `platforms.slack.history_max_user_lookups` | Integer | Maximum author-name lookups. Defaults to `50`. |
| `platforms.slack.history_scan_timeout_seconds` | Number | Maximum history scan time in seconds. Must be finite and at least `0.001`. Defaults to `90`. |
| `platforms.slack.history_members_cache_seconds` | Number | Membership cache lifetime in seconds. Must be finite and from `0.001` to `900`. Defaults to `60`. |

`platforms.slack.history_never_read` and `platforms.slack.history_exempt_members` are rejected with `BelleConfigError`.

## Task models

See [Task models](task-models.md) for what each group does and which features use it.

Each entry under `models.decision`, `models.summarizer`, `models.extractor`, or `models.classifier` has a unique `id`, an `api`, and connection fields `api_base` and `api_key`. Set `model_name` for APIs that accept one. ScaleDown's `/classify` does not use it. Connection fields accept `${NAME}`, and `api_key` takes only a reference. See [Secret fields](#secret-fields). The `id` is local to Belle. Set `models.decision.default` when more than one decision entry exists. Empty decision credentials disable decision calls.

| `models.decision.entries[].api` | `api_base` example | `model_name` example | Question format |
| --- | --- | --- | --- |
| `openrouter` | `https://openrouter.ai/api/v1` | `typesafe/jev-1.13` | OpenRouter Decisions (`noul`, `choice`, `score`) |
| `openrouter_text_state` | `https://openrouter.ai/api/v1` | `scaledown/classify-1` | OpenRouter Decisions with the full state serialized as JSON text (`noul`, `choice`, `score`) |
| `openai` | `https://api.openai.com/v1` | `gpt-6-luna` | Native OpenAI Decisions (`predicate`, `choice`, `score`) |
| `scaledown` | `https://api.scaledown.xyz` | `classify-1` | Native ScaleDown Decisions (`noul`, `choice`, `score`) |

Belle converts its questions and answers for the native OpenAI API. `model_config_obj.extra_body` works only with OpenRouter APIs. A Second Brain summarizer entry can use `api: chat_completions`, `openrouter`, or `scaledown`. `client_provider` accepts only `OpenAI` or omission; another value stops configuration loading. Belle sends `chat_completions` requests directly to `api_base` + `/chat/completions`. The `second_brain.ingest.summarizer.model` value selects a summarizer entry by `id`. With one entry and no selector, Belle uses that entry. With multiple entries, set this selector or `models.summarizer.default`. The `second_brain.ingest.summarizer.outline_model` value selects the entry for the outline notes by `id`. Without it, the prose entry also writes the outline notes. A summarize-only model, such as direct ScaleDown, writes prose but does not fill the outline notes. Set `outline_model` to a chat entry for the notes. An `outline_model` that names no entry stops the configuration load with the error `Belle config second_brain.ingest.summarizer.outline_model must name a models.summarizer entry id`.

The agent tools `summarize`, `extract`, and `classify` use the selected entries in `models.summarizer`, `models.extractor`, and `models.classifier`. Summarize and Extract accept `api: scaledown` or `api: openrouter`. Classify accepts `api: scaledown`. A `chat_completions` summarizer entry remains available to Second Brain but does not expose the `summarize` tool. With multiple entries, set the group's `default` for its tool. A group with no usable selected entry exposes no corresponding tool.

For `scaledown/classify-1` with an OpenRouter key, use `api: openrouter_text_state`. For a direct ScaleDown key, use `api: scaledown` and `model_name: classify-1`. Both send the complete Belle state as JSON text. The native adapter puts it in `state.text` and uses the `x-api-key` header.

To test Classify-1 with an OpenRouter key, set the decision group to:

```yaml
models:
  decision:
    default: classify
    entries:
      - id: classify
        api: openrouter_text_state
        api_base: ${DECISION_API_BASE}
        api_key: ${DECISION_API_KEY}
        model_name: scaledown/classify-1
```

The native ScaleDown API accepts score questions with 2 to 10 levels. A `scaledown` decision entry's `model_name` must be `classify` or `classify-1`.

The `api` of an entry sets the request that Belle sends:

| `api` | Groups | Request | Key header |
| --- | --- | --- | --- |
| `openrouter` | `decision` | `/api/alpha/decisions` on the host of `api_base`. Belle ignores the path of `api_base`. | `Authorization: Bearer` |
| `openrouter_text_state` | `decision` | The same as `openrouter`. | `Authorization: Bearer` |
| `openai` | `decision` | `api_base` + `/decisions`. `api_base` must end in `/v1`. | `Authorization: Bearer` |
| `scaledown` | `decision` | `/v1/scaledown` on the host of `api_base`. | `x-api-key` |
| `openrouter` | `summarizer`, `extractor` | `api_base` + `/chat/completions`. | `Authorization: Bearer` |
| `scaledown` | `summarizer`, `extractor` | `api_base` + `/v1/chat/completions`. Belle adds `/v1` only when `api_base` does not end in `/v1`. | `x-api-key` |
| `scaledown` | `classifier` | `api_base` + `/classify`, without a final `/v1` of `api_base`. | `x-api-key` |
| `chat_completions` | `summarizer` | Second Brain only. `api_base` + `/chat/completions`. | `Authorization: Bearer` |

Second Brain sends an `openrouter` or `scaledown` summarizer entry the request of the `summarize` tool. Without `model_name`, the model is `scaledown/summarize` for `openrouter` and `summarize` for `scaledown`. The `openrouter` request sends `temperature: 0.2`, the same as the chat request. The `scaledown` request sends no temperature, because Belle has no test of its effect on ScaleDown. Belle reads the reply text from `choices[0].message.content`. Direct ScaleDown puts a JSON object there, and Belle reads its `summary` field. A reply without this text gives no prose, and Belle logs the failed summary with the exception type.

If an `openai` decision entry's `api_base` does not end in `/v1`, Belle makes no decision calls. The error is `OpenAI decision api_base must be an absolute URL ending in /v1`. A `scaledown` decision entry with a different `model_name` also stops decision calls. Chat continues in both cases.

Self-hosted endpoints:

- An `openai` decision entry accepts any `api_base` that ends in `/v1`. A server that implements the OpenAI Decisions API at that address can answer it. A server with only chat completions has no `/decisions` endpoint.
- A `chat_completions` summarizer entry accepts an OpenAI-compatible chat endpoint, for example a self-hosted server.
- `openrouter` summarizer and extractor entries send chat completions requests to `api_base` + `/chat/completions`. Belle accepts any host. Belle has no test of `summarize` or `extract` with a self-hosted server.

On JiuwenSwarm, with no `models.summarizer` entry, the Second Brain summarizer is a model of the JiuwenSwarm model registry. `second_brain.ingest.summarizer.model` then names a model of that registry. A selected `openrouter` or `scaledown` entry without `api_base` also uses the registry, and its `model_name` names the registry model. In both cases, Belle logs a deprecation warning.

## Decision questions

The keys of this section are in the section of each platform: `platforms.slack` and `platforms.mattermost`.

By default, successful typed-decision answers and their question text reach the triggering agent turn as advice. Set `platforms.slack.decision_forward: false` to log answers only. Without a configured decision model, turns proceed unchanged. The decision outcome does not choose whether the agent replies.

The connector acknowledges the message before the decision call. It waits up to eight seconds for advice. These eight seconds include the history fetch and the decision call. The decision call has a separate deadline of four seconds. In these four seconds, Belle retries a failed call one time, after a pause of 0.25 seconds. A first attempt that times out leaves no time for the retry. A call that fails or is too late gives no advice. The turn then starts without it. For queued messages, observation happens on arrival and advice expires before their turn starts. Reaction, pin, and member turns skip the decision model. The history window includes bot messages and stops before the triggering message.

`platforms.slack.decision_questions_file` names a UTF-8 JSON object. `platforms.mattermost.decision_questions_file` and `platforms.mattermost.decision_forward` do the same for Mattermost, and the `platforms.matrix` keys of the same names for Matrix. Relative paths start at the Belle configuration directory. Each key is a question ID. A definition replaces a built-in question or adds one. `null` disables it. The built-in `independent_work` question estimates whether the requested work can reach a useful result without more input after acknowledgement. The `continuation_context` question estimates whether the request extends or changes earlier work whose details matter. Both answers give the agent advice. Neither identifies an existing session or chooses a tool:

```json
{
  "needs_review": {
    "type": "noul",
    "instructions": "The last message asks for a review of a concrete artifact."
  },
  "already_answered": null
}
```

Supported types are `noul`, `choice`, and `score`. A `noul` has `type` and `instructions`. A `choice` also has `criteria` as an option-to-description object. A `score` uses an ordered array of at least two levels. Invalid JSON or definitions disable the decision gate for that connector instance and leave ordinary chat available.

## Second brain

`second_brain` controls corpus ingestion and recall. For what it ingests, how recall works and who can see recalled content, see [Second Brain](second-brain.md). It is enabled by default and ingests from public channels. A platform that cannot identify public conversations ingests nothing by default. Discord selects no channels with `public`: use explicit parent channel IDs to include their delivered threads, including private threads. Those documents enter a shared corpus; source permissions are not reapplied during recall. See [Discord ingestion](platforms/discord.md#second-brain-ingestion). Set `enabled: false` to turn off the feature. Set `ingest.channels: []` to keep recall but disable ingestion. An empty `corpus_dir` uses `<BELLE_AGENT_DATA_DIR>/second_brain`. A relative path starts at the agent workspace. An absolute path is used as written. When configured, `models.summarizer` supplies the prose model under every backend. Belle keeps the corpus index beside the summaries, on every host.

| Area | Keys |
| --- | --- |
| Root | `enabled`, `corpus_dir` |
| `ingest` | `channels` (`public`, `*`, or channel IDs, never DMs. Only its ID selects an end-to-end encrypted conversation.), `max_file_bytes` |
| `ingest.link` | `enabled`, `deny_hosts`, `min_chars`, `max_chars`, `fetch_timeout_seconds` |
| `ingest.summarizer` | `model`, `outline_model`, `max_input_chars`, `prose_lines`, `timeout_seconds` |
| `retrieval` | `top_k`, `min_score`, `max_total_bytes`, `use_embeddings`, `vector_weight`, `text_weight` |
| `retrieval.embeddings` | `api_base`, `api_key`, `model` |

These keys have defaults in the code. An empty or zero value also gives the default:

| Key | Default |
| --- | --- |
| `ingest.max_file_bytes` | `52428800` (50 MiB) |
| `ingest.link.enabled` | `true` |
| `ingest.link.deny_hosts` | Empty. The example file lists some hosts. |
| `ingest.link.min_chars` | `600` |
| `ingest.link.max_chars` | `400000` |
| `ingest.link.fetch_timeout_seconds` | `20` |
| `retrieval.top_k` | `5` |
| `retrieval.min_score` | `0.35` |
| `retrieval.max_total_bytes` | `10240`. Belle uses a minimum of `1024`. |

Other defaults and comments are in the [example file](../examples/config/belle-config.yaml). Ingestion needs `files:read` and `reactions:write` on the Slack app.

If neither a Belle entry nor, on JiuwenSwarm, a registry model is available, Belle still stores and indexes the document but produces no model-written prose. The embedder's endpoint is `retrieval.embeddings`: `api_base`, `api_key` and `model` of an OpenAI-compatible endpoint. They accept `${VAR}` references, and `api_key` takes only a reference. An empty `api_key` sends no authorization header. An `api_base` must be an absolute HTTP URL without query or fragment.

The index is SQLite full text with BM25 ranking, and a vector for each chunk when `use_embeddings` is true and an embedder is set. `use_embeddings` is false by default, and the `SECOND_BRAIN_USE_EMBEDDINGS` environment variable overrides it. A search then mixes the cosine of each chunk's vector with its lexical score, by `vector_weight` and `text_weight`, 0.7 and 0.3 by default. Without an embedder, or with `use_embeddings: false`, ranking is lexical and nothing is embedded. The first start with an embedder embeds every summary, which takes a while on a large corpus. Later syncs embed only the chunks that changed. A change of embedding model embeds the corpus again. If the embedder fails, the index stays lexical and the next sync tries again.

Set the environment variable `SECOND_BRAIN_DEBUG_INJECTION=1` to record each recall in the corpus directory. Any nonempty value other than `0`, `false`, `no` or `off` also turns it on. For each recall, Belle adds one JSON line to `injections.jsonl`. The line has the query, the outcome, the hits and their scores, and the IDs of the injected documents. Belle also writes `last_injected.md` again, with the query and the full text that the last recall injected. These files keep message text and recalled corpus content. Belle does not rotate or delete them. Use the variable only to debug recall. Delete the files after debugging.

On JiuwenSwarm, an embeddings endpoint left without `api_base` falls back to the `embed` section of JiuwenSwarm's configuration and its `EMBED_*` variables. JiuwenSwarm's memory searched by vector whenever an embedder was configured. Set `use_embeddings: true` to keep that. The index is `lexical_index.db` in the corpus directory. A corpus that JiuwenSwarm's memory indexed before is indexed again at the first start, and its `memory/memory.db` is no longer read and can be deleted.

For JiuwenSwarm, link `belle-second-brain` with the other Belle extensions. See [JiuwenSwarm setup](integrations/jiuwenswarm.md#2-link-belle-extensions). Without the link, `brain.ingest` has no AgentServer handler.

## Local status socket

Set `control.enabled: true` to serve status and allow local operator job
pause/resume from the standalone Belle host. The default is false. Set
`control.peek: true` to allow live turn text peeks on the same socket
(`peek.open` / `peek.poll` / `peek.close`). When false, the socket serves
metadata only. Belle keeps no turn transcript of its own; a backend that
advertises `peek_history` may seed the live buffer once on first open.
Optional operator resource limits (defaults 200 frames, 65536 bytes, 4
subscribers, 300s idle): `peek_max_frames`, `peek_max_bytes`,
`peek_max_subscribers`, and `peek_subscriber_idle_seconds` (closes an idle peek
on the next open/poll that reaps after no open/poll for that long so crashed
clients do not hold slots).
Set `BELLE_AGENT_RUNTIME_DIR` to an absolute private
directory, or use `XDG_RUNTIME_DIR/belle-agent`. The directory must
belong to Belle's UID and have mode `0700`. Missing runtime paths or unsafe
permissions disable status serving with a warning.

Readers connect to `control.sock` as the same UID. See the
[local status protocol](control/protocol.md) for the probe command, fields,
resource budgets, and unavailable measurements.
