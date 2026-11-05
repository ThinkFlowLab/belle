# Slack connector

Slack uses Bolt Socket Mode. It needs no public callback URL.

## Install the Slack app

Create the app from a shipped manifest. Missing scopes require an app reinstall.

The manifests ship with Belle Agent in `src/belle_agent/platforms/slack/manifests/`. Each tier includes the previous tier's grants:

| file | bot scopes | what it installs |
| --- | --- | --- |
| `slack-app-manifest-0-core.yaml` | 6 | Receives a mention or a direct message and answers it. Nothing else. |
| `slack-app-manifest-1-connector.yaml` | 10 | Core, plus the acknowledgement reaction, files in both directions, and the `/belle` slash command. |
| `slack-app-manifest-2-tools.yaml` | 35 | Connector features plus history, pins, bookmarks, reactions, and search tools. |
| `slack-app-manifest-3-gated.yaml` | 36 | Tools plus Slack's agent surface: `features.agent_view` and `assistant:write`. |

Tier 0 cannot read inbound attachments. Features refuse operations when their required scopes are missing.

Tiers 0 to 2 need only their listed scopes, but Slack may still gate features such as Lists and canvases. Tier 3 also needs Slack's agent feature. If Slack rejects its manifest with `feature_disabled`, check whether the workspace has that feature. Slack documents no plan requirement for it.

Tier 3's `agent_view` shows conversations as a timeline above the Messages composer. To remove it, edit the Slack app. Tier 2 still shows thread status through `assistant.threads.setStatus`. Slack's own stop button on that status works only on tier 3: Slack lets only an app declared as an agent stop its session, and on a lower tier its control reads "Stop not available for this agent". On every tier, the Stop button on Belle's activity card stops a turn. Tiers 1 to 3 also declare `/belle`, so `/belle stop` works there too.

1. Open [Slack API Apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**, select the target workspace, and paste the manifest file.
2. Under **Basic Information → App-Level Tokens**, create a token with `connections:write`. Save its `xapp-...` value.
3. **Install to Workspace**, and save the generated `xoxb-...` Bot Token.
4. Add the bot to each channel where it should respond. Manifests cannot grant channel membership.

> Treat both `xoxb-...` and `xapp-...` tokens as secrets. Never commit them or print them in logs.

To change tiers, paste the new manifest on the app's **App Manifest** page, save, and reinstall. Slack applies scope changes after reinstalling.

The manifests mark two optional edits:

- Remove `groups:history` with `message.groups` to exclude private channels. Remove `mpim:history` with `message.mpim` to exclude group DMs.
- If Slack refuses tier 2 search grants, remove the six `search:read.*` lines and set Belle's `platforms.slack.search_enabled: false`.

Belle Agent's `scripts/generate_slack_manifest.py` generates the manifests from `belle_agent.platforms.slack.scope_policy`, which maps Slack calls and events to scopes. Update that policy when adding a scope to the connector.

## Connect through JiuwenSwarm

Belle's channel extension replaces JiuwenSwarm's built-in Slack connector.
Without the extension, JiuwenSwarm uses its built-in connector and
`channels.slack` credentials. To use Belle, expose the installed extension
directory to JiuwenSwarm's extension loader and allow the channel override:

Install the [agent plugin extension](../integrations/jiuwenswarm.md#2-link-belle-extensions)
alongside the Slack channel extension. Gateway and AgentServer both load it.

```sh
mkdir -p ~/.jiuwenswarm/application_plugins
EXT="$(python -c 'from importlib.resources import files; print(files("belle_agent.integrations.jiuwenswarm").joinpath("extensions"))')"
ln -sfn "$EXT/slack" ~/.jiuwenswarm/application_plugins/belle-slack
```

Set these JiuwenSwarm keys, then restart. The extension needs a `channels.slack`
mapping to enable the channel.

```yaml
extensions:
  allow_overrides: [channels.slack]
channels:
  slack:
    enabled: true
    bot_token: ${SLACK_BOT_TOKEN}
    app_token: ${SLACK_APP_TOKEN}
```

Keep the token pair under JiuwenSwarm's `channels.slack` if the built-in
connector must work when the extension is absent. Belle uses that pair when
its own token keys are unset. In a standalone Belle deployment, set the
environment variables or put token references in Belle's config:

```yaml
platforms:
  slack:
    enforce_audience: true
    bot_token: ${SLACK_BOT_TOKEN}
    app_token: ${SLACK_APP_TOKEN}
```

Set `BELLE_AGENT_CONFIG_FILE` to the absolute path of Belle's YAML file.
Keep tokens outside version control. An invalid Belle config prevents its
channel from starting.

#### Connections and message admission

`enabled`, `default_channel_id`, `allow_from`, `allowed_channel_ids`,
`session`, and `send_file_allowed` remain under JiuwenSwarm's `channels.slack`.
Belle's `platforms.slack.reply_in_thread` overrides JiuwenSwarm's boolean setting.
Without a Belle value, `true` means `multi` and `false` means `none`.
The token pair may be in Belle's `platforms.slack` block.
For each token, Belle uses its nonempty value first, then JiuwenSwarm's
`channels.slack` value, then the corresponding environment variable.
Without the extension, JiuwenSwarm reads only its own keys. Other settings
in this guide belong under Belle's `platforms.slack` block.

| Key | Type (default) | Effect |
| --- | --- | --- |
| `bot_token` | string (default: empty) | An environment reference, such as `${SLACK_BOT_TOKEN}`, to the bot Web API token (`xoxb-...`) of a single workspace. Belle refuses a literal token. |
| `app_token` | string (default: empty) | An environment reference, such as `${SLACK_APP_TOKEN}`, to the Socket Mode token (`xapp-...`) with `connections:write`. Belle refuses a literal token. |
| `workspaces` | list (default: `[]`) | Several installations in one process. Each entry accepts only `bot_token`, `app_token`, `enabled`, and `default_channel_id`. A nonempty list takes precedence over the top-level token pair. `group_digital_avatar` must be off when more than one workspace is enabled. |
| `enabled` | boolean (default: empty) | Enable the connector. Empty connects when both tokens resolve, and `false` turns it off. A workspace entry without `enabled` connects when both tokens are present. |
| `default_channel_id` | string (default: empty) | Destination for outbound messages without request context. A workspace entry can override it. |
| `allow_from` | list of user IDs (default: `[]`) | Accept these senders. Empty accepts all human senders. |
| `allowed_channel_ids` | list of conversation IDs (default: `[]`) | Accept these channels. Empty accepts all channels. DMs are unaffected. An exact-chat delivery, model, or history scope can opt in an omitted channel. |
| `app_messages` | `none`, `listed`, or `all` (default: `none`) | Admit app-posted messages with a `user` field. The bot's own messages and app messages without a user remain excluded. Slack's own users, Slack (`USLACK`) and Slackbot (`USLACKBOT`), count as an app with no app ID, so only `all` admits their posts. Their direct messages with the bot, such as the notice that the bot was removed from a channel, never start a turn: Slack refuses a reply there. |
| `app_messages_from` | list of app IDs (default: `[]`) | App IDs admitted when `app_messages: listed`. Use `A...` app IDs, not installation-specific bot IDs. |
| `group_chat_mode` | `mention`, `reply`, `all`, or `off` (default: `mention`) | Connector-wide channel trigger. `reply` also reads replies under the bot's messages. `all` reads other channel messages. DMs are unaffected. |
| `reply_in_thread` | `none`, `multi`, or `all` (default: `multi`) | Where a root-level message opens a thread. `multi` covers public channels, private channels, and group DMs. `all` adds the one-to-one DM. A message already in a thread is answered there under every word. `true` reads as `multi` and `false` as `none`. |
| `session` | `thread` or `channel` (default: `thread`) | Connector-wide session boundary for non-DM messages. An explicit scope `delivery.session` takes precedence. DMs stay per user. |
| `optional_reply_mode` | `sentinel`, `sentinel_unstored`, or `post_tool` (default: `post_tool`) | How a room whose `reply` is `optional` expresses silence. `sentinel`: the turn answers with exactly `NO_REPLY` and the connector withholds that reply. `sentinel_unstored`: the same, and a withheld reply leaves no message in the context. `post_tool`: no token. The connector withholds every reply in such a room. The turn reaches the conversation through `post_message`. The call must name a destination. An unrecognised word uses the default. |

Each workspace needs its own app and tokens. `workspaces` belongs in Belle Agent; connection credentials can also use JiuwenSwarm's single-workspace token pair. Other Belle keys apply to every connection. Use [Slack scope rules](#slack-scope-rules) for conversation-specific behavior. Before admitting another app, check its sender IDs: allow lists, scopes, and sessions trust them.

#### History and search tools

Set history limits and `search_enabled` in Belle's `slack` block. See Belle's `docs/config.md` for keys and defaults. The Slack app still needs the matching search scopes. Cron runs cannot search.

History and search send selected Slack content to the model provider. Slack scopes and bot membership still apply.

#### Feedback, streaming, and decisions

| Key | Type (default) | Effect |
| --- | --- | --- |
| `acknowledge_mode` | `reaction`, `text`, `both`, or `off` (default: `reaction`) | Acknowledge accepted requests before the turn starts. |
| `acknowledgement_text` | string (default: `Received. Analyzing…`) | Text for `text` or `both` mode. |
| `thinking_status` | string (default: `is thinking…`) | Thread status during a turn. Empty disables it. `acknowledge_mode: off` disables all statuses. |
| `queued_status` | string (default: `has queued a message`) | Thread status when a message waits. Blank uses the default. |
| `steered_status` | string (default: `has steered a message into the running turn`) | Thread status for a message added to a running turn. Blank uses the default. |
| `acknowledgement_emoji` | emoji name (default: `eyes`) | Mark an accepted request. |
| `rejected_emoji` | emoji name (default: `no_entry_sign`) | Mark a refused request. |
| `queued_emoji` | emoji name (default: `hourglass_flowing_sand`) | Mark a waiting request until it starts. |
| `completed_emoji` | emoji name (default: `disabled`) | Mark a finished turn. The connector still removes its acknowledgement when this is disabled. |
| `failed_emoji` | emoji name (default: `x`) | Mark a turn that started and failed. |
| `stopped_emoji` | emoji name (default: `black_square_for_stop`) | Mark a stopped or superseded turn. |
| `enable_streaming` | boolean (default: `false`) | Preview the reply during the turn. Interactive questions and approvals work in either mode. |
| `activity_card` | boolean (default: `true`) | Show an in-progress card with tool, todo, and subagent activity, and a Stop button. A turn owed a reply gets the card with the Stop button alone until it reports activity, and that card is deleted when the turn ends. A turn whose reply is optional gets its card with its first activity. |
| `activity_card_delay_seconds` | number of seconds, 0 or more (default: `5`) | Delay before posting an activity card. |
| `activity_card_min_edit_seconds` | number of seconds, 0 or more (default: `10`) | Minimum interval between ordinary card updates. A change inside the interval shows when the interval ends. |
| `decision_questions_file` | path string (default: empty) | Optional JSON question overrides relative to the configuration directory. |
| `decision_forward` | boolean (default: `true`) | Send successful typed-decision answers to the agent as advisory text. Without a decision model, no decision call is made. |
| `sdk_log_level` | `CRITICAL`, `ERROR`, `WARNING`, `INFO`, or `DEBUG` (default: `WARNING`) | Log level for `slack_bolt` and `slack_sdk`, subject to the shared logging handlers. |

Emoji values are names without colons. `disabled` suppresses one reaction. Blank uses its default. `acknowledge_mode: off` suppresses all acknowledgement reactions and statuses.

#### Rendering and other features

| Key | Type (default) | Effect |
| --- | --- | --- |
| `blockkit_tables` | `auto`, `marker`, or `off` (default: `auto`) | Allow Markdown tables and supported fenced content to render as blocks. See [Reply formatting](#reply-formatting). |
| `render_tables` | `data_table`, `basic`, or `off` (default: `data_table`) | Select the Slack table block after block rendering is allowed. |
| `brain_react_pending` | emoji name (default: `open_book`) | Mark a second-brain document while ingestion runs. |
| `brain_react_success` | emoji name (default: `brain`) | Mark a document present in the second-brain corpus. |
| `brain_react_declined` | emoji name (default: `x`) | Mark a document declined without an error. |
| `brain_react_failure` | emoji name (default: `warning`) | Mark failed document ingestion. |
| `group_digital_avatar` | boolean (default: `false`) | Route channel traffic through the group-avatar relevance and reply-location pipeline. |
| `my_user_id` | user ID string (default: empty) | Person represented by the group avatar. Required for that feature. |
| `principal_name` | string (default: empty) | Name for that person. Empty resolves it through Slack. |
| `bot_name` | string (default: empty) | Name used to recognize plain-text bot mentions. |
| `enable_memory` | boolean (default: `false`) | Enable connector memory support. |
| `send_file_allowed` (host) | boolean (default: `false` for Slack) | Offer JiuwenSwarm's `send_file_to_user` tool to Slack turns. |

Second-brain ingestion selects conversations through Belle Agent's `second_brain.ingest.channels`. Group avatars need `my_user_id`. File uploads need `files:write`.

## Use the bot

- In a channel, send `@bot your message`. Replies appear in its thread by default.
- `/belle` replies privately in a DM or channel. Slack does not allow slash commands inside threads. Set both Belle's `platforms.slack.command_name` and the manifest's `command` line to rename it.
  - `status` and `list` commands show information. Partial action commands open a form or picker with Cancel. Complete valid commands run directly.
  - `/belle help` (or just `/belle`) lists the commands.
  - `/belle status` shows agent health, model, active turn, waiting-message count, mid-turn handling, and `/belle ask` destination. Its duration covers only the health check. It omits API bases, paths, configuration location, and MCP servers.
  - `/belle skills list` shows installed, enabled skills allowed by this conversation's `skills.available` and `skills.unavailable` scope.
  - `/belle subagents list` shows mounted subagents allowed by this conversation's `agent.subagents` scope.
  - `/belle subagents use <name> <task>` delegates one private task. Omit the task to open a form, prefilled with an available name. The subagent receives only the task, without channel or DM history. It must be available both here and in your DM. Failed delegation reports an error. The command affects one turn.
  - `/belle skills use <skill> <task>` requires the skill's `SKILL.md` for one private turn. Omit the task to open a form, prefilled with an available skill. The skill must be available here and in your DM. Loading failure stops the turn. Later replies do not retain the skill.
  - `/belle experts list` shows installed experts, the scope-selected expert, and unavailable connectors. It does not change the selection.
  - `/belle models list` shows configured models and aliases. `/belle models current` shows the model for new requests and whether a scope, temporary choice, or default selected it.
  - `/belle models use <name>` sets a model for new requests from everyone in this conversation, across threads. Omit the name to open a picker. A matching `agent.model_name` scope takes priority. Running and queued requests keep their models. The choice may revert after restart.
  - `/belle models reset` removes this conversation's temporary selection.
  - `/belle queue list` privately shows waiting messages with links and sender names, or IDs when names are unavailable.
  - `/belle queue drop [message link]` removes a waiting message here. Without a link, choose from a picker with preview, sender, and UTC time. Dropping another person's message requires `clicks.queue_drop`. It does not stop a running turn.
  - `/belle stop [message link]` stops a running turn here. Without a link, choose from a picker with preview, sender, and UTC time. Stopping another person's turn is governed by `clicks.stop`.
  - `/belle ask <question>` answers privately in your bot DM. Omit the question to open a form.
    - From a channel, it copies that channel's session into a persistent side conversation without changing the source. For `session: channel`, it copies the whole channel. Otherwise, it copies the most recent thread. The DM quotes and links the question. Replies in its thread continue the side conversation. Routes last until restart, though copied history remains. The first question from a thread-mode channel starts without context. Replies in older DM threads use your ordinary DM session.
    - From a bot DM, it uses the existing DM conversation.
  - `/belle schedule list`, `show`, `pause`, `resume`, `delete`, and `run` manage Belle scheduled jobs when the Belle scheduler is active. See [Scheduled jobs](../scheduling.md).

  Commands need a tier 1, 2 or 3 manifest and an app reinstall after a manifest update. After restart, tier 1 may need a visible channel message before commands beyond `help` work. Tiers 2 and 3 can query the channel type.

### Channel reading modes

`group_chat_mode` sets the channel trigger. Modes are cumulative:

| Mode | The bot answers |
|:-----|:----------------|
| `mention` | `@bot` mentions (default) |
| `reply` | the above, plus thread replies posted under one of the bot's own messages |
| `all` | the above, plus every other message in allowed channels |
| `off` | nothing in channels, not even mentions |

Unlike Telegram's modes, Slack's `reply` and `all` retain mentions through `app_mention`. Only `off` silences them.

`all` respects `allowed_channel_ids`. `reply` needs the bot's ID from `auth.test` at startup. If that call fails, it answers nothing. DMs need no mention.

Ask the bot for a summary of the current channel and its thread replies. For example:

  ```text
  @Belle Summarize the decisions, technical findings, blockers, action items, and useful links from this channel over the past 7 days. Include all thread replies and cite the source messages.
  ```

For all history accessible under the configured policy:

  ```text
  @Belle Review all accessible history in this channel, including all thread replies. Organize the high-signal information and distinguish facts, inferences, and recommendations.
  ```

The history tool enforces target access from the request. "All" is bounded by Slack retention, bot access, and configured limits. It reports partial coverage. Private channels require bot membership. Tier 2 grants history and directory scopes.

The `platforms.slack.history_*` settings control read budgets without additional Belle ceilings. `read_pinned_messages` uses `history_max_messages` too; if a pin list exceeds it, coverage is partial and names `max_messages_limit`.

`find_by_name` scans at most `directory_scan_max_pages` pages per kind. Its coverage reports incomplete scans; increase the setting if a large directory hides later matches. Set `max_matches` to return more than the default 20 matches from the scan.
The tool preserves complete names and descriptions when it projects matches; it does not cut them at a fixed character count.

`search_slack_workspace` accepts bot, deleted-user and archived-channel selectors, keyword-only search, and Slack term clauses and modifiers. It returns references without message text or blocks. The action token scopes the search to its requester, but Belle cannot establish that every reader of the originating conversation may see the matched content; use an authorized conversation read for details.
Search display fields default to 250 characters. Set `max_field_chars` to return more of those fields; IDs, timestamps, and permalinks stay whole. Coverage counts truncated fields. Slack limits a search page to 20 results, and `coverage.next_cursor` continues it.

`read_slack_canvas` uses Slack's `canvases.getContent` API. It returns the whole canvas as markdown by default, or HTML when `content_format: html` is given. It checks the source conversation's audience before reading. Content above `canvas_read_max_chars` is refused without a partial document.

`group_chat_mode` is the connector-wide default. Use a `scopes:` rule in Belle config for per-conversation triggers or prompts. See [Slack scope rules](#slack-scope-rules).

By default, accepted requests get `acknowledgement_emoji`. `text` posts `acknowledgement_text`, `both` does both, and `off` disables acknowledgement. `allow_from` refusals get `rejected_emoji`. Excluded channels get no response. Reactions need tier 1 `reactions:write`. If unavailable, processing continues and the connector logs the failure.

With `enable_streaming: true`, Slack previews the reply about once per second. Choice questions use buttons in either mode. Free-form `ask_user` questions use text inputs in either mode. Submitting an answer resumes the turn. Invalid calendar dates, clock times, and Unix timestamps return a field error and leave the question open. Socket Mode needs no public Request URL.

Slack-created cron jobs default to `targets: slack`. Results return to the source channel or thread. Jobs without Slack context use `default_channel_id`. `post_as_root: true` posts at channel level.

Input declarations for selects, checkboxes, and radio buttons accept at most
100 options. Each option must be an object with a non-empty string in `label`
or `value`. Optional `label`, `value`, and `description` fields must be strings.
Belle refuses malformed options and lists over the limit before posting the form.

### Decision questions

See [Decision questions](../config.md#decision-questions) for `decision_forward` and `decision_questions_file`.

### Reply formatting

Place `<!-- belle:slack-thread-details -->` between a brief and detailed evidence. The brief becomes a top-level message. Each later section becomes a reply in its thread. Multiple markers create multiple replies. Empty sections are skipped. Without the marker, paragraph-aware splitting handles long messages.

Slack mrkdwn does not support Markdown tables. `blockkit_tables` controls Block Kit rendering:

| Value | Behavior |
| --- | --- |
| `auto` | Render tables and supported fences. A reply can opt out with `<!-- belle:slack-blocks:off -->`. |
| `marker` | Render blocks only when the reply contains `<!-- belle:slack-blocks -->`. The connector does not advertise this marker to the model. |
| `off` | Render no blocks. Strip both markers from replies. |

`render_tables: data_table` gives tables pagination, sorting, filtering, and download controls. It supports up to 200 rows and 20,000 characters. `basic` shows up to 100 rows and 10,000 characters without those controls. `off` leaves Markdown tables as text but still permits charts and model-written Block Kit. A table that exceeds Slack's block limits remains text. Streamed previews remain text until the final reply.

Supported fences are:

| Fence | Result |
| --- | --- |
| `mermaid` | A diagram. `pie` becomes a pie chart. `xychart-beta` becomes a bar or line chart. |
| `vega-lite` | A bar, area, line, or pie chart when the spec maps exactly to Slack. |
| `blockkit` | The Block Kit blocks described by the fence's JSON. |
| `slack-raw` or no language | Source text without rendering. |

Unsupported chart syntax remains visible as source text. This includes unsupported Vega-Lite marks, external `data.url`, layers, facets, and aggregates. Supported fences render without a marker when block rendering is enabled. Model-written Block Kit fences may use any block type, but cannot contain interactive controls. JiuwenSwarm's own approval buttons remain available. The connector checks fences, built-in controls, and large payloads with Slack's `blocks.validate`. If Slack rejects validation, the connector sends text.

Channel threads use separate sessions by default. The connector suppresses token-level `chat.delta` events to limit channel noise and Slack API calls. Find Slack user and channel IDs through the profile or channel details menu's **Copy ID** action.

## Slack scope rules

The Belle config file holds `scopes:` rules that match Slack requests by workspace, conversation, kind, and sender. [Conversation scopes](../scopes.md) explains matching and composition. The settings below describe Slack behavior.

A scope that names an exact Slack conversation and changes `delivery` or `agent.model_name` opts it in even when `channels.slack.allowed_channel_ids` omits it. A rule that sets only `permissions`, `clicks`, `agent.expert`, `agent.skills`, or `agent.subagents` does not opt the conversation in. Keep the channel in `allowed_channel_ids` or add an opt-in rule if it must receive messages.

Slack uses `channel` for a public channel, `group` for a private channel, `im` for a one-to-one DM, and `mpim` for a group DM. Use `chat_type: [channel, group]` for both channel privacy kinds. A Slack Connect conversation can receive requests from more than one workspace. Add `workspace: "T..."` when a rule must apply only to one installation.

### Delivery settings

| Setting | Values and effect |
| --- | --- |
| `mode` | List of triggers: `mention`, `reply`, `all`, `url`, `has_file`. Any listed trigger can start a turn. `all` includes mentions. An empty list answers nothing. |
| `prompt`, `prompt_append` | Standing text added after the user's message. The connector labels the trigger, for example `[trigger: url]`, when it adds a prompt. |
| `mid_turn` | `queue` waits and starts a separate turn. `cancel` stops the running turn. `steer` joins its next model call. On a backend that cannot steer, `steer` waits as `queue` does. Default: `queue`. |
| `session` | `thread` gives each thread its own session. `channel` shares one session across the room. Default: `thread`. DMs already use one session per person. |
| `reply` | `required` delivers a reply. `optional` lets the turn stay silent, in the form `platforms.slack.optional_reply_mode` sets. Default: `required`. DMs always require a reply. |
| `events` | Mapping from Slack event families to `off`, `context`, or `turn`. See [Event rules](#event-rules). |

`mention` is not implicit in `mode`. For example, `mode: [url]` ignores a plain mention without a link. A URL trigger receives the link in the message without opening the page. Ask the agent to open it in `prompt` or `prompt_append` if analysis needs the page contents.

`session: channel` shares history, one running turn, and one bounded queue across all threads in the channel. Replies still appear in the originating thread. Changing `session` starts a new session rather than merging old thread histories. JiuwenSwarm queues a message that requests `mid_turn: steer` in a channel-wide session. `mid_turn: cancel` lets anyone who can post stop another person's running turn.

Under `optional_reply_mode: post_tool`, the connector withholds replies in rooms with `reply: optional`. The agent must use `post_message` to answer. JiuwenSwarm reports three consequences at startup.

- A `permissions` rule that denies `post_message` leaves the room unable to answer.
- With `enable_streaming: true`, Slack shows a preview of a withheld reply. The connector clears the preview afterwards.
- `post_message` refuses calls without a destination. Each turn must name a thread or the room top level. `reply_in_thread` no longer chooses that destination. Use `sentinel` when `reply_in_thread` must choose it.

The destination is `chat_id` for a conversation, or `reply_to_message_id` for a thread inside the originating one. Either argument alone satisfies the requirement, and a thread reply needs no `chat_id`. JiuwenSwarm states the conversation and the settled thread in the turn's envelope, so both values are within a turn's reach. The refusal also reports the originating conversation's ID.

`post_message` returns `chat_id`, the first `message_id`, `thread_id`, and `parts`. Split posts also return every `part_message_ids` value. If a later part fails, the error lists the parts already sent; retrying the whole post can duplicate them.

With Block Kit blocks, `post_message` uses `text` as the fallback and preserves
its whitespace. `as_requester` prepends a disclosure. Belle refuses a combined
fallback above 40,000 characters before posting. An edit with blocks refuses
fallback text above 4,000 characters before updating. Omit blocks to split a
long post.

`post_message` and `edit_message` also accept Slack-native markdown, attachments,
metadata, and parsing options. `markdown_text` is exclusive with `text` and
`blocks` and has Slack's 12,000-character limit. Metadata is visible to members
of the workspace. `as_requester` uses only the current requester's identity.
Belle discloses its authorship on every posted part. `as_requester` cannot be
combined with `markdown_text`.

`publish_slack_home_tab` and `clear_slack_home_tab` accept an optional `hash`
from the current view. Slack returns `hash_conflict` if the view changes
before the update.

`send_slack_file` accepts one file or a `file_uploads` list. Each file can set
its title, image alternative text and snippet type. Belle checks a copied
Slack file against the source conversation before uploading any of the list.

Search `limit` accepts integers from 1 to 20.

`session` and `reply` must be set for a room or a kind of room, not for a sender. The same restriction applies to `events`. JiuwenSwarm reports invalid combinations at startup and ignores the affected key.

#### Event rules

The `events` mapping accepts `reaction`, `pin`, `member`, and `app_home`. Each family includes both directions where Slack provides them. `off` drops events. `context` keeps them for the next turn.

An event that an app did, this one included, starts no turn and is not kept as context. A reaction, pin, or member payload has no bot flag, so Belle asks `users.info` once per user whether the actor is a bot. With `workspaces`, the bot users of the other entries count as bots without asking, so their acknowledgement reactions in a channel the workspaces share start nothing. When Belle cannot tell, it drops the event: the outcome is `dropped:actor-unknown:` followed by Slack's error code, or `missing_scope` without `users:read`, which is logged once. Every manifest that subscribes to these events grants `users:read`. Slack's own users count as bots without asking. Another event in a DM needs no lookup, because only the person and this app are in it.

With `reply: optional`, `turn` gives each member or pin event its own turn. Busy events wait in memory until the session is free. Messages run before waiting events. A message arriving during an event turn waits, regardless of `mid_turn`. An unanswered question pauses the event queue. Busy reactions remain buffered context. `reply: required` downgrades event turns to context and logs a warning for member and pin rules. DMs require replies, so their events stay in context.

```yaml
scopes:
  - match: {channel: slack, chat: "C000000AAAA"}
    delivery:
      reply: optional
      events: {reaction: context, pin: context, member: "off"}
```

`reaction` and `pin` turns answer in the affected message's thread. `member` turns answer at the channel top level. The connector drops and logs a pin without a message anchor. `app_home` occurs only in the bot's DM with the user and accepts `off` or `context`. A rule naming a public or private channel never matches App Home. Use `match: {channel: slack}` to cover it across DMs. Slack event payloads lack a usable conversation kind for `chat_type` matching. Use `channel: slack` or an exact `chat` for event rules.

The connector holds at most 256 member and pin turns per session and 4096 overall, including events awaiting deduplication. They expire after 24 hours. `/belle status` reports pending and lost counts. Rejected, expired, or stopped events never enter another actor's context. The connector logs each loss with its event ID, actor, channel, and reason. The queue and loss count reset on restart. The service log records accepted pending events for manual recovery after a crash. Context buffers are also bounded and reset on restart.

### Agent settings

| Setting | Effect |
| --- | --- |
| `model_name` | Selects a configured `models.defaults` name or alias for matching turns. On JiuwenSwarm, Belle does not start with an unknown name. |
| `expert` | Selects one installed agent-template package for the main agent. Use `""` in a narrower rule to clear an inherited expert. |
| `subagents` | List of globally enabled subagent names callable in the matched turn. `[]` allows none. This does not enable a disabled subagent. |
| `skills.available` | Skills the agent may load. Omit it to inherit the global library. `[]` allows none. |
| `skills.unavailable` | Skills the agent may not load, though `skills.available` or the global library offers them. Omit it to inherit a broader list, or use `[]` to clear that list. |
| `skills.required` | Skills whose full instructions load on every matched turn. Omit it to inherit a broader required list, or use `[]` to clear that list. |

Scopes cannot set a reasoning level or `work_mode` per conversation. To change reasoning, configure a model in `models.defaults` and select it with `agent.model_name`. A session fixes `work_mode` on its first turn.

`expert` replaces the inherited name in a more specific rule. The package must be installed. Its declared connectors must be connected. If either check fails, the turn reports an equipment error. Slack sends the resolved expert on every turn. This clears a previous selection when the scope changes.

An expert can add skills, subagents, tools, and connectors. After the package loads, `skills.available` and `skills.unavailable` filter Skill loading and `subagents` restricts delegation. Neither setting limits package tools. Review those under `permissions.tools`.

`subagents` and each `skills` subkey replace their inherited lists in a more specific rule. These lists use plain names. Signed `+` and `-` entries are not accepted. A required skill must also be available, and `skills.unavailable` must not name it. Scope settings do not install skills, experts, or subagents.

```yaml
scopes:
  - match: {channel: slack, chat: "C000000AAAA"}
    agent:
      expert: research-expert
      subagents: [research_agent]
      skills:
        available: [research, ambient]
        required: [ambient]
```

### The audience rule

Belle Agent's Slack policy holds every Slack read and every Slack post to one audience rule. It has no per-scope form.

Set the policy in the Belle config file:

```yaml
platforms:
  slack:
    enforce_audience: true
```

`enforce_audience` controls the audience rule for reads and posts.

Set `enforce_audience` in Belle's config file.

Each conversation has an audience. A public channel reaches the whole workspace, because anybody in the workspace may join it and read the scrollback. Every other conversation reaches the people in it: a private channel, a group DM and a one-to-one DM are all the same case.

- Reading T into S is permitted when the audience of S is inside the audience of T.
- Posting S into T is permitted when the audience of T is inside the audience of S.

Reads bring T into S. Posts send S into T. The audience subset therefore runs in opposite directions.

Reads:

| | T public | T private | T group DM | T DM |
| --- | --- | --- | --- | --- |
| S public | allowed | refused | refused | refused |
| S private, DM, group DM | allowed | subset test | subset test | subset test |

Writes:

| | T public | T private | T group DM | T DM |
| --- | --- | --- | --- | --- |
| S public | writes | writes | writes | writes |
| S private, DM, group DM | asks | subset test | subset test | subset test |

A failed read states its reason. It lists blocking members for a public channel because Slack exposes its membership. Other targets get only a count. Reads never request confirmation.

A failed write is put to the person who asked, naming who would newly see the message, and goes ahead only if they agree. A scheduled run has nobody to ask and refuses. Deleting never asks, because it removes a message and widens nothing.

Slack Connect conversations use member lists even when Slack calls them public. The connector checks `is_ext_shared`, `is_pending_ext_shared`, `is_org_shared`, and `is_shared` in the conversation record without an extra API call.

Whether the Slack history and posting tools are offered at all is `permissions.tools`. The bot still needs Slack API access and channel membership for every read and every post.

### Tool and button limits

`permissions.tools` can narrow a tool to `ask` or `deny`. It cannot grant `allow` over a broader restriction. A `deny` always holds. A matching parameter rule with `action: allow` can still bypass an `ask`. Use `deny` for a firm block. On unattended channels such as cron or heartbeat, `ask` becomes `deny`. See `docs/en/ToolPermissionsSecurity.md` in the JiuwenSwarm checkout for the base policy.

On JiuwenSwarm, Belle refuses a tool call whose conversation is unknown while any scope sets `permissions.tools`. Today that is every tool call. See [Tool permissions of a scope](../integrations/jiuwenswarm.md#tool-permissions-of-a-scope).

Consider `ask` for `delete_slack_canvas` and `delete_slack_list`: Slack has no undelete for either. `write_slack_list` can also merit `ask` because List columns cannot change after creation. `share_slack_canvas` and `share_slack_list` ask the requester before changing access, including ownership transfers and revocations. An approval expires with its turn. Tool permissions cannot restrict one argument value, so `deny` on a share tool also affects ordinary sharing.

`schedule_slack_message`, `list_slack_scheduled_messages`, and
`cancel_slack_scheduled_message` manage Slack's own pending-message queue.
The list uses a cursor and shows messages scheduled by this app token, not
messages scheduled in Slack's UI. Scheduling accepts a future Unix timestamp
up to 120 days ahead. It has no `metadata` option because Slack currently
fails to deliver scheduled messages that include metadata. Cancellation asks
the requester during the turn and fails during the last 60 seconds before
delivery. These tools do not create or change Belle jobs.

`clicks.approve` limits who may answer a tool approval. `clicks.stop` limits who may stop a turn, but the turn initiator may always stop it. `clicks.queue_drop` lets someone remove another sender's queued Slack message with `/belle queue drop`. The sender can always remove their own. Each accepts `user` or `role`, and both forms combine with OR. A `role` needs `channel` in the match, because roles resolve to IDs per platform. Slack's `allow_from` still applies. Without a `queue_drop` rule, only the sender can remove a queued message.

```yaml
scopes:
  - match: {channel: slack, chat: "C000000AAAA"}
    permissions:
      tools: {bash: ask}
    clicks:
      approve: {user: ["U000000AAAA"]}
      stop: {user: ["U000000AAAA"]}
      queue_drop: {user: ["U000000AAAA"]}
```

Set `permissions` and `clicks` on `channel` or exact `chat` matches. They cannot use `workspace`, `chat_type`, or sender filters today. The permission hook and button handler do not have those match axes. Belle does not start with a rule that sets either section and names a workspace, a `chat_type` or senders. A click rule can only narrow access, and it cannot override Slack's `allow_from`.

## Troubleshooting

**The bot does not connect**

At start, Belle checks the bot token with `auth.test` and the app token with `apps.connections.open`. When Slack refuses either one, the error names the setting that holds the token and Slack's error code. With one workspace, Belle does not start. With `workspaces`, that entry is logged as NOT SERVED and the other entries are served.

Slack may not answer: a network failure, a Slack server error, a rate limit, or a socket that does not open within 30 s. With one workspace, Belle does not start, and the error says what did not answer. With `workspaces`, that entry is logged as NOT CONNECTED and connected again in the background, after 1 s and then twice as long each time, up to 30 s, until it serves or Slack refuses it. Until then, Belle refuses every action in that workspace: nothing is posted there. When no workspace serves at the start, the start stops.

- Check that Socket Mode is enabled.
- Check that `app_token` is an `xapp-...` token with `connections:write`.
- Check that `bot_token` is the `xoxb-...` token generated after installing the app.

**Slack's stop control says "Stop not available for this agent"**

- The app has no agent surface: Slack offers its own stop only to an app with `features.agent_view` and `assistant:write`, which tier 3 adds. Use the activity card's Stop button or `/belle stop`, or install tier 3.

**Channel mentions get no reply**

- Check that the app subscribes to `app_mention`.
- Check that the bot is a member of the channel.
- If `allowed_channel_ids` is configured, include the current channel ID.
- If `allow_from` is configured, include the current user ID.

**Direct messages get no reply**

- Check that the app was created from a shipped manifest: every tier grants `im:history` and subscribes to `message.im`.
- Reinstall the app to the workspace after changing scopes or event subscriptions.

**Links in an automatic channel do not trigger the bot**

- Check the per-conversation trigger and that the bot is a member of the channel.
- Check that the app was created from a shipped manifest: every tier grants `channels:history` and subscribes to `message.channels`.
- Check that the message is from a human, contains an HTTP/HTTPS link, and the sender passes `allow_from`.
- Reinstall the app after changing scopes or event subscriptions.

**A channel digest is empty, shows user IDs, or reports partial coverage**

- Check `permissions.tools` does not deny `read_slack_conversation`.
- Check that the bot is a member of the channel and the installed app has the needed history and membership scopes. The tier 2 manifest includes them.
- Raw `U…` IDs can mean `users:read` is absent. Reinstall after granting it.
- Reaction, pin, and member events outside DMs that end as `dropped:actor-unknown:missing_scope` also mean `users:read` is absent.
- Check the tool's coverage note. Slack retention, API failures, and the configured scan limits can stop a full scan.

**A Slack cron job does not deliver its result**

- Check that the job's `targets` value is `slack`.
- Check that the bot belongs to the Slack channel captured in the job's
  `session_id`. `default_channel_id` is only the fallback when no request
  context is available.
- Check that the bot is a member of the destination channel and has `chat:write`.
- To keep recurring reports out of the source thread, update the job with
  `post_as_root: true`.

---
