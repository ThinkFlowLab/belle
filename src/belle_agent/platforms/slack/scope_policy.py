"""Map Slack methods, events, and commands to scopes and manifest tiers.

The tables define required grants and event subscriptions. The core, connector,
and tools tiers add scope-based capabilities. The gated tier also enables the
agent view feature, which is a manifest setting rather than a token scope.
Slack Lists and canvases can require workspace access beyond token scopes; their
callers handle those refusals. See :data:`AGENT_VIEW_TIER` for the agent-view
requirement.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import chain
from types import MappingProxyType
from typing import Any

TIER_CORE = 0
TIER_CONNECTOR = 1
TIER_TOOLS = 2
TIER_GATED = 3

TIERS: tuple[int, ...] = (TIER_CORE, TIER_CONNECTOR, TIER_TOOLS, TIER_GATED)

#: The word each tier is named by in a filename and in the guides. Short,
#: because it is part of a path an operator types.
TIER_NAMES: Mapping[int, str] = MappingProxyType(
    {
        TIER_CORE: "core",
        TIER_CONNECTOR: "connector",
        TIER_TOOLS: "tools",
        # Named for what gates it rather than for what it holds, because the
        # gate is what an operator has to check before pasting the file.
        #
        # "gated" and not "paid": no Slack page states a plan requirement for
        # this family. What is published is the refusal --
        # agents.sessions.setStatus answers feature_disabled, "the agent tasks
        # feature is not enabled for this workspace" -- so the tier is named for
        # the observable fact rather than the suspected cause.
        TIER_GATED: "gated",
    }
)

TIER_SUMMARIES: Mapping[int, str] = MappingProxyType(
    {
        TIER_CORE: (
            "The smallest app that is a bot at all: it receives a mention or a"
            " direct message and answers it."
        ),
        # The one summary that names the command, and it is written with a
        # ``{command}`` field rather than the name: ``render_manifest`` fills it
        # in with the name that manifest declares, so a workspace that renamed
        # the command reads its own name here and in the feature block below.
        TIER_CONNECTOR: (
            "Core, plus the grants the connector spends on its own behalf on a"
            " default configuration: the acknowledgement reaction, files in"
            " both directions, and the {command} slash command. This is the"
            " tier to install unless there is a reason not to."
        ),
        TIER_TOOLS: (
            "Everything a scope can ask for, including the grants behind"
            " features that ship off. Install this to decide feature by feature"
            " in config rather than by reinstalling the app. This rung and the"
            " ones below it are accepted by any workspace. Some of what they"
            " reach is gated by Slack anyway, Lists and canvases among it, and"
            " the tool that calls it says so when it is refused."
        ),
        # The rung whose summary has to say what the others never needed to:
        # that pasting it may fail. It also names the one thing in any of these
        # files that changes what the workspace sees rather than what the app
        # may do.
        TIER_GATED: (
            "Tools, plus Slack's agent surface: the features.agent_view block"
            " and the assistant:write scope, which together let the app call"
            " agents.sessions.setStatus for the thread status line. This is the"
            " one rung Slack may refuse to save. The workspace has to have the"
            " agent feature, which no scope in this file asks for and none"
            " grants. Read the agent_view block before pasting: with it on,"
            " Slack draws this app's conversations as a timeline above the"
            " composer in the Messages tab, and no key in this repository's"
            " configuration takes that back. The tools manifest draws the same"
            " status line through the older thread-status family, but without"
            " the agent surface Slack's own stop button on that line does not"
            " work: Slack offers it only to an app declared as an agent. The"
            " Stop button on the activity card works on every tier."
        ),
    }
)

#: The lowest tier whose manifest declares ``features.agent_view``.
#:
#: The one entry in this module that is not a permission. Every other line here
#: answers "what may this app do". ``agent_view`` answers "what does this app
#: look like": with it on, Slack groups the app's conversations into threads and
#: draws them in a timeline above the composer in the Messages tab. Nothing in
#: this repository's configuration undoes that, so an operator who wants it back
#: edits the app in Slack.
#:
#: Two separate reasons put it on the top rung, and either alone would suffice: a
#: manifest asking for it can be refused at the moment it is saved, where every
#: rung below installs and then degrades per call; and the presentation change is
#: one an operator has to agree to rather than receive.
AGENT_VIEW_TIER = TIER_GATED

#: What Slack accepts in ``features.agent_view.agent_description``. The field is
#: required once the block is present, and Slack refuses a manifest whose value
#: is longer. Checked at render time rather than trusted, for the reason
#: :data:`_COMMAND_NAME` is checked: the failure is an operator pasting a file
#: and being told no, with nothing in this repository having warned them.
AGENT_DESCRIPTION_MAX = 300

#: The four conversation kinds Slack scopes apart. ``event_chat_type`` in the
#: connector reads ``channel_type`` and passes Slack's own word for each --
#: channel, group, im, mpim -- straight to the scope matcher, so the connector
#: declares all four and a call that reads a conversation may be reading any of
#: them.
_HISTORY_SCOPES = ("channels:history", "groups:history", "im:history", "mpim:history")
_CONVERSATION_READ_SCOPES = ("channels:read", "groups:read", "im:read", "mpim:read")

#: One method, six grants. ``assistant.search.context`` serves four channel
#: kinds and two extra content types, and each is granted separately.
_SEARCH_SCOPES = (
    "search:read.public",
    "search:read.private",
    "search:read.im",
    "search:read.mpim",
    "search:read.files",
    "search:read.users",
)


@dataclass(frozen=True)
class Requirement:
    """What one call or one event needs, and which tier it belongs to."""

    #: The scopes, in the order the manifest should list them. Empty is a
    #: claim and not a gap: it says the call needs nothing beyond holding a bot
    #: token at all.
    scopes: tuple[str, ...]
    tier: int
    #: One clause, rendered into the manifest beside the scopes it justifies.
    why: str


#: Every Slack API method this repository calls.
#:
#: Keyed in the SDK's underscore spelling, which is also what a dotted name
#: normalises to: ``reactions.add`` and ``reactions_add`` are one entry, because
#: this repository writes a method both ways -- the SDK spelling where it calls
#: a wrapper, the dotted spelling where it goes through ``api_call`` because the
#: pinned SDK has no wrapper for the method.
METHOD_REQUIREMENTS: Mapping[str, Requirement] = MappingProxyType(
    {
        # ── core ────────────────────────────────────────────────────────────
        # Called once at startup to learn the bot's own user id and the
        # workspace's host. Needs no scope; a bot token alone answers it.
        "auth_test": Requirement((), TIER_CORE, "startup identity"),
        "chat_postMessage": Requirement(("chat:write",), TIER_CORE, "posting a reply"),
        "chat_update": Requirement(("chat:write",), TIER_CORE, "editing a reply"),
        "chat_delete": Requirement(("chat:write",), TIER_CORE, "withdrawing a message"),
        "chat_postEphemeral": Requirement(
            ("chat:write",), TIER_CORE, "a notice for one reader"
        ),
        "chat_startStream": Requirement(
            ("chat:write",), TIER_CORE, "opening a streamed reply"
        ),
        "chat_appendStream": Requirement(
            ("chat:write",), TIER_CORE, "extending a streamed reply"
        ),
        "chat_stopStream": Requirement(
            ("chat:write",), TIER_CORE, "closing a streamed reply"
        ),
        # ── connector, default on ───────────────────────────────────────────
        # The thread status line, in the older of the two families Slack offers
        # for it. Belle platforms.slack.session_surface picks the family, and this one
        # is the default. chat:write and nothing else, which is what the
        # method's own reference page lists.
        "assistant_threads_setStatus": Requirement(
            ("chat:write",), TIER_CONNECTOR, "the thread status line"
        ),
        # A linting endpoint for a payload the caller already holds. It reads
        # nothing belonging to the workspace and needs no scope.
        "blocks_validate": Requirement(
            (), TIER_CONNECTOR, "checking a Block Kit payload before sending it"
        ),
        "reactions_add": Requirement(
            ("reactions:write",), TIER_CONNECTOR, "the acknowledgement mark"
        ),
        "reactions_remove": Requirement(
            ("reactions:write",), TIER_CONNECTOR, "clearing the acknowledgement mark"
        ),
        "files_info": Requirement(
            ("files:read",), TIER_CONNECTOR, "resolving an inbound attachment"
        ),
        "files_upload_v2": Requirement(
            ("files:write",), TIER_CONNECTOR, "sending a file"
        ),
        # ── tools ───────────────────────────────────────────────────────────
        # Reached by the history toolkit, and by the group digital avatar,
        # which ships off. All four kinds, for the reason _HISTORY_SCOPES
        # gives.
        "conversations_history": Requirement(
            _HISTORY_SCOPES, TIER_TOOLS, "reading a conversation"
        ),
        "conversations_replies": Requirement(
            _HISTORY_SCOPES, TIER_TOOLS, "reading a thread"
        ),
        "chat_getPermalink": Requirement(
            (), TIER_TOOLS, "linking an ingested message"
        ),
        "conversations_info": Requirement(
            _CONVERSATION_READ_SCOPES, TIER_TOOLS, "telling one conversation kind from another"
        ),
        "conversations_members": Requirement(
            _CONVERSATION_READ_SCOPES, TIER_TOOLS, "listing who is in a conversation"
        ),
        # Called by the posting tools alone. A chat_id naming a person rather
        # than a room may have no conversation behind it yet, so one is opened
        # and then posted into; Slack answers with the existing D… where there
        # already is one, so a second call costs nothing and creates nothing.
        #
        # Both scopes, because one method covers both shapes: im:write opens a
        # direct message with one person, mpim:write opens one with several, and
        # the call is the same call. Granting only the first leaves a group
        # direct message refused by Slack with the scope named, which is the
        # degrade every other tool here gets.
        "conversations_open": Requirement(
            ("im:write", "mpim:write"), TIER_TOOLS, "opening a direct message"
        ),
        "pins_add": Requirement(("pins:write",), TIER_TOOLS, "pinning a message"),
        "pins_remove": Requirement(("pins:write",), TIER_TOOLS, "unpinning a message"),
        "pins_list": Requirement(("pins:read",), TIER_TOOLS, "reading what is pinned"),
        # Slack's own queue of messages to post later, which the scheduled-message tools manage. Slack documents
        # no scope for the listing: the bot token alone reads the messages it scheduled.
        "chat_scheduleMessage": Requirement(("chat:write",), TIER_TOOLS, "scheduling a message"),
        "chat_scheduledMessages_list": Requirement((), TIER_TOOLS, "listing scheduled messages"),
        "chat_deleteScheduledMessage": Requirement(("chat:write",), TIER_TOOLS, "cancelling a scheduled message"),
        # The listing half and the writing half are separate grants, and an
        # edit calls both: it reads the bookmark before writing it back, so the
        # fields the edit does not name keep the values they have.
        "bookmarks_add": Requirement(
            ("bookmarks:write",), TIER_TOOLS, "adding a bookmark"
        ),
        "bookmarks_edit": Requirement(
            ("bookmarks:write",), TIER_TOOLS, "editing a bookmark"
        ),
        "bookmarks_remove": Requirement(
            ("bookmarks:write",), TIER_TOOLS, "removing a bookmark"
        ),
        "bookmarks_list": Requirement(
            ("bookmarks:read",), TIER_TOOLS, "reading the bookmark bar"
        ),
        # Two uses. The history toolkit falls back to raw user ids without it
        # and says so in its own warnings. A reaction, pin or member event names
        # no bot flag, and one whose actor this cannot tell is dropped; every
        # tier that subscribes to those events grants it. The audience rule asks
        # nothing of the directory.
        "users_info": Requirement(
            ("users:read",),
            TIER_TOOLS,
            "turning user ids into names in a history answer; telling an app's"
            " event from a person's",
        ),
        # The four listing methods behind find_by_name, which runs the other
        # way round from users.info: a name in, an id out. Four methods and
        # four grants rather than one, because a workspace may well grant the
        # people directory and not the emoji list; the tool reports a refused
        # kind in its coverage block and searches the rest.
        "users_list": Requirement(
            ("users:read",), TIER_TOOLS, "finding a person by name"
        ),
        # channels:read and groups:read only, and not the other two members of
        # _CONVERSATION_READ_SCOPES. A direct message has no name, so it can
        # never answer a name query, and asking for im:read and mpim:read here
        # would widen the install for a page of results that cannot hold an
        # answer.
        "conversations_list": Requirement(
            ("channels:read", "groups:read"),
            TIER_TOOLS,
            "finding a conversation by name",
        ),
        "usergroups_list": Requirement(
            ("usergroups:read",), TIER_TOOLS, "finding a user group by name"
        ),
        # Without it nothing tells a model which custom emoji this workspace
        # has, and react_to_message is left guessing a shortcode and failing.
        "emoji_list": Requirement(
            ("emoji:read",), TIER_TOOLS, "naming this workspace's custom emoji"
        ),
        "assistant_search_context": Requirement(
            _SEARCH_SCOPES, TIER_TOOLS, "searching the workspace"
        ),
        # The App Home tab, which is a page of Block Kit this app publishes into
        # a private space it shares with one person. Needs no scope, which is
        # unusual: what gates it is features.app_home.home_tab_enabled, the
        # app's own configuration rather than a grant a workspace makes, and
        # Slack refuses the call with ``not_enabled`` while that flag is off. So
        # the entry puts no scope into any manifest; it exists because every
        # Slack call this repository makes has to be classified, and "no scope"
        # is a classification.
        "views_publish": Requirement(
            (), TIER_TOOLS, "publishing somebody's App Home tab"
        ),
        # The canvas toolkit. Two scopes and eight calls: canvases:read reads a
        # canvas's content and lists its sections. Everything else about a
        # canvas -- creating it, changing it, sharing it, deleting it -- is
        # canvases:write. The read tool also calls files.info, which
        # files:read already carries at the connector tier.
        "canvases_getContent": Requirement(
            ("canvases:read",), TIER_TOOLS, "reading what a canvas says"
        ),
        "canvases_sections_lookup": Requirement(
            ("canvases:read",), TIER_TOOLS, "listing a canvas's sections"
        ),
        "canvases_create": Requirement(
            ("canvases:write",), TIER_TOOLS, "creating a canvas"
        ),
        "conversations_canvases_create": Requirement(
            ("canvases:write",),
            TIER_TOOLS,
            "creating a conversation's own canvas",
        ),
        "canvases_edit": Requirement(
            ("canvases:write",), TIER_TOOLS, "changing what a canvas says"
        ),
        "canvases_access_set": Requirement(
            ("canvases:write",),
            TIER_TOOLS,
            "setting who may read or write a canvas",
        ),
        "canvases_access_delete": Requirement(
            ("canvases:write",), TIER_TOOLS, "taking canvas access away"
        ),
        "canvases_delete": Requirement(
            ("canvases:write",), TIER_TOOLS, "deleting a canvas"
        ),
        # The List toolkit. Two scopes and twelve calls, split the ordinary
        # read/write way -- reading rows and starting an export are lists:read,
        # everything that changes a List is lists:write.
        #
        # Deleting one is neither, and that is the entry below this block.
        # Slack has no method that deletes a List, so the tool goes through
        # files.delete, and what it spends is the files grant rather than the
        # Lists grant. An install can therefore hold both List scopes and still
        # be unable to delete a List, and hold neither and still be able to --
        # which is why the delete tool's own guard reads the file first rather
        # than trusting that holding a List scope means the id is a List's.
        "slackLists_items_list": Requirement(
            ("lists:read",), TIER_TOOLS, "reading a List's rows"
        ),
        "slackLists_items_info": Requirement(
            ("lists:read",), TIER_TOOLS, "reading one row, and the List's schema"
        ),
        "slackLists_download_start": Requirement(
            ("lists:read",), TIER_TOOLS, "starting an export of a List"
        ),
        "slackLists_download_get": Requirement(
            ("lists:read",), TIER_TOOLS, "collecting an export of a List"
        ),
        "slackLists_create": Requirement(
            ("lists:write",), TIER_TOOLS, "creating a List"
        ),
        "slackLists_update": Requirement(
            ("lists:write",), TIER_TOOLS, "renaming a List"
        ),
        "slackLists_items_create": Requirement(
            ("lists:write",), TIER_TOOLS, "adding a row to a List"
        ),
        "slackLists_items_update": Requirement(
            ("lists:write",), TIER_TOOLS, "writing cells in a List"
        ),
        "slackLists_items_delete": Requirement(
            ("lists:write",), TIER_TOOLS, "deleting one row of a List"
        ),
        "slackLists_items_deleteMultiple": Requirement(
            ("lists:write",), TIER_TOOLS, "deleting several rows of a List"
        ),
        "slackLists_access_set": Requirement(
            ("lists:write",), TIER_TOOLS, "setting who may read or write a List"
        ),
        "slackLists_access_delete": Requirement(
            ("lists:write",), TIER_TOOLS, "taking List access away"
        ),
        # The only call in this repository that destroys a file, and the reason
        # delete_slack_list reads before it writes: this method deletes any
        # file it is handed. files:write is already granted at the connector
        # tier for uploads, so classifying it here widens no install; what it
        # does is make the call visible to the scan, which is the point.
        "files_delete": Requirement(
            ("files:write",), TIER_TOOLS, "deleting a List, which is a file"
        ),
        # ── gated ───────────────────────────────────────────────────────────
        # The newer of the two thread-status families, and the only entry in
        # this table on the rung a grant does not buy.
        #
        # Slack's pages disagree: the method's own reference lists chat:write,
        # while the guide to developing agents asks for an agent app --
        # features.agent_view switched on and assistant:write granted. A live app
        # refused the change and named agent_view, so this entry states the
        # stricter reading, with AGENT_VIEW_TIER putting the feature block in the
        # same manifest.
        #
        # The tier decides what an operator is handed and nothing else. An
        # install below it that asks for the agent family gets a refusal from
        # Slack, which the connector reads, remembers and logs before using
        # assistant.threads.setStatus for the rest of the process. So a manifest
        # without the surface costs one refused call and one line in the log,
        # and the older family draws the same status line.
        "agents_sessions_setStatus": Requirement(
            ("chat:write", "assistant:write"),
            TIER_GATED,
            "the thread status line, newer API",
        ),
    }
)

#: Scopes one optional argument of an already-classified method asks for.
#:
#: Keyed by the method and the argument, because the method is classified above
#: and this is a second, narrower claim about it: ``chat.postMessage`` posts on
#: ``chat:write`` at tier core, and one argument on it costs a grant of its own.
#: Folding that scope into the method's entry would buy it for the smallest
#: install there is, which mounts no tool that can pass the argument.
#:
#: The tier is therefore chosen here rather than inherited. An install below it
#: holds ``chat:write`` and posts; Slack answers ``missing_scope`` to the
#: argument, and the tool names the scope and posts nothing, which is the degrade
#: every other optional capability in this file gets.
METHOD_OPTION_REQUIREMENTS: Mapping[tuple[str, str], Requirement] = MappingProxyType(
    {
        # Tier tools, and each of the other three rungs is wrong for its own
        # reason. Core is a bot that answers a mention and mounts no posting
        # tool, so the grant would let a minimal install wear somebody's face and
        # buy nothing. The connector spends no identity override on its own
        # behalf: no reply, status line, reaction or upload sets one. Gated is for
        # what a grant cannot buy, and this is a scope any workspace can grant.
        # That leaves tier tools, which means a model-facing capability decided
        # in config rather than by reinstalling the app.
        ("chat_postMessage", "as_requester"): Requirement(
            ("chat:write.customize",),
            TIER_TOOLS,
            "posting under the asker's name and face",
        ),
    }
)


#: Every event subscription the connector has a listener for.
#:
#: These are the requirements nothing in the source records, because what needs
#: them is Slack's willingness to deliver rather than a call. An absent one is
#: the quietest failure in the system: no call fails, nothing is logged, and the
#: bot is simply mute in the conversations that event would have come from.
EVENT_REQUIREMENTS: Mapping[str, Requirement] = MappingProxyType(
    {
        "app_mention": Requirement(
            ("app_mentions:read",), TIER_CORE, "being addressed in a channel"
        ),
        # Bolt registers one ``message`` listener and the connector routes on
        # ``channel_type``, so which of the four arrive is decided entirely by
        # the subscription list below.
        "message.channels": Requirement(
            ("channels:history",), TIER_CORE, "a message in a public channel"
        ),
        "message.im": Requirement(("im:history",), TIER_CORE, "a direct message"),
        "message.groups": Requirement(
            ("groups:history",), TIER_CORE, "a message in a private channel"
        ),
        "message.mpim": Requirement(
            ("mpim:history",), TIER_CORE, "a message in a group direct message"
        ),
        # Non-message events are opt-in regardless of installed scopes.
        "reaction_added": Requirement(
            ("reactions:read",), TIER_TOOLS, "somebody reacted"
        ),
        "reaction_removed": Requirement(
            ("reactions:read",), TIER_TOOLS, "somebody took a reaction off"
        ),
        "pin_added": Requirement(("pins:read",), TIER_TOOLS, "somebody pinned"),
        "pin_removed": Requirement(("pins:read",), TIER_TOOLS, "somebody unpinned"),
        # These events need read access to public and private channel membership.
        "member_joined_channel": Requirement(
            ("channels:read", "groups:read"), TIER_TOOLS, "somebody joined"
        ),
        "member_left_channel": Requirement(
            ("channels:read", "groups:read"), TIER_TOOLS, "somebody left"
        ),
        # Slack delivers this event to any app with a bot user. The tools tier
        # enables its event family; lower tiers ship that family as off.
        "app_home_opened": Requirement(
            (),
            TIER_TOOLS,
            "somebody opened this app's App Home, which Slack delivers to any"
            " app that has a bot user",
        ),
        # This inbound half of the thread status feature uses chat:write, which
        # core already grants. Keep it in connector, alongside status updates:
        # Slack sends it only to an app with the agent surface, and an operator
        # who adds features.agent_view to a lower tier then needs no other
        # change.
        "agent_session_stopped": Requirement(
            (),
            TIER_CONNECTOR,
            "somebody pressed Slack's own stop button, which Slack offers only"
            " to an app with the agent surface and delivers on the chat:write"
            " this app already holds for posting a reply",
        ),
        # Declare only events with a listener. This connector does not handle
        # app_context_changed or agent_session_title_changed.
    }
)

#: The one slash command this app registers, as :data:`COMMAND_REQUIREMENTS`
#: keys it. An identity and not a name: what a person types is
#: read from Belle ``slack.command_name`` and passed to each function below.
#:
#: Its subcommands are the connector's own business and never reach a manifest,
#: so adding one does not send an operator back to Slack.
MAIN_COMMAND = "main"

#: Slash command name when Belle ``slack.command_name`` is empty.
DEFAULT_COMMAND_NAME = "belle"

#: Slack bot name when ``slack.bot_name`` is unset or empty.
DEFAULT_BOT_NAME = "Belle"


def resolve_bot_name(raw: Any) -> str:
    """Use the default display name when the configured name is empty."""
    return str(raw or "").strip() or DEFAULT_BOT_NAME

#: Slack command names allow 1–32 lowercase letters, digits, hyphens, or underscores.
_COMMAND_NAME = re.compile(r"[a-z0-9_-]{1,32}\Z")


def normalise_command_name(raw: Any) -> str:
    """Normalize a Slack command name; reject YAML booleans and invalid names."""
    if isinstance(raw, bool):
        return ""
    text = str(raw or "").strip()
    text = text.removeprefix("/")
    text = text.lower()
    return text if _COMMAND_NAME.match(text) else ""


def command_token(command_name: str = DEFAULT_COMMAND_NAME) -> str:
    """Prefix a configured command name with Slack's slash."""
    return f"/{command_name}"


def command_names(command_name: str = DEFAULT_COMMAND_NAME) -> Mapping[str, str]:
    """Map each command to the name Slack delivers."""
    return {MAIN_COMMAND: command_token(command_name)}


@dataclass(frozen=True)
class SlashCommand:
    """One ``features.slash_commands`` entry, and what installing it costs."""

    requirement: Requirement
    #: What Slack's command picker shows under the name. Written with a
    #: ``{bot}`` field, filled in by ``render_manifest`` with the name that
    #: manifest declares, for the reason ``TIER_SUMMARIES`` uses ``{command}``.
    description: str
    #: The argument placeholder the picker shows after the name.
    usage_hint: str


#: Every slash command the connector registers a listener for.
#:
#: A command is like an event in that Slack delivers it and no call in the
#: source names its scope. It is unlike one in where it lives: not a
#: subscription but a manifest feature of its own, rendered under
#: ``features.slash_commands`` in every tier that carries its scope.
COMMAND_REQUIREMENTS: Mapping[str, SlashCommand] = MappingProxyType(
    {
        # Connector and not core. A missing Tier 0 grant stops the workspace
        # from starting, and an app created before this command existed must
        # not stop over it: at tier 1 the absence is said once at startup, and
        # the app simply has no command.
        MAIN_COMMAND: SlashCommand(
            Requirement(
                ("commands",), TIER_CONNECTOR, "a slash command typed in Slack"
            ),
            description="Run a {bot} command",
            # Every subcommand the connector's registry holds, which a test
            # checks. Slack shows this in its command picker, so it is the only
            # place a person discovers them without typing help first.
            usage_hint=(
                "help | status | skills list | skills use <skill> <task> | "
                "subagents list | subagents use <name> <task> | "
                "queue list | queue drop [message link] | experts list | "
                "models list | models current | models use <name> | "
                "models reset | ask <question> | stop [message link] | "
                "schedule list | schedule show <job id> | "
                "schedule pause <job id> | schedule resume <job id> | "
                "schedule delete <job id> | schedule run <job id> | "
                "bg <task> | sessions list [cursor] | "
                "sessions show <session id> [cursor] | "
                "sessions result <session id> [run id] [cursor] | "
                "sessions continue <session id> <task> | "
                "sessions cancel <session id>"
            ),
        ),
    }
)

#: How a method is spelled when it is not the SDK's own key with its first
#: underscore turned into a dot. Only the methods the pinned SDK has no wrapper
#: for need an entry: they are called through ``api_call`` with the dotted name
#: written out, and that is the name an operator looks up.
DOTTED_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "assistant_threads_setStatus": "assistant.threads.setStatus",
        "agents_sessions_setStatus": "agents.sessions.setStatus",
        "assistant_search_context": "assistant.search.context",
        # Three of the canvas methods are two dots deep and a fourth is a
        # method on a nested family, so the first-underscore rule would spell
        # them canvases.access_set, canvases.sections_lookup and
        # conversations.canvases_create -- none of which an operator can look
        # up. They are written out for the same reason the two above are.
        "canvases_access_set": "canvases.access.set",
        "canvases_access_delete": "canvases.access.delete",
        "canvases_sections_lookup": "canvases.sections.lookup",
        "conversations_canvases_create": "conversations.canvases.create",
        # Keep Slack's documented method names, including the capital L in
        # ``slackLists`` and nested item methods.
        "slackLists_create": "slackLists.create",
        "slackLists_update": "slackLists.update",
        "slackLists_access_set": "slackLists.access.set",
        "slackLists_access_delete": "slackLists.access.delete",
        "slackLists_items_create": "slackLists.items.create",
        "slackLists_items_info": "slackLists.items.info",
        "slackLists_items_list": "slackLists.items.list",
        "slackLists_items_update": "slackLists.items.update",
        "slackLists_items_delete": "slackLists.items.delete",
        "slackLists_items_deleteMultiple": "slackLists.items.deleteMultiple",
        "slackLists_download_start": "slackLists.download.start",
        "slackLists_download_get": "slackLists.download.get",
    }
)


def dotted(method: str) -> str:
    """One method as Slack's own documentation spells it."""
    return DOTTED_NAMES.get(method) or method.replace("_", ".", 1)


#: The subscriptions one bolt ``message`` listener stands for.
MESSAGE_SUBSCRIPTIONS: tuple[str, ...] = (
    "message.channels",
    "message.groups",
    "message.im",
    "message.mpim",
)

#: Scopes an operator may delete from the tier that carries them, with the
#: single sentence saying what deleting one costs. Rendered into the manifest
#: beside the scope, so the narrowing is offered where it is acted on rather
#: than only in a guide.
NARROWINGS: Mapping[str, str] = MappingProxyType(
    {
        "groups:history": (
            "delete this and message.groups to keep the bot out of private"
            " channels"
        ),
        "mpim:history": (
            "delete this and message.mpim to keep the bot out of group DMs"
        ),
        "search:read.files": (
            "if Slack refuses this manifest over the six search scopes, delete"
            " all six and set Belle platforms.slack.search_enabled to false"
        ),
        "chat:write.customize": (
            "delete this to hold every message to this app's own name and"
            " picture; post_message as_requester is then refused, naming the"
            " scope, and everything else posts as before"
        ),
    }
)


def every_requirement() -> tuple[Requirement, ...]:
    """Every grant any table in this module asks for.

    One reading of the four tables, so that a scope added to any of them reaches
    the manifests and the startup check without a fifth place to remember.
    """
    return (
        *METHOD_REQUIREMENTS.values(),
        *METHOD_OPTION_REQUIREMENTS.values(),
        *EVENT_REQUIREMENTS.values(),
        *(command.requirement for command in COMMAND_REQUIREMENTS.values()),
    )


def scope_tiers() -> dict[str, int]:
    """Every scope, mapped to the lowest tier that needs it."""
    tiers: dict[str, int] = {}
    for requirement in every_requirement():
        for scope in requirement.scopes:
            if scope not in tiers or requirement.tier < tiers[scope]:
                tiers[scope] = requirement.tier
    return tiers


def scope_reasons(
    tier: int = TIER_GATED, command_name: str = DEFAULT_COMMAND_NAME
) -> dict[str, list[str]]:
    """List each granted scope's callers in table order up to ``tier``."""
    names = command_names(command_name)
    entries = chain(
        ((dotted(name), requirement) for name, requirement in METHOD_REQUIREMENTS.items()),
        (
            (f"{dotted(name)} {option}", requirement)
            for (name, option), requirement in METHOD_OPTION_REQUIREMENTS.items()
        ),
        EVENT_REQUIREMENTS.items(),
        (
            (names[command_id], command.requirement)
            for command_id, command in COMMAND_REQUIREMENTS.items()
        ),
    )
    reasons: dict[str, list[str]] = {}
    for name, requirement in entries:
        if requirement.tier <= tier:
            for scope in requirement.scopes:
                reasons.setdefault(scope, []).append(f"{name} ({requirement.why})")
    return reasons


def scopes_for_tier(tier: int) -> list[str]:
    """The bot scopes an install at ``tier`` asks for, ordered by tier then name."""
    tiers = scope_tiers()
    chosen = [scope for scope, own in tiers.items() if own <= tier]
    return sorted(chosen, key=lambda scope: (tiers[scope], scope))


GRANTED_SCOPES_HEADER = "x-oauth-scopes"


def slack_granted_scopes(headers: Any) -> frozenset[str] | None:
    """Read Slack's granted scopes, or None if the header is absent or blank."""
    if headers is None or not callable(getattr(headers, "items", None)):
        return None
    for key, value in headers.items():
        if str(key).strip().lower() == GRANTED_SCOPES_HEADER:
            if value is None:
                return None
            granted = {scope.strip() for scope in str(value).split(",")}
            granted.discard("")
            return frozenset(granted) if granted else None
    return None


def slack_required_scopes() -> frozenset[str]:
    """Require core grants except those the manifest permits operators to omit."""
    return frozenset(scopes_for_tier(TIER_CORE)) - frozenset(NARROWINGS)


def events_for_tier(tier: int) -> list[str]:
    """The event subscriptions an install at ``tier`` asks for."""
    chosen = [
        name
        for name, requirement in EVENT_REQUIREMENTS.items()
        if requirement.tier <= tier
    ]
    return sorted(chosen, key=lambda name: (EVENT_REQUIREMENTS[name].tier, name))


def commands_for_tier(
    tier: int, command_name: str = DEFAULT_COMMAND_NAME
) -> list[str]:
    """The slash commands an install at ``tier`` registers, as Slack spells them.

    The names and not the table's keys: a key identifies a command across a
    rename, and what a manifest declares and a listener registers under is the
    name the workspace actually types.
    """
    names = command_names(command_name)
    return sorted(
        names[command_id]
        for command_id, command in COMMAND_REQUIREMENTS.items()
        if command.requirement.tier <= tier
    )


def manifest_filename(tier: int) -> str:
    """The name the manifest for ``tier`` is shipped under."""
    return f"slack-app-manifest-{tier}-{TIER_NAMES[tier]}.yaml"


def _wrap(text: str, width: int, indent: str, cont: "str | None" = None) -> list[str]:
    """Comment lines for one sentence, wrapped without a dependency.

    ``cont`` is what a continuation line starts with. It defaults to ``indent``,
    which is right for a plain comment and wrong for one that opens with a label
    -- repeating "optional:" on every line of a two-line note would read as two
    notes.
    """
    cont = indent if cont is None else cont
    words = text.split()
    lines: list[str] = []
    current = ""
    start = indent
    for word in words:
        candidate = f"{current} {word}" if current else f"{start}{word}"
        if len(candidate) > width and current:
            lines.append(current)
            start = cont
            current = f"{cont}{word}"
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def agent_view_for_tier(tier: int) -> bool:
    """Whether the manifest for ``tier`` declares ``features.agent_view``."""
    return tier >= AGENT_VIEW_TIER


def agent_description(bot_name: str = DEFAULT_BOT_NAME) -> str:
    """Render the agent-view description; reject names that exceed Slack's limit."""
    text = (
        f"{bot_name} answers questions and runs tasks in this workspace."
        " Mention it in a channel or send it a direct message and it replies in"
        " a thread. It reads only what it has been given access to, and it asks"
        " before it does anything that needs approval."
    )
    if len(text) > AGENT_DESCRIPTION_MAX:
        raise ValueError(
            f"agent_description is {len(text)} characters with the bot name"
            f" {bot_name!r}, and Slack accepts at most"
            f" {AGENT_DESCRIPTION_MAX}. Shorten Belle platforms.slack.bot_name."
        )
    return text


def _description(bot_name: str) -> str:
    return f"{bot_name} agent runtime, connected over Socket Mode."


def _long_description(bot_name: str) -> str:
    return (
        f"{bot_name} connects an agent runtime to this workspace over Socket"
        " Mode, so no public callback URL is needed. It answers mentions and"
        " direct messages, streams long replies, and posts questions and"
        " approvals as buttons.\n\nIt is installed per workspace from a manifest"
        " and configured with a bot token and an app-level token pasted into the"
        " operator's own configuration file. It is not distributed, and it is not"
        " listed in the Slack Marketplace."
    )


def render_manifest(
    tier: int,
    command_name: str = DEFAULT_COMMAND_NAME,
    bot_name: str = DEFAULT_BOT_NAME,
) -> str:
    """Render a tier's manifest with the configured command and bot names."""
    if tier not in TIER_NAMES:
        raise ValueError(f"no such tier: {tier!r}")

    reasons = scope_reasons(tier, command_name)
    names = command_names(command_name)
    tiers = scope_tiers()
    out: list[str] = []

    out.append(f"# Slack app manifest for {bot_name} -- tier {tier}, {TIER_NAMES[tier]}.")
    out.append("#")
    out.extend(
        _wrap(
            TIER_SUMMARIES[tier].format(command=command_token(command_name)),
            79,
            "# ",
        )
    )
    out.append("#")
    out.extend(
        _wrap(
            "GENERATED FILE. Edit belle_agent.platforms.slack.scope_policy and run"
            " scripts/generate_slack_manifest.py. A test regenerates this file and"
            " fails on any difference, and a second test fails when the code calls"
            " a Slack method the policy module does not classify.",
            79,
            "# ",
        )
    )
    out.append("#")
    out.extend(
        _wrap(
            "TO INSTALL: https://api.slack.com/apps -> Create New App -> From a"
            " manifest, pick the workspace, paste this file. Two things no"
            " manifest field carries stay manual afterwards: an App-Level Token"
            " carrying connections:write, minted under Basic Information, and the"
            " install itself, which returns the bot token.",
            79,
            "# ",
        )
    )
    out.extend([
        "_metadata:",
        "  major_version: 1",
        "  minor_version: 1",
        "",
        "display_information:",
        f"  name: {bot_name}",
        f"  description: {_description(bot_name)}",
        '  background_color: "#1f2937"',
        "  long_description: |-",
    ])
    for paragraph in _long_description(bot_name).split("\n"):
        out.append(f"    {paragraph}" if paragraph else "")
    out.extend([
        "",
        "features:",
        "  bot_user:",
        f"    display_name: {bot_name}",
        "    always_online: true",
        "  app_home:",
        "    home_tab_enabled: true",
        "    messages_tab_enabled: true",
        "    messages_tab_read_only_enabled: false",
    ])
    if agent_view_for_tier(tier):
        out.append("  agent_view:")
        out.extend(
            _wrap(
                "The agent surface. With this block present Slack groups this"
                " app's conversations into threads and draws them in a timeline"
                " above the composer in the Messages tab, so the app looks"
                " different to everybody in the workspace. Deleting the block"
                " before pasting keeps the ordinary layout, and costs the"
                " agents.sessions.setStatus family: Slack refuses that family to"
                " an app without the feature, and the connector falls back to"
                " assistant.threads.setStatus and says so once in the log. It"
                " also costs Slack's own stop button on the status line, which"
                " Slack offers only to an app with the feature.",
                79,
                "    # ",
            )
        )
        out.append("    #")
        out.extend(
            _wrap(
                "assistant:write is the grant that goes with it and is listed"
                " below. Delete one and the other buys nothing.",
                79,
                "    # ",
            )
        )
        out.append(f"    agent_description: {agent_description(bot_name)}")
    commands = sorted(
        (
            (names[command_id], command)
            for command_id, command in COMMAND_REQUIREMENTS.items()
            if command.requirement.tier <= tier
        ),
        key=lambda entry: entry[0],
    )
    if commands:
        out.append("  slash_commands:")
        for name, command in commands:
            requirement = command.requirement
            out.append(f"    - command: {name}")
            out.extend(
                _wrap(
                    f"{requirement.why}; needs {', '.join(requirement.scopes)}."
                    " No url: Socket Mode delivers it over the WebSocket.",
                    79,
                    "      # ",
                )
            )
            out.extend([
                f"      description: {command.description.format(bot=bot_name)}",
                f"      usage_hint: {command.usage_hint}",
                "      should_escape: false",
            ])
    out.extend(["", "oauth_config:", "  scopes:", "    bot:"])
    current_tier = -1
    for scope in scopes_for_tier(tier):
        if tiers[scope] != current_tier:
            current_tier = tiers[scope]
            out.append(
                f"      # tier {current_tier}, {TIER_NAMES[current_tier]}"
            )
        out.append(f"      - {scope}")
        out.extend(_wrap("needed by " + "; ".join(reasons[scope]), 79, "        # "))
        narrowing = NARROWINGS.get(scope)
        if narrowing:
            out.extend(
                _wrap(narrowing, 79, "        # optional: ", "        #   ")
            )
    out.extend(["", "settings:", "  event_subscriptions:"])
    out.extend(
        _wrap(
            "No request_url: Socket Mode delivers these over the WebSocket the"
            " connector opens with the app-level token.",
            79,
            "    # ",
        )
    )
    out.append("    bot_events:")
    for event in events_for_tier(tier):
        requirement = EVENT_REQUIREMENTS[event]
        out.append(f"      - {event}")
        # An empty scope list is a claim rather than a gap -- see Requirement --
        # so it is rendered as one. "needs " with nothing after it would read as
        # a line the generator failed to finish, and an operator deciding what
        # to narrow needs to be told that this one costs no grant at all.
        #
        # This describes only the manifest scopes added by the requirement.
        # The event's ``why`` field explains any other Slack access it needs.
        needs = (
            f"needs {', '.join(requirement.scopes)}"
            if requirement.scopes
            else "needs no scope this manifest does not already grant"
        )
        out.extend(_wrap(f"{requirement.why}; {needs}", 79, "        # "))
    out.append("")
    out.append("  interactivity:")
    out.extend(
        _wrap(
            "The question, approval and stop buttons send block_actions payloads."
            " With Socket Mode on those arrive on the same WebSocket as the events"
            " above, so no request_url is needed. Turned off, the buttons render"
            " and every click is discarded.",
            79,
            "    # ",
        )
    )
    out.extend(["    is_enabled: true", "", "  socket_mode_enabled: true", ""])
    out.extend(
        _wrap(
            "Not distributed. This app is created per workspace from this manifest"
            " and installed by its own operator; it is not submitted to the Slack"
            " Marketplace and has no OAuth redirect flow.",
            79,
            "  # ",
        )
    )
    out.extend(["  org_deploy_enabled: false", "  token_rotation_enabled: false", ""])
    out.extend(
        _wrap(
            "Slack stores these two settings on every app. Stating Slack's values"
            " here keeps an install from this manifest from stopping on a"
            " difference between the manifest and the app settings.",
            79,
            "  # ",
        )
    )
    out.extend(["  app_level_token_rotation_enabled: false", "  is_mcp_enabled: false"])
    return "\n".join(out) + "\n"


__all__ = [
    "AGENT_DESCRIPTION_MAX",
    "AGENT_VIEW_TIER",
    "COMMAND_REQUIREMENTS",
    "DEFAULT_BOT_NAME",
    "DEFAULT_COMMAND_NAME",
    "EVENT_REQUIREMENTS",
    "GRANTED_SCOPES_HEADER",
    "MAIN_COMMAND",
    "MESSAGE_SUBSCRIPTIONS",
    "METHOD_OPTION_REQUIREMENTS",
    "METHOD_REQUIREMENTS",
    "NARROWINGS",
    "TIERS",
    "TIER_CONNECTOR",
    "TIER_CORE",
    "TIER_GATED",
    "TIER_NAMES",
    "TIER_SUMMARIES",
    "TIER_TOOLS",
    "Requirement",
    "SlashCommand",
    "agent_description",
    "agent_view_for_tier",
    "command_names",
    "command_token",
    "commands_for_tier",
    "events_for_tier",
    "every_requirement",
    "manifest_filename",
    "normalise_command_name",
    "render_manifest",
    "resolve_bot_name",
    "scope_reasons",
    "scope_tiers",
    "scopes_for_tier",
    "slack_granted_scopes",
    "slack_required_scopes",
]
