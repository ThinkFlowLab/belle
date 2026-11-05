# Discord platform (experimental)

Belle runs on Discord as one or more bot applications, over Discord's REST API (v10) and its gateway.
Each configured bot serves every guild it is in and its direct messages.

## Set up the application

1. In the [developer portal](https://discord.com/developers/applications), create an application. On
   its **Bot** tab, reset the token and set it in the environment variable for that bot. For one bot,
   use `DISCORD_BOT_TOKEN`. For named bots, use the variable named by that bot's `token_env` field.
   Token environment values must have no surrounding whitespace.
2. On the same tab, under **Privileged Gateway Intents**, turn on **Message Content**. Without it, a
   message in a guild channel reaches Belle with no text unless it mentions the bot, so ambient channels
   cannot work. Turn on **Server Members** only where `guild_members` is true (see below). An intent that
   the connection asks for and the portal does not grant stops the start, and the error names the toggle.
3. On **General Information**, leave **Interactions Endpoint URL** empty. Buttons, selects, forms and the
   slash command then reach Belle over the gateway, with no public address.
4. Generate an invite from the capabilities in your Belle config. Run this where Belle and its Discord
   optional dependency are installed:

   ```text
   belle-discord-install --application-id <application id> --config /path/to/belle.yaml
   ```

   The helper prints a URL with the `bot` and `applications.commands` scopes and the permissions used by
   the registered Discord tools and enabled Discord features. Ask a server administrator to open it and
   approve the install. The helper cannot create the application or approve an install for a server.

OAuth permissions let the bot perform actions in a server. Privileged intents are separate Developer Portal
settings. The helper lists the Message Content toggle when `message_content` is true. It always lists Server
Members for REST audience checks; `guild_members` controls whether Belle also requests member events over
the Gateway. Discord applies the Server Members intent to the List Guild Members HTTP endpoint independently
of the Gateway Identify request. Reading conversations requires **View Channel** and **Read Message History**.
Discord also requires **Connect** to read messages in voice channels, so the invite helper requests it for
`read_discord_conversation`. Reactions, posts, file captions, and edits that use another server's custom
emoji need **Use External Emojis**; posts with external stickers need **Use External Stickers**. The helper
requests those grants for the relevant tools. Channel permission overwrites can still deny a bot permission.
Members also need **Use Application Commands** in the server or channel to invoke `/belle`.
If you enable a feature that adds a permission, regenerate the URL and ask an administrator to approve the
updated invite. A code deploy cannot grant that permission to an installed bot.

To verify a token, set its configured environment variable and add `--verify-token`. For a named bot,
also pass `--bot <name>`. The helper asks Discord for the bot identity and prints only its username and ID.
Invite generation without this flag makes no network request and does not claim that a token is valid.

The section `platforms.discord` turns the platform on when its configured token variables resolve.
Without `bots`, it reads one token from `DISCORD_BOT_TOKEN`. With `bots`, each entry's `token_env`
names the environment variable for that bot's token.
Belle registers `/belle` globally.
The [example config](../../examples/config/belle-config.yaml) adds Discord scopes for
queued turns, direct replies in DMs, and optional replies in server conversations.
It also hides Slack-only skills on Discord.

## Messages

- **Where a message is.** The workspace is the guild, or `direct` for a direct message. A thread is a
  channel of its own on Discord. A public thread uses its parent channel as its conversation. A private
  thread uses its own ID as its conversation. Its sessions, scopes, commands and event context stay separate
  from the parent, including with `delivery.session: channel`. Both use the thread ID as their thread.
  The chat types are `text`, `announcement`, `forum`, `voice`, `stage`, `thread`,
  `private_thread` and `dm`. A group DM is not served: a bot joins one only through a person's OAuth
  grant, which Belle does not use.
- **Triggers.** A direct message, a mention of the bot or a role it holds, a message in a thread the bot
  started, and a reply to one of its messages. `@everyone` is not a mention of the bot. Scopes decide
  which triggers start a turn.
- **Answers.** A channel message is answered in a thread started on it, named after its first 100
  characters; `reply_in_thread: false` answers it in the channel. A thread started from a message has
  that message's id, so the thread of an answer is the message's id. A message in a thread or a direct
  message is answered where it is.
- **Apps.** A message of another bot or of a webhook is handed to the host as an app's message, and the
  host refuses it by default. The bot's own messages are not handed over.
- **Rendering.** The model writes Markdown, and Discord shows a subset of it. A table becomes a fenced
  block with aligned columns, a heading of level 4 to 6 a bold line, `__strong__` (underline on Discord)
  `**strong**`, and an image a link. A long answer goes in messages of at most 2000 characters, cut
  between blocks; a code block cut in two is closed and opened again.
- **Mentions.** By default a post can notify named users. The `post_message` tool may also notify roles or everyone when Belle policy and Discord permissions allow it.
- **Marks and typing.** Each mark accepts a nonempty bare Unicode value (no spaces, at most 64 characters), `name:id` for a custom emoji, or `disabled`. Discord checks whether a bare value is an emoji when reacting.
  The bot shows as typing where its answer goes while a turn runs.
- **Files.** The files of a message come from Discord's CDN, at most `file_download_max_bytes` each (30 MiB by default), without the bot's
  token. A file that is not read is named in a post where the answer goes. Files in one outgoing
  `post_message` request total at most 25 MiB.
- **Events.** Reactions are events. Member events need `guild_members` and the Server Members intent. A
  pin is no event: Discord does not say who pinned what.
- **Private messages.** Discord has a message for one person only as an answer to that person's
  interaction, so `post` refuses one, and the host sends none (a mark is the only sign).

## Guild resources

`list_discord_scheduled_events`, `get_discord_scheduled_event` and
`list_discord_event_users` read scheduled events and their subscriber pages.
Events hosted in a channel are returned only when the bot and requester can
view that channel and the current conversation does not have a wider audience.
An event outside that audience is omitted from a list and refused by the detail
tool. The list reports how many events were omitted.

`list_discord_soundboard_sounds` and `get_discord_soundboard_sound` read sounds
in a guild where the bot and requester are members. `list_discord_default_sounds`
reads Discord's public default sounds. These tools cannot create events or
sounds, change guild settings, or play sounds in voice channels.

## Questions and the Stop button

- **Questions and approvals.** A question with at most five options shows one button each; an approval
  shows **Approve** and **Reject**. A question with six to 25 options shows one select. A question with no
  option shows an **Answer** button, which opens a form with one text field (at most 4000 characters).
  Discord limits a button's label to 80 characters and a select option's to 100: a question whose options
  do not fit, or that has more than 25, is not shown. A harness that can be told, such as OpenCode, hands
  the model the reason with the limit, and the turn goes on, so the model can ask again. On another
  harness the turn ends, and its failure names the limit.
- **Clicks.** Each click is acknowledged at once, as Discord asks within 3 seconds, and then handed to the
  host, which decides by the `clicks` rules who may answer. An answered question loses its components and
  says who chose what. A person who may not answer, or whose click comes too late, is told in a message
  only they see; the question stays open for somebody who may.
- **Stop.** A click on a Stop button asks the host to stop that message's turn; the person is told whether
  it stops.
- A component of an earlier start of Belle answers nothing: its components come off.

The shared backend question contract supports three answer shapes: an approval verdict, one value from a flat
option list, or one free-text value. Discord renders those as approve/reject buttons, buttons or a single-choice
select, and a one-field text form. Backends do not request multiple selections, Discord entity values, multiple
named fields, or uploaded answers, so Belle does not expose controls for them.

A slow callback response does not block another interaction's callback. Callbacks, response edits and
followups honor reported route limits and 429 waits. Webhook limits are tracked per webhook ID and token.
Internal rate-limit keys use token digests. Diagnostics redact tokens. Discord exempts these interaction
endpoints from the
[bot's global rate limit](https://docs.discord.com/developers/topics/rate-limits#global-rate-limit).

## The `/belle` command

At each start, Belle registers `/belle` globally or replaces the global command of that name;
the application's other commands are left alone. It works in every server and the bot's direct messages.
Discord may take up to an hour to show a new or changed command. A refusal, such as a bot invited without `applications.commands`, is
logged at start and Belle runs on without the command. Registration runs in the background after
READY and bot identity validation, so messages and Gateway acknowledgements continue during registration.
Shutdown cancels pending registration. An unexpected registration failure reports terminal bot health.

Invoke `/belle` from Discord's application-command menu, then enter the shared command grammar in its one text
option. Typing `/belle sessions result <id>` as an ordinary DM message does not invoke the command. `/belle help`
(or bare `/belle`) lists the supported workflows and marks commands that are unavailable in the current conversation.
The answer is shown only to the person who typed the command. `ask`, `skills use` and `subagents use` post the
question in the bot's direct message with that person and answer there.

Belle keeps the free-text option because it preserves the shared command grammar, including open-ended prompts
and scope-dependent availability. Native subcommands and typed options would add a second schema and a mapping
back into that grammar without a current workflow that needs autocomplete. Discord also limits a chat-input
command to 25 options, so the current workflows would need a grouped schema. Message and user context commands
have no action: commands run against the invoking conversation, and `/belle stop` accepts a message
ID or link. See Discord's
[application command option limits](https://docs.discord.com/developers/interactions/application-commands#application-command-object).

## A running turn

- **A reply as it is written** (`enable_streaming`). The reply shows in a message Belle edits in place,
  about once a second, in the thread of the answer. A reply longer than a message goes on in a new message.
  It ends as the final answer; a reply that cannot read as the final is deleted and the final posted. A
  stopped reply keeps what it wrote, marked as stopped.
  If Discord may have created a streamed message but Belle cannot confirm its response, Belle stops
  that stream and marks the turn as failed. It does not post the final again, because that could duplicate the reply.
- **The activity card** (`activity_card`). Once a turn has run `activity_card_delay_seconds`, a message with
  an embed lists what it runs, with a **Stop** button. A turn owed a reply that has called no tool yet gets
  the title and the button alone, so it can be stopped too; a turn whose reply is optional gets its card with
  its first tool call. The card is edited at most every `activity_card_min_edit_seconds`, and longer where
  Discord asks to wait. When the turn ends, the card says how it ended and loses its button; the card of a
  turn that said nothing or was stopped, or that held only the button, is deleted.
- Belle shows as typing until the answer starts.

## Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `message_content` | `true` | Ask for the privileged Message Content intent. |
| `guild_members` | `false` | Ask for the privileged Server Members intent: member events. |
| `member_event_channels` | `{}` | Optional map of guild IDs to fallback channel IDs for member events when the system channel is missing or unusable. |
| `reply_in_thread` | `true` | Answer a channel message in a thread started on it. |
| `typing_indicator` | `true` | Show the bot as typing while a turn runs. |
| `enable_streaming` | `false` | Show a reply as it is written, in a message Belle edits about once a second, then ends as the final. |
| `activity_card` | `true` | Show a card of the tools a running turn calls, with a Stop button. A turn owed a reply gets the card with the Stop button alone until it calls a tool. A card of a turn that said nothing or was stopped, or that held only the Stop button, is deleted. |
| `activity_card_delay_seconds`, `activity_card_min_edit_seconds` | `5`, `10` | How long a turn runs before its card is posted, and the least time between two edits of the card. |
| `pending_events_max` | `1024` | Maximum Gateway events waiting per bot, excluding interactions. Use a whole number of at least 1. Overflow stops the connector and reports terminal health failure. |
| `pending_questions_max`, `question_expiry_seconds` | `256`, `86400` | Maximum unanswered questions held in memory and their lifetime in seconds. Belle closes the oldest question at capacity and closes each question at expiry. A stale control cannot answer. |
| `allow_role_mentions`, `allow_everyone_mentions`, `allow_tts` | `false` | Allow privileged post forms when the bot also has the required Discord permission. |
| `allow_from`, `allowed_channel_ids` | empty | As for every platform: who is answered, and where. |
| `decision_questions_file`, `decision_forward` | `""`, `true` | The decision gate, as on Slack and Mattermost. |
| `acknowledgement_emoji`, `rejected_emoji`, `queued_emoji`, `completed_emoji`, `failed_emoji`, `stopped_emoji` | 👀, 🚫, ⏳, `disabled`, ❌, ⏹️ | The reaction of each mark. |
| `brain_react_pending`, `brain_react_success`, `brain_react_declined`, `brain_react_failure` | 📖, 🧠, ❌, ⚠️ | The reactions of a second brain ingest. |

## Tools

`read_discord_conversation` reads one message or a newest-first page (1–100) from a DM, channel or
thread. With no `chat_id`, it uses the active thread, then the source channel. Use only one message
selector. History cursors accept unsigned 64-bit decimal strings, including `0` and synthetic
Snowflakes; exact message IDs still identify actual objects. Coverage supplies directional
`next_before_message_id` and `next_after_message_id` cursors. An `around_message_id` page reports
neither history boundary as exhausted. The message projection includes returned components, polls,
stickers, references, snapshots and reactions, and lists missing and omitted fields. Deleted messages
and older edit revisions cannot be recovered.

`search_discord_guild` accepts structured guild message/file filters, including `has: ["file"]`.
Explicit channel filters require readable targets before searching. With no channel filter, Discord's
guild index includes indexed archived threads and forum posts. Belle checks each returned hit against
the requester, bot and source audience before disclosing content or thread/member context. It omits
hidden results and guild-wide totals. Discord allows up to 25 messages per page, offsets through 9975
and 500 explicit channel filters per request. Coverage reports indexing delays and `next_offset`.
Continue with `filters.offset`; at the offset ceiling, narrow the date-ID or channel filters.
Discord documents these limits and indexing caveats in [Search Guild Messages](https://docs.discord.com/developers/resources/message#search-guild-messages).
A short page or changing result total does not prove completeness. Safe thread and membership
context accompanies accessible hits. The bot's Message Content intent must be enabled for guild
search. This tool does not search DMs or resource names.

`find_by_name` finds visible channels (including forum,
media, voice, category, stage and directory channels), threads and forum posts, forum/media tags,
guild members, roles, emoji and stickers. Omit `kind` to search every supported kind. Each match
includes its resource kind, exact ID, guild, parent and match type; thread matches include archive
state and membership evidence. Channel discovery checks View Channel for the requester and bot.
A category is also visible through a child channel that the person can view.
History reads have separate permission requirements. Returning another channel's metadata also
requires proof that the source audience can see it. That proof may need Discord's full guild member
list (Server Members privileged intent) and private-thread membership access. Missing audience
evidence produces unavailable coverage. Member search matches username and nickname prefixes,
with a 1000-member API ceiling and no remote continuation; narrow the query at that limit.
An exact member ID uses the member lookup. Discord-wide user and DM directories are unavailable.
`limit` defaults to 100 returned matches. Pass `coverage.next_cursor` with the same query, kind and
guild to continue; coverage reports each kind's searched, denied, unavailable or still-paged sources.
Cursors are authenticated and bound to the query, guild, requester, bot and source conversation.
They resume live listings and do not preserve a snapshot. They are valid only in the running Belle
process that issued them. After a restart, omit `cursor` to start the search again.
Archived discovery reuses `list_discord_threads`. Without Manage Threads, it searches bot-joined
private archives and reports that the full private archive is unavailable. Guild directory results
require the source audience to belong to that guild (or the source to be the requester's DM).

`download_discord_attachment` accepts the channel, message and attachment IDs, fetches the parent
message again for a current signed URL, and enforces `platforms.discord.file_download_max_bytes`. An attachment present
in Discord's response can be downloaded with Message Content intent disabled. A missing attachment
is refused; Discord may have withheld it. The tool does not accept arbitrary URLs. These reads check
the requester's and this bot's current access to the target; private threads also require membership
or Manage Threads. History reads from a guild conversation, including same-channel reads, require
complete audience evidence. Every source viewer must have View Channel and Read Message History in the target.
This prevents a new answer from exposing old messages to members denied history access.
Voice message audiences also require Connect for each member. Missing audience evidence refuses
the read. Channel and category discovery still checks visibility separately from history access.

`send_discord_file` accepts `source_attachment` with `chat_id`, `message_id`, and `attachment_id`
to copy a verified attachment. Copying accepts attachments returned by Discord with Message Content
intent disabled. Missing or withheld attachments are refused.

`list_discord_threads` reads one collection per call: active guild threads, public archived,
private archived, or joined private archived threads. Archived pages accept `limit` values from 2
to 100 and return a cursor. Joined private archives use a thread ID; the other archives use a
timestamp. Private archived listings require Manage Threads for both the requester and bot.
Results name the parent, thread type, archive state and requester/bot membership (`joined`,
`not_joined` or `unknown` when Discord supplies no membership evidence). Coverage identifies excluded
threads and states that results include only targets visible to the requester, bot and source audience.
Threads whose audience cannot be verified are counted separately in `coverage.unavailable_threads`;
the response reports partial coverage even when the remote endpoint has no more pages.
`list_discord_thread_members` pages one readable thread and requires
`guild_members: true` plus Discord's Server Members intent. `read_discord_poll` returns the
question, answers and count status; it can page voters for one answer. Open counts are
approximate, and absent results mean unknown counts.

`create_discord_thread` starts a text or announcement thread from a message or with an explicit
public/private kind. Forum and media creation uses `post_message` with `reply_mode: thread`.
`update_discord_thread` changes thread settings. `change_discord_thread_member` joins or leaves as the bot,
or adds or removes a named user. Each action checks the requester's and bot's current access and authority.
Adding or removing another user, changing archive or lock state, and ending a bot-owned poll ask
for approval. These actions must complete in the requesting turn; an answer after it ends runs
nothing. Scope `ask` also prepares the toolkit checks before approval.
One toolkit question approves the call when both checks require approval.
A call whose scope says `ask` is refused if the host cannot obtain its approval.

Edits, deletion, thread changes, and poll closure require an active turn before each REST mutation,
including after rate-limit waits. If the turn ends during an access check, the tool returns `turn_ended`
and sends no mutation. A request already sent can still complete.

`post_message` defaults to the turn's reply destination. `chat_id` selects a text,
announcement, forum, or media channel, an existing thread, or a DM channel. `recipient_user_id` opens a user DM;
these two arguments are mutually exclusive. A reply message or existing thread must belong
to the selected channel.

`reply_mode` chooses placement:

| Mode | Placement |
| --- | --- |
| `auto` (default) | Follow the source conversation's `reply_in_thread` behavior. For another destination, use a native reply if `reply_to_message_id` is set, otherwise post at top level. |
| `top_level` | Post in the selected channel, DM or existing thread; reject `reply_to_message_id`. With no target, use the source conversation's parent channel, including when invoked from a thread. |
| `reply` | Use Discord's native `message_reference` reply to the required `reply_to_message_id`. |
| `thread` | Post in an existing thread, start one from `reply_to_message_id` in a text or announcement channel, or create a forum/media post with `thread.name`. DMs are unsupported. |

`thread` accepts creation options only for a new thread: `name` (1 to 100 characters),
`auto_archive_minutes` (60, 1440, 4320 or 10080), and `rate_limit_per_user`
(null or an integer from 0 to 21600 seconds). These limits come from Discord's
[Start Thread from Message endpoint](https://docs.discord.com/developers/resources/channel#start-thread-from-message).
For forum and media channels, `thread.name` is required and `thread.applied_tag_ids` selects up to five available tags. A channel that requires a tag rejects an empty list. Belle creates the thread with the first message, then sends any remaining split parts into it. The response includes each confirmed message ID and the new thread ID. Existing threads reject creation options. Native replies use the
[Create Message endpoint](https://docs.discord.com/developers/resources/message#create-message),
including in DMs. Each split part keeps the requested placement and native reply reference.

Belle sends a distinct Discord nonce with enforcement for every message part.
`forward_discord_message` creates a native snapshot of a readable source message
in another channel or DM. Its destination follows the same audience approval
rule as `post_message`. `crosspost_discord_message` distributes a message this
bot posted in an announcement channel to that channel's followers. It always
asks the requester in the current turn because Belle cannot verify every follower.
`set_discord_thread_pinned` pins or unpins a forum or media post thread. Both
the requester and bot need Manage Threads on its parent channel. The tool
preserves the thread's other flags.

The tool accepts up to 10 embeds per message. Across those embeds, Belle limits text to 6,000
characters: a title to 256, a description to 4,096, a footer to 2,048, an author name to 256,
and each field name and value to 256 and 1,024. Each embed can have up to 25 fields.

A poll needs at least one and at most 10 answers. Its question text is at most 300 characters,
each answer text is at most 55, and its duration is 1 to 768 hours. Up to three sticker IDs may
be sent in one message. Discord can still reject a sticker the bot cannot use.

The tool also accepts display components, a shared client theme, explicit mentions, notification
and embed suppression, and inline file attachments. Text can be omitted
when another field creates the message. A long text is split: the rich fields and files go on
the first part, while the requested mention and notification settings apply to every part.
File attachments accept `filename` with either `content` or `content_base64`; local paths and
stored file IDs are not accepted here. Interactive components require a registered interaction
handler. Until one is registered for this tool, link and premium buttons and display components
are available, but controls with `custom_id` are refused. Discord Components V2 cannot share a
message with text, embeds, stickers, polls or a shared theme.

`allow_role_mentions`, `allow_everyone_mentions` and `allow_tts` default to `false`. Enabling one
also requires the corresponding Discord permission at the destination. Belle refuses a privileged
send when the operator setting or permission is missing, before posting any part.

For another destination, Belle fetches current guild members, roles and channel permission
overwrites and active member timeouts. Timed-out members retain only existing View Channel and
Read Message History permissions. Owners and administrators are exempt
([Discord timeout permissions](https://docs.discord.com/developers/topics/permissions#permissions-for-timed-out-members)).
Belle refuses malformed timeout timestamps. Private threads also require their member list.
The bot must be able to post, and the requester must be able to read a selected channel. Enable Server Members access in
the Discord developer portal for guild audience checks. This REST access is separate from
`guild_members`, which controls the Gateway subscription to member events
([Discord HTTP restrictions](https://docs.discord.com/developers/events/gateway#http-restrictions)). Its default `false`
does not skip audience checks. Missing REST access returns `forbidden` naming the members
endpoint; unavailable or incomplete evidence refuses the post. Posts to additional readers require requester approval through Belle's
tool host. Approval is bound to that call's destination and reader set. Belle checks them again
before sending and refuses a changed audience; request a new approval for a new call.
Scheduled calls with no requester cannot approve wider disclosure.

Results retain every confirmed message and its location, including parts posted before a
later failure. An unanswered message or forum/media thread creation request, or a malformed
success response, reports `delivery_uncertain_do_not_retry`: Discord may have accepted it.
Belle does not automatically retry that request. If an approved call returns this result, Belle
warns that delivery is uncertain and the call must not be retried. Internal nonces protect
transport retries of the same request; a new tool call is a new post.
For an upload, `attachment_status` is `confirmed` with `attachment_ids` when Discord returns
each attachment; otherwise it is `unconfirmed`, even if the message itself was posted.

A successful `send_discord_file` receipt counts as a delivered answer, including in detached sessions and
scheduled runs. Belle records its message placement alongside receipts from `post_message`.

## Connection health and event delivery

Temporary Gateway disconnects and missed heartbeat acknowledgements trigger a session resume.
A transport failure during reconnect closes the failed connection and retries with increasing waits.
Close codes 4007 (invalid sequence) and 4009 (expired session) start a new session with Identify.
Gateway frames continue while message admission waits. Interactions bypass that queue so Belle can acknowledge them within Discord's three-second deadline.
`pending_events_max` limits queued events to bound memory use when admission is slower than incoming events.
Queue overflow stops the connector and reports the required queue size and setting through terminal health.
Waiting events are abandoned on terminal failure. Interactions continue to bypass the queue while it is within the limit.
Fatal close codes, refused Identify attempts, and unexpected reader failures stop connector work
and report the cause through the host's health lifecycle. A launcher serving only Discord exits
with status 1 after a terminal loss.

Reaction and member delivery IDs include the Gateway session and dispatch sequence. A replay
within a resumed session keeps its ID; separate actions and new sessions have distinct IDs.
Reaction events include the actor's bot status so the host can ignore other applications.
Belle reads the user from the member payload or looks up the user when Discord omits it.
A failed user lookup drops the reaction.
Member events use the guild's system channel after checking its channel type and the bot's
View Channel and Send Messages permissions, including channel overwrites. If that channel is
missing or unusable, `member_event_channels` can name a fallback text or announcement channel
for each guild. Belle checks its guild and permissions too. If neither channel is usable, Belle drops
the event and logs the reason.

## Several bot accounts

Configure one entry per application under `platforms.discord.bots`:

```yaml
platforms:
  discord:
    bots:
      - name: belle
        token_env: DISCORD_BELLE_TOKEN
      - name: helper
        token_env: DISCORD_HELPER_TOKEN
```

Set each variable separately. Empty entries, duplicate names, duplicate references and duplicate token
values are refused. Without `bots`, Belle uses `DISCORD_BOT_TOKEN` for one bot.
Each bot registers `/belle` for its own application and owns its Gateway, REST client, questions,
interaction nonce, caches and health. Settings beside `bots` apply to all entries.

A named bot's workspace is `belle:<guild_id>` in a guild and `belle:direct` in DMs. Use that full value
in `match.workspace`; `match.chat` and `match.user` keep raw Discord IDs. Raw guild IDs remain in
`discord_guild_id` tool metadata and API requests. Renaming a bot changes its workspace and session
identity, so keep names stable. Existing unqualified single-bot sessions are not migrated.

Inbound messages, commands, schedules, questions, approvals, Stop clicks and tool calls retain their
originating bot identity. Unknown identities are refused. A failed bot does not stop the other bots.
A bot that Discord does not answer at start, or answers with a server error or a rate limit, is logged
as NOT CONNECTED and connected again in the background, after 1 s and then twice as long each time, up
to 30 s. Until it connects, Belle refuses every action for that bot. When no bot connects at start, the
start stops. `DiscordBots.health` reports each bot as up, connecting, not served, or its terminal failure
reason. Aggregate terminal health becomes down when every started bot is down and no bot is still
connecting; a partially available process continues serving healthy bots and logs failed entries.

For the installation helper, add `--bot belle` to select the application's configured environment
reference. Invite generation still works without a token; `--verify-token` checks only the selected bot.

## Second Brain ingestion

The default `second_brain.ingest.channels: [public]` selects no Discord
channels. A guild text channel can restrict readers with role grants or
member overwrites. A bot receiving a message proves only the bot's access.
Unknown visibility is treated as private for ingestion, including after a
restart. Public and archived threads inherit their parent's permissions;
private threads also require membership or Manage Threads. Their type or
ID does not prove a public audience.

Opt in with explicit parent channel IDs, for example
`second_brain.ingest.channels: ['<channel-id>']`. This selects that
channel and its public threads, including archived public threads whose
messages Discord delivers to the bot. A private thread requires its own ID
in this setting. A thread ID alone does not select its parent conversation.
`['*']` selects every delivered guild conversation.
DMs are never ingested, even when their ID is listed.

An opted-in attachment or link enters the deployment's shared corpus and
can be recalled in other allowed turns. Recall does not recheck the source
channel's Discord permissions. Choose IDs only when their documents may be
shared with all users who can access the corpus. Ingest status uses the
configured brain reactions. A reaction event contains no document
and does not ingest its anchor message. Host integrations need a Second
Brain backend; without one, ingestion is disabled and brain tools report
that no backend is loaded.
