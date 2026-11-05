# Feature matrix

What Belle shows and does depends on the platform and on the
harness. The platform is the messaging service where people talk to Belle.
It sets how a reply, a question, a mark or a stop looks. The harness runs
the model turns. It sets which scope settings apply to a turn, and how a
turn stops or waits for a person.

The first table compares the platforms. The second table compares the
harnesses. A cell with a number has a note below its table. Each note links
to the guide that gives the details.

## Platforms

Discord, Mattermost and Matrix are experimental.

| Feature | Slack | Mattermost (experimental) | Matrix (experimental) | Discord (experimental) |
| --- | --- | --- | --- | --- |
| Direct messages | Yes | Yes | Yes | Yes (36) |
| Mentions | Yes | Yes | Yes (1) | Yes |
| Replies in a thread of the bot | Yes (2) | Yes (2) | Yes (2) | Yes |
| Ambient replies | Yes (3) | Yes (3) | Yes (3) | Yes (35) |
| Streamed reply | Yes (4) | Post edits (4) | Notice edits (4) | Message edits (34) |
| Activity card | Yes | Yes | Yes | Yes |
| Stop a turn | Button (5) | Button (6) | ⏹️ reaction (7) | Button |
| Questions | Buttons and forms (8) | Buttons (9) | Poll (10) | Buttons and forms |
| Approvals | Buttons | Buttons (9) | Poll (10) | Buttons |
| Private replies | Ephemeral | Ephemeral (11) | No (12) | Interaction response |
| Commands | `/belle` (13) | `/belle` (14) | `!belle` (15) | `/belle` |
| Reactions as marks | Yes | Yes | Yes (16) | Yes |
| Typing or status | Thread status (17) | Typing | Typing | Typing |
| Files in a message | Yes (18) | Yes (19) | Yes (19) | Yes (37) |
| Files sent by Belle | Opt-in (20) | Yes | Yes | Yes (38) |
| History tools | Yes | Yes | Yes (21) | Yes |
| Search tools | Yes (22) | Yes (23) | Yes (24) | Yes (39) |
| Pins | Read, pin, unpin | Read, pin, unpin | Read, pin, unpin (25) | Read, pin, unpin |
| Events | Reaction, member, pin, App Home | Reaction, member | Reaction, member, pin | Reaction, member (40) |
| Second Brain ingest | Yes (26) | Yes (27) | Yes (28) | Yes (41) |
| End-to-end encryption | No | No | Yes (29) | No |
| Several workspaces | Yes (30) | Yes (31) | Yes (32) | Several bot accounts |
| Home tab | Yes | No | No | No |
| Canvases | Yes | No | No | No |
| Lists | Yes | No | No | No |
| Bookmarks | Yes | No | No | No |
| Charts from a reply | Yes (33) | No | No | No |

1. The bot's user ID in `m.mentions` is a mention. A message with no `m.mentions` object is a mention when its `formatted_body` holds a `https://matrix.to/#/` link to the bot's user ID. The bot's name in plain text is not. See [Matrix messages](platforms/matrix.md#messages).
2. Slack takes a reply under one of the bot's own messages when the mode includes `reply`. Mattermost and Matrix take a message in a thread whose first message is the bot's, and Matrix also takes a direct reply to the bot. See [Slack reading modes](platforms/slack.md#channel-reading-modes) and [Mattermost messages](platforms/mattermost.md#messages).
3. A scope with `delivery.mode: [all]` and `reply: optional` gives each message a turn, and the turn speaks only when it calls `post_message`. See [Slack delivery settings](platforms/slack.md#delivery-settings) and [Mattermost ambient channels](platforms/mattermost.md#ambient-channels-and-the-decision-gate).
4. Off by default. `enable_streaming: true` turns it on. Mattermost and Matrix edit one post at most once a second, and continue in a new post when the text is too long for one. See [Mattermost running turn](platforms/mattermost.md#a-running-turn) and [Matrix running turn](platforms/matrix.md#a-running-turn).
5. The Stop button on the activity card, Slack's own stop button on an app with the agent surface (manifest tier 3), and `/belle stop`. See [Slack commands](platforms/slack.md#use-the-bot).
6. The Stop button on the activity card, and `/belle stop`. Both need the action listener. See [Mattermost running turn](platforms/mattermost.md#a-running-turn).
7. ⏹️ on the activity card or on the message that started the turn. ⏹️ on a waiting message removes it. `!belle stop` does the same. See [Stop by reaction](platforms/matrix.md#stop-by-reaction).
8. A question with options gets buttons. A question with no options gets a text input. See [Slack reading modes](platforms/slack.md#channel-reading-modes).
9. One button for each option. Buttons need the action listener. Without it, Belle refuses a call that needs an approval, with `approval_required`. A question with no options cannot be asked: on OpenCode the model reads why and goes on, and on another harness its turn fails with the reason. See [Mattermost questions](platforms/mattermost.md#questions-and-approvals).
10. One poll in the turn's thread, with at most 20 options. A question with no options or with more than 20 options cannot be asked: on OpenCode the model reads why and goes on, and on another harness its turn fails with the reason. A reaction does not answer. See [Matrix questions](platforms/matrix.md#questions-and-approvals).
11. The answer to `/belle` and to a click needs no permission. Any other ephemeral post needs the bot to hold `create_post_ephemeral`. See [Ephemeral messages](platforms/mattermost.md#ephemeral-messages).
12. Matrix has no message that only one person sees. Belle refuses a post with `ephemeral_to`. A private question goes to the direct room with the person. See [What Matrix does not offer](platforms/matrix.md#what-matrix-does-not-offer).
13. Needs a tier 1 or higher manifest. Slack does not allow a slash command in a thread. See [Slack commands](platforms/slack.md#use-the-bot).
14. Needs the slash command on the server, its token, and the action listener. See [Slash command](platforms/mattermost.md#slash-command).
15. A message that starts with `!belle`, or with `/belle` sent as a message. Element refuses `/belle` as an unknown command. See [Matrix commands](platforms/matrix.md#commands).
16. In an encrypted room, the server can read each reaction, so it can see which messages Belle handled. See [What the server can read](platforms/matrix.md#what-the-server-can-read).
17. `thinking_status` sets the text. `acknowledge_mode: off` turns off all statuses. See [Slack feedback settings](platforms/slack.md#feedback-streaming-and-decisions).
18. Needs a tier 1 or higher manifest. Tier 0 cannot read files. See [Install the Slack app](platforms/slack.md#install-the-slack-app).
19. Belle reads at most `file_download_max_bytes` and sends at most `file_upload_max_bytes` of each file, 30 MiB by default. The server's upload limit can be lower for a send. A Matrix message carries one file. See [Mattermost files](platforms/mattermost.md#files-of-a-message) and [Matrix files](platforms/matrix.md#files-of-a-message).
20. `send_file_allowed: true` turns on the file tool. The default is `false`. See [Slack rendering settings](platforms/slack.md#rendering-and-other-features).
21. The bot cannot decrypt a message sent before it joined an encrypted room. The tool lists such a message as `undecryptable`. See [Matrix tools](platforms/matrix.md#tools).
22. The search tool needs the `search:read.*` scopes and `search_enabled`, which defaults to `true`. See [History and search tools](platforms/slack.md#history-and-search-tools).
23. The server searches only the channels where the bot is a member. The result names each open channel that it did not search. See [Mattermost tools](platforms/mattermost.md#tools).
24. The server does not search an encrypted room. The result names each room that it did not search. See [Search, names and pins](platforms/matrix.md#search-names-and-pins).
25. To pin, the bot needs the power level that the room sets for pins, 50 by default. See [Search, names and pins](platforms/matrix.md#search-names-and-pins).
26. On JiuwenSwarm, ingest needs the Second Brain extension. See [Second Brain per platform](second-brain.md#per-platform-behaviour).
27. With Belle's own launcher only. On JiuwenSwarm, the Mattermost channel ingests nothing. See [JiuwenSwarm limits](integrations/jiuwenswarm.md#limits).
28. `public` and `*` select only rooms without encryption. Only its own ID selects an encrypted room. A direct room is never ingested. See [Matrix Second Brain](platforms/matrix.md#second-brain).
29. The bot cannot read history from before it joined, or a message from a person whose client sends keys only to verified devices. See [Encryption](platforms/matrix.md#encryption).
30. A `workspaces` list, with one Slack app for each workspace. See [Connections and message admission](platforms/slack.md#connections-and-message-admission).
31. A `servers` list, with one bot account and one action listener for each server. See [Several servers](platforms/mattermost.md#several-servers).
32. An `accounts` list, with one helper process, store and recovery key for each account. The workspace is the account's name. See [Several accounts](platforms/matrix.md#several-accounts).
33. Tables, and `mermaid` and `vega-lite` fences that map to a Slack chart. See [Reply formatting](platforms/slack.md#reply-formatting).
34. Streaming is off by default. With `enable_streaming: true`, Belle edits one message about once a second and continues in a new message when needed. See [Discord running turn](platforms/discord.md#a-running-turn).
35. A scope with `delivery.mode: [all]` can start a turn from an unmentioned guild message. Discord must have the privileged Message Content intent enabled. See [Discord setup](platforms/discord.md#set-up-the-application).
36. Belle supports direct messages to the bot. Group DMs are unsupported. See [Discord messages](platforms/discord.md#messages).
37. Belle reads each incoming file up to 30 MiB by default. `file_download_max_bytes` changes the limit. See [Discord files](platforms/discord.md#messages).
38. The files in one outgoing request total at most 25 MiB. See [Discord tools](platforms/discord.md#tools).
39. Guild search needs the Message Content intent. Discord indexing delays, withheld matches, and offset limits can make coverage partial. See [Discord tools](platforms/discord.md#tools).
40. Member events need `guild_members: true` and Discord's Server Members intent. See [Discord setup](platforms/discord.md#set-up-the-application).
41. The default `[public]` selects no Discord channels for ingestion. Select explicit channel IDs to ingest guild conversations; DMs are excluded. See [Discord ingestion](platforms/discord.md#second-brain-ingestion).

## Harnesses

OpenCode v2, Docker Agent and ACP are experimental.

| Feature | JiuwenSwarm | OpenCode v2 (experimental) | Docker Agent (experimental) | ACP (experimental) (30) |
| --- | --- | --- | --- | --- |
| Platforms | Slack, Mattermost, Matrix (1) | Slack, Mattermost, Matrix, Discord | Slack, Mattermost, Matrix, Discord | Slack, Mattermost, Matrix, Discord |
| Per-turn tool narrowing | No (2) | Yes (3) | Yes (4) | No (31) |
| Tool approvals | Buttons (5) | Yes (6) | Yes (6) | Harness actions (32) |
| Questions from the model | Yes | Yes (7) | No (8) | No |
| Scheduled jobs | Slack only (9) | Yes (23) | Yes (23) | Not tested |
| Detached sessions and `/belle bg` | No (24) | Yes | Yes | Not tested |
| `session_continue` | No (24) | Yes | Yes | Yes (36) |
| `session: shared` schedules | No (25) | Yes | Yes | Not tested |
| Detached session run after a Belle restart | No (24) | Recovered | Interrupted (26) | Not tested |
| Chat turn after a Belle restart | No (27) | Delivered (28) | Interrupted (26) | Interrupted (33) |
| Idle expiry of attached sessions | No (29) | Yes | Yes | No (34) |
| Subagents | Yes | No (10) | Partial (11) | No |
| Experts | Yes | No | Yes (12) | No |
| Skills scopes | Yes | Yes (13) | Partial (14) | No |
| Model selection | Yes | Yes | Yes (15) | No |
| Task models | Yes (16) | Yes | Yes | Yes |
| Second Brain | Yes (17) | Yes (18) | Yes (18) | Not tested |
| Stop a turn | Yes | Yes | Yes (19) | Yes (35) |
| Message joins a running turn | Slack only (20) | No (21) | Yes (22) | No (21) |

1. Slack replaces JiuwenSwarm's built-in connector. Mattermost and Matrix use separate Belle extensions. JiuwenSwarm does not mount Belle's Matrix tools. See [Use Belle with JiuwenSwarm](integrations/jiuwenswarm.md#matrix-channel-experimental).
2. While a scope sets `permissions.tools`, Belle refuses every tool call of every Slack and Mattermost turn, because the tool call does not know its conversation. See [Tool permissions of a scope](integrations/jiuwenswarm.md#tool-permissions-of-a-scope).
3. A tool at `deny` is not in the model's tool list for that turn. `deny_tools` also removes OpenCode's own tools. See [Tool access](backends/opencode.md#tool-access).
4. A tool at `deny` or `ask` becomes a session permission. The model still sees a denied tool in its list, and Belle refuses the call. See [Docker Agent scopes](backends/docker-agent.md#scopes).
5. JiuwenSwarm's permission engine asks with buttons. A post that reaches more people than the turn's conversation asks the requester first. See [Tools and approvals](integrations/jiuwenswarm.md#tools-and-approvals).
6. A tool at `ask` waits for a person, who answers on the platform. The `clicks.approve` rule says who may answer. See [Docker Agent scopes](backends/docker-agent.md#scopes) and [Held calls](platforms/README.md#held-calls).
7. OpenCode's own `question` tool.
8. Belle supports only the approval of a tool call on Docker Agent. Docker Agent has a `user_prompt` toolset that asks questions, but Belle does not support it yet.
9. A job delivers to Slack. A job cannot target Mattermost or Matrix. Belle refuses such a job, and a heartbeat follow-up asked for in a Mattermost or Matrix turn. See [JiuwenSwarm limits](integrations/jiuwenswarm.md#limits).
10. OpenCode does not apply `agent.subagents`, and Belle warns at start. Belle refuses `/belle subagents use`. `deny_tools` can remove OpenCode's own `subagent` tool. See [Tool access](backends/opencode.md#tool-access).
11. `agent.subagents` applies: Belle refuses a call to a subagent that the scope does not allow, but the model still sees it listed. Belle refuses `/belle subagents use`. The shipped agent YAML declares no subagent. See [Agents and models](backends/docker-agent.md#agents-and-models).
12. Where the agent YAML declares experts. The shipped agent YAML declares none. See [Agents and models](backends/docker-agent.md#agents-and-models).
13. A skill that the scope does not allow is not in the model's skill list for that turn. See [Tool access](backends/opencode.md#tool-access).
14. Belle refuses a skill that the scope does not allow when the model asks for it, but the model still sees it listed. For `agent.skills.required` and `/belle skills use`, Belle adds the text of a skill that ships with Belle. It refuses any other skill. See [Docker Agent scopes](backends/docker-agent.md#scopes).
15. A model that the agent YAML names in `models`. See [Docker Agent scopes](backends/docker-agent.md#scopes).
16. The summarizer can also use JiuwenSwarm's own models. See [Task models](task-models.md).
17. Slack ingests and recalls. Mattermost recalls only. The AgentServer's rail recalls without a wait, and not for a cron or heartbeat run. See [Second Brain per platform](second-brain.md#per-platform-behaviour).
18. Belle ingests and recalls in its own process, and waits for the recall before the turn starts. See [OpenCode Second Brain](backends/opencode.md#second-brain).
19. A stop closes the turn's stream. See [Sessions and stops](backends/docker-agent.md#sessions-and-stops).
20. Under `delivery.mid_turn: steer`, in a session for each thread. A channel-wide session queues the message. Mattermost queues it. See [Slack delivery settings](platforms/slack.md#delivery-settings).
21. The message waits for a turn of its own.
22. Where the agent YAML relays `user_steering_messages_submit`. The shipped agent YAML does. See [Sessions and stops](backends/docker-agent.md#sessions-and-stops).
23. Belle stores scheduled jobs and delivers their results. See [Scheduled jobs](scheduling.md).
24. Detached sessions need Belle's scheduler, and Belle does not run it on JiuwenSwarm. See [Select the scheduler](scheduling.md#select-the-scheduler).
25. JiuwenSwarm's scheduler runs each job in a new session. See [Select the scheduler](scheduling.md#select-the-scheduler).
26. A Docker Agent run ends with the request that streams it. This is a limit of Docker Agent. See [Sessions and stops](backends/docker-agent.md#sessions-and-stops).
27. On Slack, JiuwenSwarm runs each chat turn, and Belle keeps no record of it. On Mattermost and Matrix, Belle cannot read or stop a turn of an earlier process. The next process logs the turn, posts a notice that the answer is lost, and marks the message as failed. See [A running turn](platforms/README.md#a-running-turn).
28. After a crash or a stop of Belle, OpenCode still runs the turn, and the next Belle process delivers its answer once. The turn timeout still applies, and the turn budget does not. See [A running turn](platforms/README.md#a-running-turn).
29. Belle does not yet expire the session of a JiuwenSwarm chat place. Belle refuses `attached_sessions.expire_after_idle_hours` on JiuwenSwarm. See [Attached sessions](config.md#attached-sessions).
30. Belle drives an agent harness over the Agent Client Protocol, through a relay socket. The column holds what Belle checked on OpenCode and Docker Agent. One key switches a harness between its integration and ACP. See [Switch a harness](config.md#switch-a-harness-between-its-integration-and-acp). For Docker Agent, see [Bindings](backends/acp.md#bindings). With a connection for each conversation, each agent process lives until Belle closes its connection. No idle limit ends it. See [ACP backend](backends/acp.md).
31. Belle still enforces `deny` and `ask` on its own tools, and refuses a call at `ask`. See [Limits](backends/acp.md#limits).
32. The agent's permission requests, as approvals. See [Permissions](backends/acp.md#permissions).
33. A restart ends the connection, and the relay ends the agent process. The next Belle process reports the turn as failed, with an unknown outcome. See [Restarts](backends/acp.md#restarts).
34. The pinned ACP SDK has no client method for `session/delete`, so Belle deletes no ACP session.
35. A stop sends `session/cancel`. See [Turns](backends/acp.md#turns).
36. Belle checked it against a scripted ACP agent only. After a Belle restart, a continuation needs an agent that advertises `session/resume` or `session/load`. See [Restarts](backends/acp.md#restarts). On a shared connection, a continuation also fails after a stop that the agent did not answer in time. See [Turns](backends/acp.md#turns).
