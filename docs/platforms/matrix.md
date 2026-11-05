# Matrix platform (experimental)

The Matrix platform connects Belle to a Matrix homeserver as one user
account, or to several accounts: see [Several accounts](#several-accounts). A helper process, `belle-matrix-helper`, holds the account's
encryption and all its traffic with the server. The helper uses
matrix-rust-sdk, the library of Element, for each encryption step. Belle
talks to the helper on a local socket. The launcher image holds the helper.
The Python package does not: install the helper beside it to run Belle
outside the image.

## Messages

| Matrix | Belle |
| --- | --- |
| A room with two members that the bot's `m.direct` names | Chat type `direct`. Each message starts a turn. |
| A room whose join rule is `public` | Chat type `public` |
| Any other room | Chat type `private` |
| The bot's user ID in `m.mentions`, or, in a message with no `m.mentions` object, a `https://matrix.to/#/` link to the bot's user ID in `formatted_body` | A mention. `@room` and a display name in plain text are no mention. |
| A reply to the bot, or a message in a thread whose first message is the bot's | A reply |
| A notice (`m.notice`), a sender in `bot_user_ids`, or another account of `accounts` | A bot's message. It starts no turn. |
| An edit by its sender | An edit. It starts no turn. An edit by another person is dropped. |
| `m.image`, `m.file`, `m.audio`, `m.video` | The message's file. See [Files of a message](#files-of-a-message). |
| `!belle` or `/belle` first, or first after a mention of the bot | A command. See [Commands](#commands). It starts no turn. |

Belle answers in the thread of a room message, and at the top of a direct
room. Answers are notices. An answer larger than one event goes in parts:
at most 60,000 bytes each in a room without encryption, and 44,000 in an
encrypted room.

The bot joins a room when a person in `allow_from` invites it, or on every
invite where `allow_from` is empty. A message that reaches Belle more than
`catch_up_seconds` after it was sent, such as after downtime, starts no
turn. Time a message waits while Belle handles earlier ones, such as marks
that wait out the server's rate limit, does not count. Such a message gets
the `late_emoji` reaction (⏰) where its turn would have owed an answer, as
for a direct message or a mention, so that its sender knows to send it
again. A message whose reply is optional gets no reaction.

## Encryption

The helper decrypts what the bot's device holds a key for. The bot cannot
read a message sent before it joined a room, or a message from a person
whose client sends keys only to verified devices. In a direct room, the bot
tells the sender once per encryption session that it cannot read a message.

### The bot's identity

The account has a cross-signing identity, and its recovery key unlocks the
identity's keys on the server. The platform starts only with a device that
the identity signed (a cross-signed device). Element then puts no warning on the bot's
messages.

| State at start | What the platform does |
| --- | --- |
| The account has no identity | It makes one with the password, writes the new recovery key to `recovery_key_output`, and stops. You keep the key and start again. |
| A new store, and the account has an identity | It logs in as a new device and signs it with the recovery key. Without the key it stops, and names `MATRIX_RECOVERY_KEY` and `MATRIX_RECOVERY_KEY_FILE`. |
| The store exists | It opens the store with `MATRIX_STORE_PASSPHRASE`. A wrong passphrase stops it. |
| The device is not signed for any other cause | It stops, and names the cause. |

Keep the recovery key as a secret, outside the store and outside its
volume. Prefer `MATRIX_RECOVERY_KEY_FILE` where a shell tool runs in Belle's
container: a shell tool can read the environment of Belle's process.

### The store

The crypto store is in `store_dir`, on Belle's state volume. It holds the
device's keys and the encrypted SQLite databases. `MATRIX_STORE_PASSPHRASE`
encrypts them. The helper locks the store. A second process on the same
store, such as an operator command while Belle runs, is refused with
`store_locked`.

**Never restore the store from a copy.** A device whose store is an old copy
accepts again one-time keys that it used already, which opens a replay of
old encrypted sessions. The server refuses the upload of such a key, and
matrix-sdk reports it. Then the platform stops and logs:

```text
matrix stops: the server refused a one-time key upload as a duplicate, so this store is an old copy. Discard the store, start as a new device and recover with the recovery key
```

The other platforms of the host go on. To fix it, remove the store's volume, and start Belle with the recovery key. Belle logs in as a new
device. Then remove the old device with
[`delete-devices`](#operator-commands).

A store that is lost has the same fix: a new device and the recovery key.
Each new device adds a device to the account. Belle logs the other devices
of the account at each start (`other devices of this account`).

### What the server can read

In an encrypted room the server stores ciphertext for the text of each
message, each file, each poll and each vote. Some events of Matrix are
never encrypted, in Element too:

| Readable by the server | What the server learns |
| --- | --- |
| Reactions | Who reacted with which emoji on which message. Element sends a person's reactions in clear. matrix-sdk sends the bot's reactions in clear too. Thus the bot's marks (👀, ⏳, ✅ and the others), its brain marks and ⏹️ tell which messages Belle handled. ⏹️ also tells who stopped a turn. |
| Room state | The members, the room's name and topic, the power levels and the list of pinned message IDs |
| Typing and read receipts | When Belle types in a room, and which messages it read |
| Relations | Which message an edit, a reply or a reaction belongs to, and each thread |
| Metadata | The sender, the room and the time of each event |

For this reason a question is a poll, not a reaction: a vote is encrypted.

### Lost recovery key

Without the recovery key, a new device cannot become signed. The fix is an
identity reset, with the account's password. The reset removes the key
backup of the account and makes a new identity and a new recovery key.

- Each other device of the bot loses its signature. It starts again only
  with the new key.
- Element tells each person who shares a room with the bot that its
  identity was reset, until they click **Dismiss**.
- A new device cannot read the messages of the old backup.

Run the reset as an [operator command](#operator-commands):
`reset-identity --yes`.

## Quick start with containers

The launcher image runs Belle on Matrix with no change. Its example
configuration turns the `platforms.matrix` section on when
`MATRIX_HOMESERVER`, `MATRIX_USER_ID` and `MATRIX_STORE_PASSPHRASE` are set.
This quick start uses the OpenCode harness. Build both images, and write
`model.env` and `pair-opencode.env`, as in
[the OpenCode quick start](../../containers/opencode/README.md). First do
the steps of [Set up on a Matrix server](#set-up-on-a-matrix-server).

`belle.env` (Belle only), mode `0600`, with literal values:

```dotenv
MATRIX_HOMESERVER=https://matrix.example.org
MATRIX_USER_ID=@belle:example.org
MATRIX_PASSWORD=replace-with-the-account-password
MATRIX_STORE_PASSPHRASE=replace-with-a-random-passphrase
MATRIX_RECOVERY_KEY_OUTPUT=/home/belle/.local/share/belle-agent/matrix-recovery.key
```

`MATRIX_HOMESERVER` is the address of the server's client API, as Belle
reaches it. Make the passphrase with `openssl rand -hex 32`. Keep it with
the store: a store without its passphrase is lost.
`MATRIX_RECOVERY_KEY_OUTPUT` is on Belle's data volume, not on the state
volume of the store.

The first start makes the account's identity and stops. These commands
work in bash and in fish:

```sh
podman network create belle-net
podman run -d --name belle-opencode --network belle-net \
  --env-file model.env --env-file pair-opencode.env \
  localhost/belle:opencode-local
podman run -d --name belle --network belle-net \
  --env-file pair-opencode.env --env-file belle.env \
  -v belle-state:/home/belle/.local/state/belle-agent \
  -v belle-data:/home/belle/.local/share/belle-agent \
  localhost/belle:launcher-local
podman wait belle
podman logs belle
```

The log ends with:

```text
belle cannot start: matrix cannot start: a new identity was made: keep the recovery key as a secret, then start again. The key is in the file of platforms.matrix.recovery_key_output: set MATRIX_RECOVERY_KEY or MATRIX_RECOVERY_KEY_FILE from it
```

Copy the key out of the stopped container. Keep a copy in your password
manager. Add it to `belle.env`, then remove the file from the data volume:

```sh
podman cp belle:/home/belle/.local/share/belle-agent/matrix-recovery.key matrix-recovery.key
chmod 600 matrix-recovery.key
printf 'MATRIX_RECOVERY_KEY=%s\n' "$(cat matrix-recovery.key)" >> belle.env
podman rm belle
podman run --rm -v belle-data:/home/belle/.local/share/belle-agent localhost/belle:launcher-local \
  rm /home/belle/.local/share/belle-agent/matrix-recovery.key
```

Start Belle again with the same command:

```sh
podman run -d --name belle --network belle-net \
  --env-file pair-opencode.env --env-file belle.env \
  -v belle-state:/home/belle/.local/state/belle-agent \
  -v belle-data:/home/belle/.local/share/belle-agent \
  localhost/belle:launcher-local
podman logs belle
```

The log ends its start with `matrix connected: user=@belle:example.org
device=<id> backup=enabled`, then `belle is serving: platform=matrix
backend=opencode`. Invite the bot to a room, or open a direct room with it.
The bot joins on the invite.

Keep the two volumes. `belle-state` holds the store: a recreated container
with it is the same device. Never copy it back from a backup. Belle needs
`MATRIX_PASSWORD` only for a new device and for the operator commands. You
can remove it from `belle.env` after the second start.

The `compose.yaml` of each harness starts the same two containers with the
same volumes, with this `belle.env`, `model.env` and a `.env` that names
the images. See [the OpenCode quick start](../../containers/opencode/README.md#one-command).
Compose has no restart policy for Belle, so the first start stops the
container as above. Read the key with
`podman compose run --rm belle cat /home/belle/.local/share/belle-agent/matrix-recovery.key > matrix-recovery.key`.
Then add it to `belle.env`, remove the file with
`podman compose run --rm belle rm /home/belle/.local/share/belle-agent/matrix-recovery.key`,
and run `podman compose up -d` again.

## Set up on a Matrix server

Belle needs a user account on the server and the rooms that it serves. A
server administrator does the first two steps once.

1. **The account.** Make a plain user account with a password, such as
   `@belle:example.org`. On Synapse, an administrator runs
   `register_new_matrix_user -c homeserver.yaml -u belle --no-admin`. On a
   server that takes a registration token, register the account with the
   token. An appservice is not necessary.
2. **The rate limit.** Synapse limits a normal account to about one message
   every 5 seconds after a burst of 10. Belle waits as the server asks, and
   loses no text, but a streamed reply and the activity card become slow.
   An administrator lifts the limit for the bot with the admin API:

   ```sh
   curl -X POST -H "Authorization: Bearer <admin access token>" \
     -d '{"messages_per_second": 0, "burst_count": 0}' \
     https://matrix.example.org/_synapse/admin/v1/users/@belle:example.org/override_ratelimit
   ```

3. **Rooms.** Invite the bot to each room that it serves. The bot joins on
   an invite from a person in `allow_from`, or on every invite where
   `allow_from` is empty. It declines the other invites. A person opens a
   direct room with the bot from their client.
4. **Pins.** `pin_message` needs the power level that the room sets for
   `m.room.pinned_events`, 50 by default. A room administrator gives the
   bot that level, or the bot cannot pin.

The first start of Belle then makes the bot's identity, as the
[quick start](#quick-start-with-containers) says. The bot needs no
interactive verification (no emoji check). Its device is cross-signed, so
Element puts no warning on its messages in the default settings.

## Settings

See [Matrix in the configuration guide](../config.md#matrix-experimental) for each key
and each secret.

## Several accounts

One Belle can serve several Matrix accounts at once, on one homeserver or on
several. One account reaches every room of the servers that its server
federates with, so a second account is for a second bot identity, or for a
server that does not federate. List the accounts under `accounts`:

```yaml
platforms:
  matrix:
    accounts:
      - name: work
        homeserver: https://matrix.example.com
        user_id: "@belle:example.com"
        store_passphrase_env: MATRIX_WORK_STORE_PASSPHRASE
        password_env: MATRIX_WORK_PASSWORD
        recovery_key_env: MATRIX_WORK_RECOVERY_KEY
        recovery_key_output: /home/belle/.local/share/belle-agent/matrix-work-recovery.key
      - name: lab
        homeserver: https://lab.example.org
        user_id: "@belle:lab.example.org"
        store_passphrase_env: MATRIX_LAB_STORE_PASSPHRASE
        password_env: MATRIX_LAB_PASSWORD
        recovery_key_env: MATRIX_LAB_RECOVERY_KEY
        recovery_key_output: /home/belle/.local/share/belle-agent/matrix-lab-recovery.key
```

| Key of an account | Meaning |
| --- | --- |
| `name` | The account's name in Belle: 1 to 32 letters, digits, `-` and `_`. It is the workspace of each conversation of the account, for sessions, scopes and log lines. Two names that differ only in case are one name. |
| `homeserver`, `user_id` | As the section's keys of the same names. `${NAME}` reads one from the environment. |
| `store_dir` | The account's store. Default: `<state dir>/matrix/<name>`. |
| `recovery_key_output` | As the section's key. Give each account its own file. |
| `store_passphrase_env` | Required. The name of the variable that holds the store passphrase. Belle also reads the file that the variable with `_FILE` names, as for `MATRIX_STORE_PASSPHRASE`. |
| `password_env`, `recovery_key_env` | The names of the variables of the password and the recovery key, in the same way. Without one, Belle reads no such secret for the account. |
| `enabled` | Empty: on when `homeserver`, `user_id` and the store passphrase resolve. `false`: off. `true`: a missing one stops the start. |

A non-empty list takes the place of the section's `homeserver`, `user_id`,
`store_dir` and `recovery_key_output`. One of those that resolves beside the
list stops the start, so remove them, or leave their variables unset. Every
other key of the section applies to each account. `allow_from`,
`allowed_channel_ids` and the scopes hold the user IDs and room IDs of every
account.

An account is on when its `homeserver`, `user_id` and store passphrase
resolve, and off when none does. An account with some of the three stops
the start, and the error names each missing key or variable. The platform is
on when one account is on. Two accounts with the same `name`, `user_id`,
`store_dir` or `recovery_key_output` stop the start too: two stores of one
account are two devices, and each would answer the same message. Two
entries can also name one account in ways the settings do not show, such as
a user ID written in another case, which a homeserver may log in to the same
account. Belle compares the accounts that the homeserver logged in, and an
entry that logged in to an account that another entry serves is NOT SERVED.
Where that start made a new device, Belle logs it out and removes its store.
A device that the entry's store held before stays on the account: remove the
entry, and remove the device with [`delete-devices`](#operator-commands) if
nothing uses it.

Each account has its own helper process, store, device and recovery key.
The first start of each account makes its identity and writes its key, as
in the [quick start](#quick-start-with-containers). An error log names an
account that cannot start, for example on its first start or with a wrong
passphrase, and the other accounts serve. An account whose homeserver does
not answer, or answers with a server error or a rate limit, is logged as NOT
CONNECTED and started again in the background, after 1 s and then twice as
long each time, up to 30 s, until it serves or refuses. Until then, Belle
refuses every action of that account: nothing is posted from it. When no
account can serve, the start stops with the reason of each. The platform stops for good, and a
process with no other platform exits, once every account that served has
stopped for good.

Nothing of one account reaches another:

- The workspace of a conversation is the name of its account. The
  section's one account has the workspace `default`.
- A reply, a mark, a card, a poll and typing go out from the account that
  received the message.
- A tool call runs with the account of its turn. A room that only another
  account joined is a room the bot did not join, and the tool refuses it
  with `chat_not_joined`. A turn whose account no longer serves gets
  `matrix_account_unknown`.
- Each account takes the other accounts for bots, as if `bot_user_ids`
  listed them. In a room they share, one account's messages and marks start
  no turn of the other, and its ⏹️ stops none.

Where two accounts are in one room, each answers what triggers it: a
mention of that account, a reply to it, or a message in a room whose scope
answers every message. With `mode: all`, both accounts take part. Leave one
account out of such a room, or give it a scope for that room that answers
mentions only:

```yaml
scopes:
  - match: {channel: matrix, workspace: "lab", chat: "!room:example.org"}
    delivery: {mode: [mention]}
```

A move from the section's one account to `accounts` changes the workspace
from `default` to the account's name, and the default store from
`<state dir>/matrix/default` to `<state dir>/matrix/<name>`. Set `store_dir`
to the earlier store to keep the device. The sessions of the earlier
workspace are not continued, and a scope that names the workspace needs the
new name.

## Identifiers

| Kind | Form |
| --- | --- |
| Room, `chat_id` | `!` and an opaque ID, such as `!abc:example.org`. A `chat_id` also takes a user ID, for the direct room with that person. |
| Message, `message_id` | `$` and an opaque ID |
| User, `user_id` | `@name:server` |
| File, `file_id` | The ID of the message that carries the file. A Matrix message carries one file. |

A tool refuses a malformed ID before it does anything else, and names the
argument and the form.

## Tools

| Tool | Use |
| --- | --- |
| `post_message` | Post in the turn's room, in another room the bot is in, or in the direct room with a person |
| `react_to_message` | Add or remove the bot's reaction on a message of the turn's room |
| `read_conversation` | Read a room, one thread, or an exact event with optional context. A message the bot cannot decrypt has `undecryptable`. A file message lists its `file_id`. |
| `download_file` | Read the file of one message, decrypted |
| `edit_message` | Replace the text of a message that the bot sent. An edit of another person's message is refused: a Matrix server accepts an edit by any member. |
| `delete_message` | Remove (redact) a message that the bot sent. Removal of another person's message is refused. |
| `send_file` | Send a file in its own message, with an optional caption. In an encrypted room the helper encrypts the file before upload. The server accepts empty files. |
| `search_messages` | Search messages, room names and topics in rooms the server searches for the bot. The coverage names each room the server did not search, with the reason. |
| `find_by_name` | Find a person in the server's user directory, or a room among the bot's rooms and the server's public room directory |
| `pin_message` | Pin or unpin a message of the turn's room |
| `read_matrix_pinned_messages` | List the pinned messages of a room, newest first, decrypted where the bot holds the key |

A tool reads for the person who asked, and gives them only what Matrix
lets them read themselves. In an encrypted room that is what was sent after
they joined: the bot can hold older keys, the person cannot. In a room
without encryption, the history visibility `joined` or `invited` sets the
same bound by the join or the invite. `read_conversation`,
`search_messages` and `read_matrix_pinned_messages` leave older messages
out, and their coverage says so with `before_requester_joined: withheld`.
`download_file` refuses an older file with `message_before_requester_joined`.
A run with no requester, such as a scheduled one, has no such bound.
If the membership history exceeds the configured budget, reads and searches
return `membership_history_budget_exceeded`. Raise
`platforms.matrix.membership_history_max_events` and retry.

In a room or thread, `read_conversation` applies `after_iso_utc` to each event's
creation time. It omits older thread roots. It lists and counts undecryptable
events newer than the bound.
Set `event_id` to read one event by ID, including a room-state event.
`context_messages` asks the server for nearby events. These options cannot
be combined with a thread or time window. Belle applies the requester's
history boundary to both the event and its context.

`post_message` returns every sent event ID in send order, including parts
split again after a server size refusal. `parts` counts sent events and
`thread_id` names the first part's thread root, or is empty for a top-level
post. `reply_mode: reply` makes an unthreaded native reply to
`reply_to_message_id`; the result names that event in `reply_to_message_id`.
The default `reply_mode: thread` uses the target's thread. Later parts of a
top-level post form a thread under its first event. The tool sends `m.notice`
unless `message_type` selects `m.text` or `m.emote`. It can include Matrix user
IDs or a room mention; only the first part notifies them.
If a later send fails, the result has `ok: false` and retains all IDs
already sent. Retrying the whole message can duplicate those parts.
A failure before any event is sent has no message ID.

An edit holds its text twice, so its text takes at most 30,000 bytes in a
room without encryption, and 22,000 in an encrypted room. A longer
replacement is refused with `edit_text_too_long` and the limit. `send_file`
sends at most `platforms.matrix.file_upload_max_bytes` (30 MiB by default), or the server's upload limit where that is lower.
The sender can set the MIME type, image or video dimensions, duration in
milliseconds, and an uploaded thumbnail. In an encrypted room the thumbnail
uses the same encrypted upload path as the main file.

### Search, names and pins

A homeserver searches only the rooms that the bot joined, and it cannot
read the messages of an encrypted room. Thus `search_messages` never finds
a message of an encrypted room. Its coverage lists the rooms that the
server searched (`channels_searched`) and the rooms that it did not
(`channels_not_searched`). Each room that the server did not search has a
reason: `encrypted` for a joined encrypted room, `not_joined` for a public
room of the directory that the bot is not in. The result names a room only
where the readers of the turn's room can read it, and counts the others in
`channels_unnamed`. A match in a room that the turn's readers cannot read
is withheld and counted. To read an encrypted room, use
`read_conversation`, which decrypts.

The coverage scan reads ten public-directory pages by default. Raise
`max_public_pages` if it reports `channel_list_incomplete`. The result names
up to 50 unsearched rooms by default and always gives their known total.
Raise `max_unsearched_rooms` to name more.

Search accepts Matrix content keys (`content.body`, `content.name`,
`content.topic`), room, sender and event-type inclusion and exclusion
filters, `recent` or `rank` order, and before/after context counts. A name
or topic match appears in `state_matches` with its native state-event type.
It is a state event. A continuation token is bound to the query, filters,
origin and requester. Use it only with the same options.

The Matrix search specification includes rooms an account has left. On a
Synapse server, a search after leaving a test room returned no match, while
an exact event read and context read still succeeded. Belle reports the
search coverage that the server returned. An empty search after leave
does not prove the room has no matching events. The same server accepted
an empty media upload and returned a zero-byte download. Belle therefore
allows empty files.

The server decides how words match. Synapse with SQLite matches whole
words. It keeps only the first and last three letters of a word that has a
digit, so such a word also matches other words with the same ends. Neither
Synapse nor Tuwunel matched a prefix of a word on the rig. A result with
`older_matches: more_available` gives `next_page_token`. A further call
with `page_token` reads the next page.

`find_by_name` searches the server's user directory with the query.
Synapse lists the people who share a room with the bot or are in a public
room. Use `directory_limit` to request more people when the server marks its
result as limited. The user-directory API has no page token; the server may
still cap the result. For `kind: chat`, `coverage.next_page_token` continues
through joined rooms and then the public room directory. Pass the same query
and `directory_server` on every page. `directory_server` selects another
server's public directory; it does not join the room or grant access to a
private room. Synapse lets nobody list a room in its directory unless
`room_list_publication_rules` allows it.

A Matrix room lets only a member with enough power change its pins:
`m.room.pinned_events` needs power level 50 by default. Where the bot lacks
it, `pin_message` is refused with `pin_not_permitted`, the level that the
room needs and the bot's level. Matrix keeps the pins of a room as one
list, and does not say who pinned each message.
`read_matrix_pinned_messages` says who changed the list last, and lists
each pin that the bot cannot read as a message in
`coverage.not_readable_message_ids`.

## Questions and approvals

Matrix has no buttons. A question or an approval is a poll in the turn's
thread, with one answer per option. An approval has **Approve** and
**Reject**. Belle sends the unstable poll events of MSC3381
(`org.matrix.msc3381.poll.start`): Element supports only these. A poll holds at most
20 options. A question with more options, or with none, cannot be asked. A
harness that can be told, such as OpenCode, hands the model the reason, and
the turn goes on. On another harness the turn ends, and its failure names the
reason.

A vote is encrypted where the room is. A reaction is not: Element sends
reactions in clear in an encrypted room, so the server can read them.
Therefore a reaction does not answer a question. The host decides who may
answer, by `allow_from` and the scope's `clicks.approve`, with the full user
ID. Belle answers a vote in a reply to the poll. The reply notifies nobody.

| Vote | What happens |
| --- | --- |
| By a person who may answer | The answer reaches the turn. The poll ends, and the reply says `Answered by <user id>: <option>`. |
| By a person who may not | The poll stays open. The reply names the person and says that they are not permitted to answer, once per person and poll. Element still counts the vote in the poll's results. |
| On a poll that no longer waits, such as after a restart | The poll ends, and the reply says that the request no longer waits. Nothing is answered. |

A call held for a person's agreement is asked the same way: a post, an edit
or a file that reaches people who cannot read the turn's room, or a tool that
the scope's `permissions` sets to `ask`.

## A running turn

| Surface | What a person sees |
| --- | --- |
| Typing | The room says that Belle is typing while a turn of it runs. `typing_indicator` turns it off. |
| The reply as it is written | With `enable_streaming`, the reply appears in a notice that Belle edits as the model writes. At the end the notice holds the final reply. A notice holds at most 30,000 bytes of text and HTML, or 22,000 in an encrypted room, because an edit holds its text twice. A longer reply goes on in the next notice, cut at a paragraph and not inside a code block. A turn that fails takes its streamed notices down, and the host posts what it has to say. A stopped turn keeps them, marked _Turn stopped_. |
| The activity card | With `activity_card`, a turn that runs longer than `activity_card_delay_seconds` and calls a tool gets a notice in its thread. The card names the running tools and subagents, and Belle edits it at most once per `activity_card_min_edit_seconds`. Belle adds ⏹️ to it. At the end the card says how the turn ended, and Belle takes ⏹️ off it. The card stays: a redaction would strip its thread relation, and Element would put its note for a redacted message in the room's main timeline. Where a reply is optional (`reply: optional`), a turn gets a card only after it has posted, so a silent turn leaves nothing. ⏹️ on the message stops such a turn. |

Belle writes one edit at a time. A rate limit of the server makes an edit
wait: the helper waits as long as the server asks and sends again. Text
that arrives meanwhile goes in the next edit, so the edits become less
frequent and no text is lost. The end of the reply and of the card waits in
the same way, so the final reply arrives whole. Synapse limits an account
without the rate-limit override to about one message every 5 seconds after
a burst of 10.

### Stop by reaction

A person's ⏹️ on the activity card, or on the message that started a turn,
stops the turn (D18). On a message that waits for its turn, ⏹️ removes it.
The person who sent the message may always do so. Anybody else needs the
scope's `clicks.stop` or `clicks.queue_drop`, with the full user ID. A
refusal is one reply in the thread that names the person and notifies
nobody. `!belle stop` and `!belle queue drop` do the same.

Element sends a reaction in clear in an encrypted room, and so does
matrix-sdk for the bot's own reactions. The server can read that a stop was
asked for, by whom, and on which message, but not the text of a message. The
marks of a message (👀, ⏳ and the others) are visible to the server in the
same way.

## Commands

A message whose first word is `!belle` or `/belle`, or whose first word
after a mention of the bot is one, is a command. `command_name` sets the
word. Element answers `/belle` with an "Unknown command" dialog: use
`!belle`, or send `/belle` with **Send as message**. A command works in
every room the bot serves, direct rooms included. The answer is a notice in
the command's thread, in reply to it, that notifies nobody.

| Subcommand | What it does |
| --- | --- |
| `help` | Lists the subcommands. |
| `status` | The agent, the model, and what runs and waits in the room |
| `stop [message]` | Stops the turn that runs in the room. With several, name the message by its event ID or its `matrix.to` link. |
| `queue list`, `queue drop <message>` | Lists the waiting messages, or removes one |
| `models list`, `models current`, `models use <name>`, `models reset` | The model of the room |
| `skills list`, `subagents list`, `experts list` | What the scope lets the room use |
| `ask <question>`, `skills use <skill> <task>`, `subagents use <name> <task>` | A private question. See below. |
| `schedule list`, `schedule show`, `schedule pause`, `schedule resume`, `schedule delete`, `schedule run` | Manages Belle scheduled jobs. See [Scheduled jobs](../scheduling.md). |

Matrix has no message that one person sees alone. A private question goes
to the bot's direct room with the person who asked. Asked from another
room, it opens a thread in the direct room, a conversation of its own that
does not copy the room's conversation. The room where it was asked gets
only a note that the answer is in the direct room. The question itself is
in the command message, which the room's members can read.

A person whom `allow_from` does not admit gets the rejected reaction and
no answer. A room that `allowed_channel_ids` leaves out gets nothing.

## Events

A scope's `delivery.events` selects which events reach the host:
`reaction`, `member` and `pin`, each `off`, `context` or `turn`.

| Event | Matrix | Actor | Message |
| --- | --- | --- | --- |
| `reaction` | `m.reaction`, and a redaction of a reaction that Belle saw since its start | The person who reacted or removed the reaction | The message reacted to |
| `member` | `m.room.member`, when the membership changes. A change of the display name or the avatar is no event. | The sender: the person who joined or left, or who invited, removed or banned the member. The record names the member. | None |
| `pin` | `m.room.pinned_events`. Each message added to or removed from the list is one event. | The person who pinned | The message pinned or unpinned |

What the bot did itself is no event. The reaction of another bot, a sender in
`bot_user_ids` or another account of `accounts`, starts no turn. ⏹️ is no
reaction event: from a person, it asks for a stop. From a bot, it is that
bot's stop mark and stops nothing.

## Files of a message

The file of a message reaches the turn that the message starts, as content
that the backend gives the model. The connector reads it when the turn
starts, not when the message arrives. The helper downloads the file and, in
an encrypted room, decrypts it and checks its hash. The plain bytes stay in
Belle's memory: the helper keeps no copy.

The bot reads at most `platforms.matrix.file_download_max_bytes` of one file
(30 MiB by default). The server's upload limit (`m.upload.size`) applies only
to new uploads. The helper reads that limit at start.
For a file that it does not read, the connector posts a notice in the
message's thread with the reason. A file over the limit gets a notice that
names the limit, and the turn starts without the file.

## Second brain

The second brain ingests the files and links of a message in each room that
`second_brain.ingest.channels` selects:

| Entry | Selects on Matrix |
| --- | --- |
| `public` | Each room whose join rule is `public` and that is not encrypted |
| `*` | Each room that is not encrypted. The list's other entries select nothing. |
| A room ID, in a list with no `*` | That room, encrypted or not |

An encrypted room has the expectation that its server cannot read it. The
corpus keeps the text of each document in plain text on Belle's disk, and
recall adds it to a turn in any conversation. Thus only the room's own ID selects an
encrypted room. Add it only where the room's members accept that. A direct
room is never ingested.

The ingest marks its message with reactions: `brain_react_pending` while it
runs, then `brain_react_success`, `brain_react_failure` or
`brain_react_declined`. A document is identified by the SHA-256 of its
bytes. A second share of the same document is not ingested again, and its
message gets `brain_react_success`. Each Matrix event is ingested once: the
host keeps the IDs of the events that it ingested.

Recall works as on the other platforms. It has no audience check: a
document from a selected room can be shown to each person who talks to
Belle, in any room. See [Second Brain](../second-brain.md#privacy-and-audience).

### Who reads what

- A post, an edit or a file in another room that reaches people who cannot
  read the turn's room needs the requester's agreement, asked as a poll.
  A run with no requester cannot ask, and the tool refuses.
- `read_conversation` reads another room only where everybody who reads the
  turn's room, and the requester, can read that room too. A room whose join
  rule is `public` is readable by anybody.
- `download_file` reads a file of another room only where
  `read_conversation` may read that room.
- `read_matrix_pinned_messages` reads the pins of another room only where
  `read_conversation` may read that room. `search_messages` withholds a
  match in a room that `read_conversation` may not read.
- The content tools `summarize`, `extract` and `classify` take the ID of a
  file message of the turn's room as `attachment_ref`, and read the file by
  the rule of `download_file`. See [Content tools](../tools.md).

## Operator commands

`python -m belle_agent.platforms.matrix` runs one command on the bot's
store, with Belle's configuration and secrets. Where the section lists
`accounts`, `--account <name>` before the command names the account, and the
command reads the variables that its entry names. Stop Belle first: the helper
refuses a store that another process uses. Run the command in a container
of the launcher image, with Belle's env file and volumes:

```sh
podman stop belle
podman run --rm --network belle-net --env-file belle.env \
  -v belle-state:/home/belle/.local/state/belle-agent \
  -v belle-data:/home/belle/.local/share/belle-agent \
  localhost/belle:launcher-local python -m belle_agent.platforms.matrix devices
podman start belle
```

| Command | What it does |
| --- | --- |
| `devices` | Lists the account's devices: the ID, the time the server last saw it, the name, and `this store` for Belle's own device |
| `delete-devices <device ID> ...` | Removes devices of the account, such as the old devices of a lost store. It needs `MATRIX_PASSWORD`. It refuses Belle's own device. |
| `reset-identity --yes` | Makes a new identity and a new recovery key, and removes the key backup. It needs `MATRIX_PASSWORD` and a new file in `recovery_key_output`. See [Lost recovery key](#lost-recovery-key). |

After a reset, keep the new key as `MATRIX_RECOVERY_KEY` or
`MATRIX_RECOVERY_KEY_FILE`. The device of the command's store becomes
Belle's device. Each other device of the bot recovers only with the new
key.

A reset after a lost key, step by step, in bash and in fish:

```sh
podman rm belle
podman run --rm --network belle-net --env-file belle.env \
  -e MATRIX_RECOVERY_KEY_OUTPUT=/home/belle/.local/share/belle-agent/matrix-recovery-new.key \
  -v belle-state:/home/belle/.local/state/belle-agent \
  -v belle-data:/home/belle/.local/share/belle-agent \
  localhost/belle:launcher-local python -m belle_agent.platforms.matrix reset-identity --yes
podman create --name belle-key -v belle-data:/home/belle/.local/share/belle-agent localhost/belle:launcher-local
podman cp belle-key:/home/belle/.local/share/belle-agent/matrix-recovery-new.key matrix-recovery.key
podman rm belle-key
```

Then replace the `MATRIX_RECOVERY_KEY` line of `belle.env` with the new
key, remove the file from the data volume as in the quick start, and start
Belle.

## Failures and their fixes

| The log says | Cause | Fix |
| --- | --- | --- |
| `a new identity was made` | The first start of the account | Keep the key, as the [quick start](#quick-start-with-containers) says |
| `this new device has no recovery key` | A new or lost store, and no key | Set `MATRIX_RECOVERY_KEY` or `MATRIX_RECOVERY_KEY_FILE`. A lost key: [Lost recovery key](#lost-recovery-key). |
| `The MAC check for the secret storage key failed` | A recovery key of an older identity | Set the current key |
| `cannot open the store` | A wrong `MATRIX_STORE_PASSPHRASE` | Set the passphrase of this store |
| `this device is not cross-signed` | The identity was reset from another device | Set the new recovery key |
| `another helper process uses this store` | Belle runs, or an operator command does | Stop the other process |
| `the server refused a one-time key upload as a duplicate` | The store is an old copy | Remove the store's volume, start as a new device with the key, remove the old device |
| `matrix stays down: the helper exited 5 times within 300 s` | The helper exits at once, such as on a missing library | Read the helper's last lines in the log, fix the cause, restart Belle |
| `platforms.matrix.helper_path names ..., which is not an executable on PATH` | No helper | Use the launcher image, or install the helper |
| `the Matrix homeserver did not answer` | The homeserver is down or unreachable, or answered with a server error or a rate limit | Check that the homeserver runs and that Belle reaches its address. With `accounts`, the account connects by itself once it answers |

A refused start stops Belle with `belle cannot start: matrix cannot start:`
and the reason. A store that the platform finds to be an old copy, or a
helper that stays down, stops Matrix only: the process keeps running for
its other platforms. When no platform is left, Belle logs
`belle stops: every platform has stopped for good`, with each platform's
reason, and exits with 1. A container restart policy or a monitor then
sees the stop. A restart on the same store that is an old copy stops again,
so remove the store first, as the table says.

## Update the helper

The helper's crates have exact versions in `Cargo.lock`, and
`rust-toolchain.toml` names the toolchain. The launcher image builds the
helper with `cargo build --locked`, so a build always uses these versions.
To update matrix-sdk, change its version in `Cargo.toml` and
`Cargo.lock` together in one commit, and run the Matrix tests and the rig
checks on it. Before each release of the image, check the crates against
the RustSec advisories, for example with `cargo audit` in the toolchain
container. matrix-sdk-crypto has had advisories, and it has no public
independent audit. vodozemac, its encryption core, had one in 2022.

## Resource discovery

`resolve_matrix_alias` checks a full `#name:server` alias without joining its
room. Its `joined` field describes Belle's membership; the alias lookup alone
does not grant read access. `list_matrix_space` reads a page of a joined
space's hierarchy and returns `coverage.next_token` for the next page. It
hides private children whose audience Belle cannot verify. `read_matrix_state`
reads one current state event from this room or another room that passes the
same audience check as `read_conversation`.

Matrix pin changes are read, modify, write operations on one state event.
The Matrix state API has no compare-and-swap token, so concurrent pin changes
can overwrite each other. General state editing remains outside these tools.

## What Matrix does not offer

- No buttons. Questions are polls.
- No message that only one person of a room sees. A post with
  `ephemeral_to` is refused. The rejected reaction is the only sign of a
  refused message.
- No home tab, and no visit event.
- No search of an encrypted room by the server. The bot reads an encrypted
  room with `read_conversation`.
- No interactive verification of the bot (emoji check). A person who sends
  keys only to verified devices cannot write to the bot.

## Limits

- The bot cannot read history from before it joined an encrypted room.
  `read_conversation` lists those messages as undecryptable.
- Tuwunel 1.9.4 does not bundle the latest edit with its message. A thread
  read on Tuwunel gives the first text of an edited reply.
- Synapse with SQLite matches whole words only, as the
  [search](#search-names-and-pins) section says.
- Element counts the vote of a person who may not answer in a poll's
  result, though the poll's answer ignores it.
- The question of a poll is plain text: Markdown keeps its marks.
- A reaction that a person removes is an event only when Belle saw the
  reaction since its start.
