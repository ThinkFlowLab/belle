"""Slack slash-command payloads and Block Kit picker presentation."""

import hashlib
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from belle_agent import capability_catalog
from belle_agent.platforms.slack import commands as slack_commands
from belle_agent.platforms.slack import task_forms as slack_task_forms
from belle_agent.platforms.slack.admission import SlackQueuedMessage, SlackTurnInitiator, click_identity
from belle_agent.platforms.slack.delivery import slack_error_code, slack_failure_reason
from belle_agent.platforms.slack.identity import CHAT_TYPE_IM, chat_type_from_chat_id, click_channel_id, event_team_id
from belle_agent.platforms.slack.inputs import (
    MAX_OPTION_DESCRIPTION_LENGTH,
    MAX_OPTION_TEXT_LENGTH,
    MAX_OPTIONS_PER_ELEMENT,
    MAX_PLACEHOLDER_LENGTH,
    clamp,
    plain_text,
)
from belle_agent.platforms.slack.text import MAX_SLACK_TEXT_LENGTH
from belle_agent.scopes import SESSION_CHANNEL


@dataclass(frozen=True)
class Invocation:
    """Command identity and text from the Slack payload."""

    team_id: str
    user_id: str
    channel_id: str
    text: str
    event_id: str = ""

    @classmethod
    def from_payload(cls, payload: Any) -> "Invocation":
        payload = payload if isinstance(payload, Mapping) else {}
        return cls(
            **{name: str(payload.get(name) or "").strip() for name in ("team_id", "user_id", "channel_id")},
            text=str(payload.get("text") or ""),
            event_id=str(payload.get("trigger_id") or ""),
        )

    @classmethod
    def from_interaction(cls, payload: Any) -> "Invocation":
        """Read the identity Slack sends with a Block Kit interaction."""
        payload = payload if isinstance(payload, Mapping) else {}
        team, user = payload.get("team"), payload.get("user")
        return cls(
            team_id=str((team.get("id") if isinstance(team, Mapping) else "") or "").strip(),
            user_id=str((user.get("id") if isinstance(user, Mapping) else "") or "").strip(),
            channel_id=click_channel_id(payload),
            text="",
        )

    def identity(self) -> str:
        """Log identity without the command text."""
        return f"team={self.team_id or '-'} channel={self.channel_id or '-'} user={self.user_id or '-'}"


def session_matches(session_id: str, invocation: Invocation) -> bool:
    """Check a slash command's workspace and conversation boundary."""
    prefix = f"slack_{invocation.team_id}_{invocation.channel_id}"
    return session_id == prefix or session_id.startswith(prefix + "_")


CANCEL_SELECT_ACTION = "belle_slash_cancel_select"
CANCELLED_NOTICE = "Cancelled. No action taken."


def select_blocks(
    *,
    prompt: str,
    action_id: str,
    placeholder: str,
    options: Iterable[tuple[str, str] | tuple[str, str, str]] | None = None,
    confirm: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build an ephemeral picker with fixed or searchable options."""
    element: dict[str, Any] = {
        "type": "external_select" if options is None else "static_select",
        "action_id": action_id,
        "placeholder": plain_text(placeholder[:MAX_PLACEHOLDER_LENGTH]),
    }
    if options is None:
        element["min_query_length"] = 0
    else:
        choices = list(options)
        if len(choices) > MAX_OPTIONS_PER_ELEMENT:
            raise ValueError(f"Slack accepts at most {MAX_OPTIONS_PER_ELEMENT} select options")
        element["options"] = []
        for label, value, *description in choices:
            option: dict[str, Any] = {
                "text": plain_text(label[:MAX_OPTION_TEXT_LENGTH]),
                "value": value,
            }
            if description and description[0]:
                option["description"] = plain_text(description[0][:MAX_OPTION_DESCRIPTION_LENGTH])
            element["options"].append(option)
    if confirm is not None:
        element["confirm"] = dict(confirm)
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": prompt}},
        {
            "type": "actions",
            "elements": [
                element,
                {
                    "type": "button",
                    "action_id": CANCEL_SELECT_ACTION,
                    "text": plain_text("Cancel"),
                },
            ],
        },
    ]


_MESSAGE_LINK = re.compile(r"/archives/([A-Z0-9]+)/p(\d{16,})/?$")


def message_link_target(link: str, *, host: str) -> tuple[str, str] | None:
    """Return the conversation and timestamp of a Slack message permalink."""
    parsed = urlparse(link.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if host and parsed.hostname.lower() != host.lower():
        return None
    if not host and not parsed.hostname.lower().endswith(".slack.com"):
        return None
    match = _MESSAGE_LINK.fullmatch(parsed.path)
    if match is None:
        return None
    digits = match.group(2)
    return match.group(1), f"{digits[:-6]}.{digits[-6:]}"

MODEL_USE_SELECT_ACTION_ID = "belle_models_use_select"
SLASH_STOP_SELECT_ID = "belle_slash_stop_select"
SLASH_QUEUE_DROP_SELECT_ID = "belle_slash_queue_drop_select"


def model_picker_blocks() -> list[dict[str, Any]]:
    return select_blocks(
        prompt="Select a model for new requests in this conversation.",
        action_id=MODEL_USE_SELECT_ACTION_ID,
        placeholder="Search configured models",
        confirm={
            "title": {"type": "plain_text", "text": "Change conversation model?"},
            "text": {
                "type": "mrkdwn",
                "text": (
                    "New requests here use this model unless a scope selects "
                    "another one. Running and queued requests keep their "
                    "models. The configured default may return after a restart."
                ),
            },
            "confirm": {"type": "plain_text", "text": "Change model"},
            "deny": {"type": "plain_text", "text": "Cancel"},
        },
    )


def model_options(names: Iterable[str], query: str) -> list[dict[str, Any]]:
    folded = query.casefold()
    return [
        {"text": plain_text(name[:MAX_OPTION_TEXT_LENGTH]), "value": name}
        for name in names
        if len(name) <= 150 and folded in name.casefold()
    ][:MAX_OPTIONS_PER_ELEMENT]


def slash_picker_key(session_id: str, request_id: str, message_ts: str) -> str:
    """Bind a menu choice to its request and source message."""
    raw = f"{session_id}\0{request_id}\0{message_ts}".encode()
    return hashlib.blake2s(raw, digest_size=16).hexdigest()


def slash_picker_preview(text: str) -> str:
    # Block Kit option text is plain text. Strip Slack reference markup
    # before displaying the message in a narrow menu.
    clean = re.sub(r"<[^>]*>", "[reference]", text)
    return clamp(" ".join(clean.split()) or "Message without text", 75)


def slash_picker_time(message_ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(message_ts), UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (ValueError, OverflowError):
        return "time unknown"


def picker_option(
    session_id: str,
    request_id: str,
    sender_name: str,
    message_ts: str,
    preview_text: str,
) -> tuple[str, str, str]:
    return (
        slash_picker_preview(preview_text),
        slash_picker_key(session_id, request_id, message_ts),
        clamp(f"{sender_name} · {slash_picker_time(message_ts)}", 75),
    )


def stop_picker_blocks(options: Iterable[tuple[str, str, str]]) -> list[dict[str, Any]]:
    return select_blocks(
        prompt="Choose a running turn",
        action_id=SLASH_STOP_SELECT_ID,
        placeholder="Select a turn to stop",
        options=options,
        confirm={
            "title": {"type": "plain_text", "text": "Stop turn?"},
            "text": {"type": "mrkdwn", "text": "Stop the selected running turn?"},
            "confirm": {"type": "plain_text", "text": "Stop"},
            "deny": {"type": "plain_text", "text": "Keep running"},
            "style": "danger",
        },
    )


def queue_drop_picker_blocks(
    options: Iterable[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    return select_blocks(
        prompt="Choose a waiting message",
        action_id=SLASH_QUEUE_DROP_SELECT_ID,
        placeholder="Select a message to remove",
        options=options,
        confirm={
            "title": {"type": "plain_text", "text": "Remove message?"},
            "text": {
                "type": "mrkdwn",
                "text": "Remove the selected message from the queue?",
            },
            "confirm": {"type": "plain_text", "text": "Remove"},
            "deny": {"type": "plain_text", "text": "Keep waiting"},
            "style": "danger",
        },
    )


def select_invocation(body: Mapping[str, Any]) -> Invocation | None:
    """Require a consistent identity in a picker interaction."""
    container = body.get("container")
    if not isinstance(container, Mapping):
        return None
    invocation = Invocation.from_interaction(body)
    if not all((invocation.team_id, invocation.user_id, invocation.channel_id)):
        return None
    channel = body.get("channel")
    if (
        isinstance(channel, Mapping)
        and str(channel.get("id") or "").strip() != invocation.channel_id
    ):
        return None
    if str(container.get("channel_id") or "").strip() != invocation.channel_id:
        return None
    return invocation


logger = logging.getLogger(__name__)
_MAX_SLASH_TASK_FORMS = 512
_NOT_THE_BOTS_CONVERSATION = frozenset({"channel_not_found", "not_in_channel"})


class SlackCommandInteractions:
    """Validate Slack actions and present commands through the channel interface."""

    #: The context of a private question asked where no conversation can be given to it.
    _ask_context_unsourced = slack_commands.CONTEXT_NONE

    def _register_commands(self, app: Any) -> None:
        """Register the shared slash command and its Block Kit interactions."""
        app.action(MODEL_USE_SELECT_ACTION_ID)(self._handle_model_select_action)
        app.options(MODEL_USE_SELECT_ACTION_ID)(self._handle_model_select_options)
        app.options(re.compile(rf"^{re.escape(slack_task_forms.NAME_ACTION)}:"))(self._handle_slash_task_name_options)
        app.action(re.compile(rf"^{re.escape(slack_task_forms.ACTION_PREFIX)}"))(self._handle_slash_task_action)
        app.action(SLASH_STOP_SELECT_ID)(self._handle_slash_stop_select)
        app.action(SLASH_QUEUE_DROP_SELECT_ID)(self._handle_slash_queue_drop_select)
        app.action(CANCEL_SELECT_ACTION)(self._handle_slash_select_cancel)
        app.command(self._slash_command())(self._handle_slash_command)

    def _unprovided(self, sub: slack_commands.Subcommand) -> str:
        """Why this deployment cannot run a subcommand, or ``""`` when it can."""
        return "this host does not provide scheduling" if sub.words[0] in {"schedule", "sessions", "bg"} else ""

    def _catalog_caveat(self, kind: str, scoped: Mapping[str, Any]) -> str:
        """What a list of ``kind`` must add about the scope that filtered it, or ``""``."""
        return ""

    def _serving(self) -> bool:
        """Whether the connection takes commands. A class whose ``_running`` is no flag says so itself."""
        return bool(self._running)

    async def _handle_model_select_action(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any],
        respond: Any = None,
    ) -> None:
        """Recheck a model selection when the picker is used."""
        await ack()
        try:
            await self._route_model_select_action(body, action, respond)
        except Exception:
            logger.exception("Slack model selection failed")
            if respond is not None:
                await respond(
                    response_type="ephemeral", replace_original=True,
                    text=slack_commands.FAILED_NOTICE,
                )

    async def _route_model_select_action(
        self, body: dict[str, Any], action: dict[str, Any], respond: Any
    ) -> None:
        invocation = Invocation.from_interaction(body)

        async def answer(text: str) -> None:
            if respond is not None:
                await respond(
                    response_type="ephemeral", replace_original=True, text=text
                )

        refusal = self._slash_command_refusal(invocation)
        if refusal is not None:
            await answer(refusal[0])
            return
        if (
            not self._conversation_chat_type(invocation.channel_id)
            and not await self._slash_chat_type(invocation.channel_id)
        ):
            await answer(slack_commands.conversation_kind_unknown_notice(
                self._bot_display_name()
            ))
            return
        option = action.get("selected_option") if isinstance(action, Mapping) else None
        name = (
            str(option.get("value") or "").strip()
            if isinstance(option, Mapping) else ""
        )
        if not name:
            await answer(slack_commands.FAILED_NOTICE)
            return
        await self._run_models_command(
            slack_commands.MODELS_USE, invocation, name, answer
        )

    async def _handle_model_select_options(
        self, ack: Any, body: dict[str, Any]
    ) -> None:
        """Offer configured models only to an authorized command user."""
        invocation = Invocation.from_interaction(body)
        if self._slash_command_refusal(invocation) is not None:
            await ack(options=[])
            return
        chat_type = self._conversation_chat_type(invocation.channel_id)
        if not chat_type:
            await ack(options=[])
            return
        models = await self._slash_models()
        if not models.known:
            await ack(options=[])
            return
        scoped = self._slash_scoped_model(invocation, chat_type)
        if scoped and models.accepts(scoped):
            await ack(options=[])
            return
        await ack(options=model_options(
            models.names, str(body.get("value") or "")
        ))

    async def _run_models_command(
        self,
        sub: slack_commands.Subcommand,
        invocation: Invocation,
        arguments: str,
        answer: Callable[..., Any],
    ) -> str:
        """Apply Belle's model-command decision to this conversation."""
        models = await self._slash_models()
        scoped = ""
        if models.known:
            chat_type = self._conversation_chat_type(invocation.channel_id)
            scoped = self._slash_scoped_model(invocation, chat_type)
        return await slack_commands.run_models_command(
            sub, arguments, models=models, scoped=scoped,
            overrides=self._model_overrides,
            key=(invocation.team_id, invocation.channel_id),
            command=self._slash_command(), answer=answer,
            answer_lines=self._answer_lines,
            picker_blocks=model_picker_blocks,
        )

    @staticmethod
    async def _answer_lines(lines: list[str], answer: Callable[[str], Any]) -> None:
        chunk = lines[0]
        for line in lines[1:]:
            if len(chunk) + len(line) + 1 > MAX_SLACK_TEXT_LENGTH:
                await answer(chunk)
                chunk = line
            else:
                chunk += "\n" + line
        await answer(chunk)

    async def _handle_slash_command(
        self, ack: Any, command: dict[str, Any], respond: Any = None
    ) -> None:
        """Acknowledge and answer a slash command."""
        invocation = Invocation.from_payload(command)
        acked = False

        async def acknowledge(
            text: str = "", *, blocks: list[dict[str, Any]] | None = None
        ) -> bool:
            """Acknowledge once; ``False`` when that had already been done."""
            nonlocal acked
            if acked:
                return False
            acked = True
            if text or blocks is not None:
                payload: dict[str, Any] = {
                    "response_type": "ephemeral", "text": text,
                }
                if blocks is not None:
                    payload["blocks"] = blocks
                await ack(**payload)
            else:
                await ack()
            return True

        async def answer(
            text: str, *, blocks: list[dict[str, Any]] | None = None
        ) -> None:
            if respond is None:
                logger.warning(
                    "Slack %s had nothing to answer through: %s",
                    self._slash_command(),
                    invocation.identity(),
                )
                return
            try:
                payload: dict[str, Any] = {"response_type": "ephemeral", "text": text}
                if blocks is not None:
                    payload["blocks"] = blocks
                answered = await respond(**payload)
                # Bolt returns Slack's answer to the response URL. A refusal there raises nothing.
                status = getattr(answered, "status_code", 200)
                said = str(getattr(answered, "body", "") or "")
                if status != 200 or '"ok":false' in said.replace(" ", ""):
                    logger.warning(
                        "Slack %s answer was not accepted by Slack: %s status=%s slack_said=%s sent_chars=%d",
                        self._slash_command(),
                        invocation.identity(),
                        status,
                        said[:120],
                        len(text),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Slack %s could not answer %s: %s",
                    self._slash_command(),
                    invocation.identity(),
                    type(exc).__name__,
                )

        try:
            outcome = await self._run_slash_command(
                command, invocation, acknowledge, answer
            )
        except Exception:
            logger.exception(
                "%s %s raised: %s",
                self._log_tag,
                self._slash_command(),
                invocation.identity(),
            )
            outcome = "failed:handler-raised"
            if acked:
                await answer(slack_commands.FAILED_NOTICE)
            else:
                await acknowledge(slack_commands.FAILED_NOTICE)
        logger.info(
            "Slack %s %s -> %s", self._slash_command(), invocation.identity(), outcome
        )

    async def _run_slash_command(
        self,
        command: Any,
        invocation: Invocation,
        acknowledge: Callable[..., Any],
        answer: Callable[[str], Any],
    ) -> str:
        """Gate, parse and run one invocation; return the outcome to log."""
        if not isinstance(command, Mapping):
            await acknowledge(slack_commands.FAILED_NOTICE)
            return "dropped:malformed-payload"
        sub, arguments = slack_commands.resolve(invocation.text)
        refusal = self._slash_command_refusal(
            invocation,
            stop_arguments=arguments if sub is slack_commands.STOP else None,
        )
        if refusal is not None:
            text, outcome = refusal
            await acknowledge(text)
            return outcome

        if sub is None:
            await acknowledge(slack_commands.unknown_text(
                self._slash_command(), bot_name=self._bot_display_name()
            ))
            return "answered:unknown-subcommand"
        unprovided = self._unprovided(sub)
        if unprovided:
            await acknowledge(slack_commands.not_available_text(
                sub, self._slash_command(), unprovided
            ))
            return f"answered:not-provided {sub.name}"
        given = bool(arguments.strip())
        if (
            sub not in (
                slack_commands.MODELS_USE, slack_commands.SKILLS_USE,
                slack_commands.SUBAGENTS_USE, slack_commands.ASK,
            )
            and (sub.requires_arguments if not given else not sub.arguments)
        ):
            await acknowledge(
                slack_commands.usage_text(sub, self._slash_command())
            )
            return f"answered:usage {sub.name}"
        if sub is slack_commands.HELP:
            await acknowledge(slack_commands.help_text(
                self._slash_command(), bot_name=self._bot_display_name(),
                unavailable={
                    each: reason for each in slack_commands.SUBCOMMANDS
                    if (reason := self._unprovided(each))
                },
            ))
            return "answered:help"
        if not self._conversation_chat_type(invocation.channel_id):
            await acknowledge()
            if not await self._slash_chat_type(invocation.channel_id):
                await answer(slack_commands.conversation_kind_unknown_notice(
                    self._bot_display_name()
                ))
                return "refused:conversation-kind-unknown"
        if sub in (
            slack_commands.MODELS_LIST, slack_commands.MODELS_CURRENT,
            slack_commands.MODELS_USE, slack_commands.MODELS_RESET,
        ):
            await acknowledge()
            return await self._run_models_command(sub, invocation, arguments, answer)
        if sub.words[0] == "schedule":
            await acknowledge()
            return await self._run_schedule_command(sub, invocation, arguments, answer)
        if sub.words[0] in {"bg", "sessions"}:
            await acknowledge()
            return await self._run_session_command(sub, invocation, arguments, answer)
        if sub in (slack_commands.STOP, slack_commands.QUEUE_DROP):
            await acknowledge()
            handler = self._run_stop if sub is slack_commands.STOP else self._answer_queue_drop
            return await handler(invocation, arguments, answer)
        for choice, handler in (
            (slack_commands.STATUS, self._answer_status),
            (slack_commands.SKILLS_LIST, self._answer_skills_list),
            (slack_commands.EXPERTS_LIST, self._answer_experts_list),
            (slack_commands.SUBAGENTS_LIST, self._answer_subagents_list),
            (slack_commands.QUEUE_LIST, self._answer_queue_list),
        ):
            if sub is choice:
                await acknowledge()
                return await handler(invocation, answer)
        if sub in (slack_commands.SUBAGENTS_USE, slack_commands.SKILLS_USE):
            kind = "subagent" if sub is slack_commands.SUBAGENTS_USE else "skill"
            return await self._run_named_task_use(kind, invocation, arguments, acknowledge, answer)
        if sub is slack_commands.ASK:
            return await self._run_ask(invocation, arguments, acknowledge, answer)
        await acknowledge(slack_commands.FAILED_NOTICE)
        return f"failed:no-runner {sub.name}"

    async def _slash_chat_type(self, channel_id: str) -> str:
        """Resolve a cold slash command's conversation kind after acknowledging."""
        known = self._conversation_chat_type(channel_id)
        if known or self._client is None:
            return known
        read_scopes = {"channels:read", "groups:read", "mpim:read"}
        if self._granted_scopes is not None and not self._granted_scopes.intersection(read_scopes):
            return ""
        try:
            response = await self._client.conversations_info(channel=channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "Slack %s could not verify conversation %s: %s",
                self._slash_command(),
                channel_id,
                slack_error_code(exc) or type(exc).__name__,
            )
            return ""
        get = getattr(response, "get", None)
        info = get("channel") if callable(get) else None
        if not isinstance(info, Mapping) or info.get("id") != channel_id:
            return ""
        if info.get("is_im"):
            kind = "im"
        elif info.get("is_mpim"):
            kind = "mpim"
        elif info.get("is_private") or info.get("is_group"):
            kind = "group"
        elif info.get("is_channel"):
            kind = "channel"
        else:
            return ""
        self._chat_types[channel_id] = kind
        return kind

    def _slash_command_refusal(
        self,
        invocation: Invocation,
        *,
        stop_arguments: str | None = None,
    ) -> tuple[str, str] | None:
        """Why this invocation gets no further, or ``None`` when it may.

        Check the workspace, ``allow_from``, then channel access. A recorded
        starter may stop their own uniquely identified turn outside
        ``allow_from``, as with the stop button; channel access still applies.
        An instance that has not learned its team claims nothing.
        """
        mine = self._identity.team_id
        if not mine or invocation.team_id != mine:
            return (
                slack_commands.foreign_workspace_notice(self._bot_display_name()),
                "refused:foreign-workspace",
            )
        if not invocation.user_id or (
            not self.is_allowed(invocation.user_id)
            and (
                stop_arguments is None
                or not self._slash_stop_is_own_turn(invocation, stop_arguments)
            )
        ):
            return (
                slack_commands.not_permitted_notice(self._bot_display_name()),
                "refused:user-not-in-allow_from",
            )
        channel_id = invocation.channel_id
        is_dm = chat_type_from_chat_id(channel_id) == CHAT_TYPE_IM
        if not is_dm and not self._channel_is_allowed(channel_id):
            return (
                slack_commands.conversation_not_enabled_notice(self._bot_display_name()),
                "refused:channel-not-in-allowed_channel_ids",
            )
        return None

    def _slash_stop_candidates(
        self, invocation: Invocation, target_ts: str
    ) -> list[tuple[str, SlackTurnInitiator]]:
        """Current turns in this conversation, optionally narrowed by message."""
        return [
            (session_id, entry)
            for session_id, entry in tuple(self._admission.turn_initiators.items())
            if session_matches(session_id, invocation)
            and (not entry.channel_id or entry.channel_id == invocation.channel_id)
            and (not target_ts or entry.message_ts == target_ts)
            and self.turn_initiator(session_id) is entry
        ]

    def _slash_stop_target_ts(
        self, invocation: Invocation, arguments: str
    ) -> str | None:
        if not arguments.strip():
            return ""
        target = message_link_target(
            arguments, host=self._identity.host
        )
        if target is None or target[0] != invocation.channel_id:
            return None
        return target[1]

    def _slash_stop_is_own_turn(
        self, invocation: Invocation, arguments: str
    ) -> bool:
        """Only a uniquely identified starter may bypass the command allow-list."""
        target_ts = self._slash_stop_target_ts(invocation, arguments)
        if target_ts is None:
            return False
        candidates = self._slash_stop_candidates(invocation, target_ts)
        if not arguments.strip():
            return any(entry.user_id == invocation.user_id for _, entry in candidates)
        return len(candidates) == 1 and candidates[0][1].user_id == invocation.user_id

    async def _slash_picker_options(
        self,
        entries: Iterable[tuple[str, str, str, str, str]],
        names: dict[str, str],
    ) -> list[tuple[str, str, str]]:
        options = []
        for session_id, request_id, user_id, message_ts, preview_text in entries:
            if user_id not in names:
                names[user_id] = await self._queue_sender_name(user_id)
            options.append(picker_option(
                session_id, request_id, names[user_id], message_ts, preview_text
            ))
        return options

    async def _stop_picker_blocks(
        self,
        invocation: Invocation,
        candidates: list[tuple[str, SlackTurnInitiator]],
    ) -> tuple[list[dict[str, Any]], bool]:
        eligible = [
            (session_id, entry)
            for session_id, entry in candidates
            if self._may_stop(session_id, invocation.user_id, invocation.channel_id)[0]
        ]
        if not eligible:
            return [], False
        options = await self._slash_picker_options(
            (
                (session_id, entry.request_id, entry.user_id,
                 entry.message_ts, entry.preview_text)
                for session_id, entry in eligible[:100]
            ),
            {},
        )
        return stop_picker_blocks(options), len(eligible) > 100

    async def _run_stop(
        self,
        invocation: Invocation,
        arguments: str,
        answer: Callable[[str], Any],
        *,
        selected_key: str | None = None,
    ) -> str:
        """Stop one live turn in the command's conversation."""
        if not arguments.strip() and selected_key is None:
            candidates = self._slash_stop_candidates(invocation, "")
            if not candidates:
                await answer(slack_commands.STOP_NONE_NOTICE)
                return "refused:stop-no-turn"
            blocks, overflow = await self._stop_picker_blocks(invocation, candidates)
            if not blocks:
                await answer(slack_commands.STOP_REFUSED_NOTICE)
                return "refused:stop-permission"
            notice = "Select the turn to stop."
            if overflow:
                notice += " More turns exist; use a message link for one not shown."
            await answer(notice, blocks=blocks)
            return "answered:stop-picker"

        target_ts = self._slash_stop_target_ts(invocation, arguments)
        if target_ts is None:
            await answer(slack_commands.STOP_LINK_INVALID_NOTICE)
            return "refused:stop-invalid-link"

        candidates = self._slash_stop_candidates(invocation, target_ts)
        if selected_key is not None:
            candidates = [
                (session_id, entry)
                for session_id, entry in candidates
                if slash_picker_key(
                    session_id, entry.request_id, entry.message_ts
                ) == selected_key
            ]
        if not candidates:
            await answer(slack_commands.STOP_NONE_NOTICE)
            return "refused:stop-no-turn"
        if len(candidates) != 1:
            await answer(slack_commands.STOP_AMBIGUOUS_NOTICE)
            return "refused:stop-ambiguous"

        session_id, entry = candidates[0]
        permitted, ground = self._may_stop(
            session_id, invocation.user_id, invocation.channel_id
        )
        if not permitted:
            await answer(slack_commands.STOP_REFUSED_NOTICE)
            return f"refused:stop-{ground}"

        if not await self._stop_turn(session_id, entry, invocation, ground):
            await answer(slack_commands.STOP_NONE_NOTICE)
            return "refused:stop-stale"
        result = slack_commands.STOP_ACCEPTED_NOTICE
        if selected_key is not None and entry.message_ts:
            permalink = self._thread_permalink(invocation.channel_id, entry.message_ts)
            if permalink:
                result = f"Stopping <{permalink}|that turn>."
        await answer(result)
        return "stopped:turn"

    def _status_facts(
        self, invocation: Invocation, **agent: Any
    ) -> slack_commands.StatusFacts:
        """The connector's half of ``status`` for this invocation."""
        self._prune_pending_events()
        channel_id = invocation.channel_id
        user_id = invocation.user_id
        chat_type = self._conversation_chat_type(channel_id)
        is_dm = chat_type_from_chat_id(channel_id) == CHAT_TYPE_IM
        if is_dm:
            chat_type = CHAT_TYPE_IM
        scope = "" if is_dm else self._session_scope(channel_id, user_id, chat_type)

        # Include this conversation's thread, channel, and side sessions.
        mine = [
            session
            for session in {
                *self._admission.turn_initiators, *self._admission.queued_messages,
                *self._admission.pending_events, *self._admission.admitting_events,
            }
            if session_matches(session, invocation)
        ]
        running_entries = {
            session: entry for session in mine
            if (entry := self._admission.turn_initiators.get(session)) is not None
        }
        running = len(running_entries)
        event_running = any(
            self._admission.event_turn_sessions.get(session)
            == entry.request_id for session, entry in running_entries.items()
        )
        queued = sum(len(self._admission.queued_messages.get(session) or ()) for session in mine)
        pending_events = len({
            id(entry)
            for session in mine
            for entry in (
                *self._admission.pending_events.get(session, ()),
                *self._admission.admitting_events.get(session, {}).values(),
            )
        })
        running_message_link = ""
        if running == 1:
            entry = next(iter(running_entries.values()))
            if entry.channel_id == channel_id and entry.message_ts:
                running_message_link = self._thread_permalink(
                    channel_id, entry.message_ts
                )

        ask_context, ask_link = "", ""
        if not is_dm:
            source, thread_ts = self._channel_source_session(invocation)
            ask_context = (
                slack_commands.CONTEXT_NONE
                if not source
                else slack_commands.CONTEXT_THREAD
                if thread_ts
                else slack_commands.CONTEXT_CHANNEL
            )
            ask_link = self._thread_permalink(channel_id, thread_ts)

        return slack_commands.StatusFacts(
            **agent,
            model_override=self._channel_model_name(channel_id, user_id, chat_type),
            chat_type=chat_type,
            mid_turn=self._mid_turn_mode(
                channel_id,
                user_id,
                chat_type=chat_type,
                session_is_channel_wide=scope == SESSION_CHANNEL,
            ),
            running=running,
            event_running=event_running,
            running_message_link=running_message_link,
            queued=queued,
            pending_events=pending_events,
            lost_events=self._lost_events.get(channel_id, 0),
            ask_context=ask_context,
            ask_link=ask_link,
        )

    async def _answer_queue_list(
        self, invocation: Invocation, answer: Callable[[str], Any]
    ) -> str:
        self._prune_queued_messages()
        entries = self._conversation_queue(invocation)
        if not entries:
            await answer("No messages are waiting in this channel or direct message.")
            return "answered:queue-list count=0"
        lines = [f"Messages waiting in this channel or direct message ({len(entries)}):"]
        sender_names: dict[str, str] = {}
        for entry in entries:
            link = self._thread_permalink(entry.channel_id, entry.message_ts)
            label = f"<{link}|message>" if link else entry.message_ts
            if entry.user_id not in sender_names:
                sender_names[entry.user_id] = await self._queue_sender_name(entry.user_id)
            lines.append(f"• {label} from {sender_names[entry.user_id]}")
        await self._answer_lines(lines, answer)
        return f"answered:queue-list count={len(entries)}"

    async def _queue_drop_picker_blocks(
        self,
        invocation: Invocation,
        entries: list[SlackQueuedMessage],
        *,
        sender_names: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        eligible = [
            entry for entry in entries if self._may_drop_queued(invocation, entry)
        ]
        if not eligible:
            return [], False
        options = await self._slash_picker_options(
            (
                (entry.session_id, str(getattr(entry.request, "id", "") or ""),
                 entry.user_id, entry.message_ts, entry.preview_text)
                for entry in eligible[:100]
            ),
            sender_names if sender_names is not None else {},
        )
        return queue_drop_picker_blocks(options), len(eligible) > 100

    async def _answer_queue_drop(
        self,
        invocation: Invocation,
        link: str,
        answer: Callable[[str], Any],
        *,
        selected_key: str | None = None,
    ) -> str:
        if not link.strip() and selected_key is None:
            self._prune_queued_messages()
            entries = self._conversation_queue(invocation)
            if not entries:
                await answer("No messages are waiting in this conversation.")
                return "refused:queue-not-found"
            blocks, overflow = await self._queue_drop_picker_blocks(invocation, entries)
            if not blocks:
                await answer("You cannot remove any waiting message here.")
                return "refused:queue-drop-permission"
            notice = "Select a waiting message to remove."
            if overflow:
                notice += " More messages exist; use a message link for one not shown."
            await answer(notice, blocks=blocks)
            return "answered:queue-drop-picker"

        stamp = (
            self._queued_message_link(link, invocation.channel_id)
            if link.strip() else ""
        )
        if selected_key is None and not stamp:
            await answer("Use a message link from this conversation.")
            return "refused:queue-link"
        self._prune_queued_messages()

        def matches(entry: SlackQueuedMessage) -> bool:
            if selected_key is None:
                return entry.message_ts == stamp
            request_id = str(getattr(entry.request, "id", "") or "")
            return slash_picker_key(
                entry.session_id, request_id, entry.message_ts
            ) == selected_key

        found = next(
            (entry for entry in self._conversation_queue(invocation) if matches(entry)),
            None,
        )
        if found is None:
            await answer("That message is not waiting in this conversation.")
            return "refused:queue-not-found"
        if not self._may_drop_queued(invocation, found):
            await answer("Only the sender can remove that waiting message here.")
            return "refused:queue-drop-permission"
        if not await self._drop_queued(found, invocation):
            await answer("That message is no longer waiting.")
            return "refused:queue-not-found"
        result = "Removed the waiting message. Its turn will not start."
        if selected_key is not None:
            permalink = self._thread_permalink(found.channel_id, found.message_ts)
            if permalink:
                result = (
                    f"Removed <{permalink}|the waiting message>."
                    " Its turn will not start."
                )
        await answer(result)
        return "answered:queue-drop"

    async def _queue_sender_name(self, user_id: str) -> str:
        """Resolve a queue sender to inert text, falling back to their user id."""
        name = user_id
        if self._client is not None:
            try:
                response = await self._client.users_info(user=user_id)
                if response.get("ok"):
                    user = response.get("user") or {}
                    profile = user.get("profile") or {}
                    name = (
                        profile.get("display_name")
                        or profile.get("real_name")
                        or user.get("real_name")
                        or user.get("name")
                        or user_id
                    )
            except Exception:  # noqa: BLE001, S110 - names are optional for this command
                pass
        # Break @here/@channel/@everyone and plain @name parsing without
        # changing how the name looks. Angle brackets must never form a mention.
        return slack_commands.escape(" ".join(str(name).split())).replace("@", "@\u200b")

    def _queued_message_link(self, link: str, channel_id: str) -> str:
        """Return a message timestamp only for this workspace and channel."""
        raw = link.strip().strip("<>").split("|", 1)[0]
        target = message_link_target(raw, host=self._identity.host)
        return target[1] if target and target[0] == channel_id else ""

    async def _handle_slash_stop_select(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any], respond: Any = None
    ) -> None:
        await self._handle_slash_selection(
            ack, body, action, respond, kind="stop"
        )

    async def _handle_slash_queue_drop_select(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any], respond: Any = None
    ) -> None:
        await self._handle_slash_selection(
            ack, body, action, respond, kind="queue-drop"
        )

    async def _handle_slash_select_cancel(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any],
        respond: Any = None,
    ) -> None:
        """Dismiss a selector without changing a model, turn, or queue."""
        await ack()
        if (
            not isinstance(action, Mapping)
            or action.get("action_id") != CANCEL_SELECT_ACTION
        ):
            return
        if respond is not None:
            await respond(
                response_type="ephemeral", replace_original=True,
                text=CANCELLED_NOTICE, blocks=[],
            )

    async def _handle_slash_selection(
        self,
        ack: Any,
        body: Any,
        action: Any,
        respond: Any,
        *,
        kind: str,
    ) -> None:
        await ack()
        if (
            not self._serving()
            or not isinstance(body, Mapping)
            or not isinstance(action, Mapping)
        ):
            return
        invocation = select_invocation(body)
        if invocation is None:
            return

        async def answer(text: str) -> None:
            if respond is not None:
                try:
                    await respond(
                        response_type="ephemeral", replace_original=True, text=text
                    )
                    return
                except Exception:  # noqa: BLE001 - still tell the clicker below
                    logger.warning("Slack %s picker could not replace its message", kind)
            await self._notify_clicker(
                body, invocation.user_id, text, notice=f"slash-{kind}"
            )

        expected_action = (
            SLASH_STOP_SELECT_ID if kind == "stop" else SLASH_QUEUE_DROP_SELECT_ID
        )
        choice = action.get("selected_option")
        selected_key = (
            str(choice.get("value") or "") if isinstance(choice, Mapping) else ""
        )
        if action.get("action_id") != expected_action or not re.fullmatch(
            r"[0-9a-f]{32}", selected_key
        ):
            await answer("That selection is invalid. Run the command again.")
            return
        refusal = self._slash_command_refusal(
            invocation, stop_arguments="" if kind == "stop" else None
        )
        if refusal is not None:
            await answer(refusal[0])
            return
        try:
            handler = self._run_stop if kind == "stop" else self._answer_queue_drop
            outcome = await handler(invocation, "", answer, selected_key=selected_key)
        except Exception:
            logger.exception("Slack %s picker raised: %s", kind, invocation.identity())
            await answer(slack_commands.FAILED_NOTICE)
            return
        logger.info(
            "Slack %s picker %s -> %s",
            self._slash_command(),
            invocation.identity(),
            outcome,
        )

    async def _answer_task_form(
        self,
        kind: str,
        invocation: Invocation,
        answer: Callable[..., Any],
        text: str,
        *,
        names: list[str] | None = None,
        initial_name: str = "",
    ) -> None:
        """Offer a private form; keep the typed command available as a fallback."""
        if kind != "ask" and not names:
            await answer(text)
            return
        now = time.monotonic()
        self._slash_task_forms = {
            key: form for key, form in self._slash_task_forms.items()
            if now - form.created_at < slack_task_forms.FORM_LIFETIME_SECONDS
        }
        while len(self._slash_task_forms) >= _MAX_SLASH_TASK_FORMS:
            self._slash_task_forms.pop(next(iter(self._slash_task_forms)))
        nonce = uuid.uuid4().hex
        form = slack_task_forms.TaskForm(
            kind=kind,
            team_id=invocation.team_id,
            user_id=invocation.user_id,
            channel_id=invocation.channel_id,
            created_at=now,
            initial_name=initial_name,
            names=tuple(names or ()),
        )
        self._slash_task_forms[nonce] = form
        blocks = slack_task_forms.task_form_blocks(
            form, nonce, intro_text=text, bot_name=self._bot_display_name(),
        )
        await answer(text, blocks=blocks)

    async def _handle_slash_task_action(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any],
        respond: Any = None,
    ) -> None:
        """Submit a slash task once; input-change events only need an ack."""
        await ack()
        try:
            await self._route_slash_task_action(body, action, respond)
        except Exception:
            logger.exception(
                "%s a slash task action raised: %s",
                self._log_tag,
                click_identity(body, action),
            )
            await self._answer_slash_task_action(respond, slack_commands.FAILED_NOTICE)

    @staticmethod
    async def _answer_slash_task_action(
        respond: Any, text: str, *, replace: bool = False
    ) -> None:
        if respond is not None:
            await respond(response_type="ephemeral", text=text, replace_original=replace)

    async def _route_slash_task_action(
        self, body: Any, action: Any, respond: Any,
    ) -> None:
        if not isinstance(body, Mapping) or not isinstance(action, Mapping):
            return
        action_id = action.get("action_id")
        if action_id not in (
            slack_task_forms.SUBMIT_ACTION, slack_task_forms.CANCEL_ACTION
        ):
            return
        nonce = str(action.get("value") or "")
        form = self._slash_task_forms.get(nonce)
        if form is None:
            await self._answer_slash_task_action(
                respond, "This form has expired. Run the command again."
            )
            return
        user = body.get("user")
        user_id = str(user.get("id") or "") if isinstance(user, Mapping) else ""
        if (event_team_id(body) != form.team_id or user_id != form.user_id
                or click_channel_id(body) != form.channel_id):
            await self._answer_slash_task_action(
                respond, slack_commands.not_permitted_notice(self._bot_display_name())
            )
            return
        if (
            not self._serving()
            or time.monotonic() - form.created_at >= slack_task_forms.FORM_LIFETIME_SECONDS
        ):
            self._slash_task_forms.pop(nonce, None)
            await self._answer_slash_task_action(
                respond, "This form has expired. Run the command again."
            )
            return
        if action_id == slack_task_forms.CANCEL_ACTION:
            # Claim before awaiting so a replay cannot submit the form.
            self._slash_task_forms.pop(nonce, None)
            if respond is not None:
                await respond(
                    response_type="ephemeral", replace_original=True,
                    text=CANCELLED_NOTICE, blocks=[],
                )
            return
        invocation = Invocation(
            team_id=form.team_id, user_id=form.user_id,
            channel_id=form.channel_id, text="",
        )
        refusal = self._slash_command_refusal(invocation)
        if refusal is not None:
            await self._answer_slash_task_action(respond, refusal[0])
            return
        if (
            not self._conversation_chat_type(form.channel_id)
            and not await self._slash_chat_type(form.channel_id)
        ):
            await self._answer_slash_task_action(
                respond,
                slack_commands.conversation_kind_unknown_notice(self._bot_display_name()),
            )
            return
        state = body.get("state")
        values = state.get("values") if isinstance(state, Mapping) else None
        name, task = slack_task_forms.read_task_state(
            values, form.initial_name, nonce=nonce, names=form.names
        )
        if (form.kind != "ask" and not name) or not task:
            await self._answer_slash_task_action(
                respond, "Select a name and enter a task before submitting."
                if form.kind != "ask" else "Enter a question before submitting."
            )
            return
        # Claim before awaiting so concurrent clicks cannot dispatch twice.
        if self._slash_task_forms.pop(nonce, None) is None:
            await self._answer_slash_task_action(
                respond, "This form was already submitted. Run the command again."
            )
            return
        await self._answer_slash_task_action(
            respond, "Submitting your request.",
            replace=True,
        )

        async def acknowledged(_text: str = "") -> bool:
            return False

        async def answer(text: str) -> None:
            await self._answer_slash_task_action(respond, text)

        if form.kind == "ask":
            outcome = await self._run_ask(invocation, task, acknowledged, answer)
        else:
            outcome = await self._run_named_task_use(
                form.kind, invocation, f"{name} {task}", acknowledged, answer
            )
        logger.info(
            "Slack %s task form %s -> %s",
            self._slash_command(),
            invocation.identity(),
            outcome,
        )

    async def _answer_skills_list(
        self,
        invocation: Invocation,
        answer: Callable[[str], Any],
    ) -> str:
        """List installed skills allowed by this conversation's scope."""
        catalog, scoped, timed_out = await self._slash_catalog(
            invocation, "skills"
        )
        if catalog is None:
            await answer(slack_commands.SKILLS_UNAVAILABLE_NOTICE)
            return (
                "failed:skills-list-timed-out"
                if timed_out
                else "failed:skills-list-refused"
            )
        skills = capability_catalog.usable_skills(
            catalog, scoped.get("agent_skills_available"),
            scoped.get("agent_skills_unavailable", ()),
        )
        await answer(slack_commands.skills_text(
            skills, scoped.get("agent_skills_required", ()),
        ) + self._catalog_caveat("skills", scoped))
        return f"answered:skills-list count={len(skills)}"

    async def _answer_experts_list(
        self,
        invocation: Invocation,
        answer: Callable[[str], Any],
    ) -> str:
        """List installed experts and the scope's selection for this conversation."""
        catalog, scoped, _ = await self._slash_catalog(
            invocation, "experts"
        )
        if catalog is None:
            await answer(slack_commands.experts_unavailable_notice(self._bot_display_name()))
            return "failed:experts-list-unavailable"
        selected = str(scoped.get("agent_template_name") or "")
        await answer(slack_commands.experts_text(catalog, selected))
        return f"answered:experts-list catalog_count={len(catalog)}"

    async def _answer_subagents_list(
        self,
        invocation: Invocation,
        answer: Callable[[str], Any],
    ) -> str:
        """List mounted subagents allowed by this conversation's scope."""
        catalog, scoped, _ = await self._slash_catalog(
            invocation, "subagents"
        )
        if catalog is None:
            await answer(slack_commands.subagents_unavailable_notice(self._bot_display_name()))
            return "failed:subagents-list-unavailable"
        names = capability_catalog.usable_subagents(
            catalog, scoped.get("agent_subagents_available")
        )
        await answer(
            slack_commands.subagents_text(names) + self._catalog_caveat("subagents", scoped)
        )
        return f"answered:subagents-list count={len(names)}"

    async def _handle_slash_task_name_options(
        self, ack: Any, body: dict[str, Any]
    ) -> None:
        """Search a form's names after checking its owner and current scope."""
        options: list[dict[str, Any]] = []
        try:
            invocation = Invocation.from_interaction(body)
            action_id = str(body.get("action_id") or "")
            prefix = f"{slack_task_forms.NAME_ACTION}:"
            nonce = action_id[len(prefix):] if action_id.startswith(prefix) else ""
            form = self._slash_task_forms.get(nonce)
            if (
                self._serving() and form is not None
                and time.monotonic() - form.created_at < slack_task_forms.FORM_LIFETIME_SECONDS
                and form.kind in ("skill", "subagent")
                and (invocation.team_id, invocation.user_id, invocation.channel_id)
                == (form.team_id, form.user_id, form.channel_id)
                and self._slash_command_refusal(invocation) is None
            ):
                chat_type = self._conversation_chat_type(form.channel_id)
                if chat_type:
                    scoped = self._slash_scoped_params(
                        form.channel_id, form.user_id, chat_type, form.team_id
                    )
                    is_skill = form.kind == "skill"
                    available = scoped.get(
                        "agent_skills_available" if is_skill
                        else "agent_subagents_available"
                    )
                    unavailable = scoped.get("agent_skills_unavailable", ()) if is_skill else ()
                    query = str(body.get("value") or "").casefold()
                    options = [
                        slack_task_forms.external_name_option(name, index)
                        for index, name in enumerate(form.names)
                        if capability_catalog.scope_allows(name, available, unavailable)
                        and query in name.casefold()
                    ][:100]
        except Exception:  # noqa: BLE001 - a search that fails offers no name, and is logged
            self._log.exception("Slack task form options failed")
        await ack(options=options)

    async def _run_named_task_use(
        self,
        kind: str,
        invocation: Invocation,
        arguments: str,
        acknowledge: Callable[..., Any],
        answer: Callable[[str], Any],
    ) -> str:
        """Start a task with a required skill or subagent from the scoped catalog."""
        is_skill = kind == "skill"
        plural = "skills" if is_skill else "subagents"
        parts = arguments.strip().split(maxsplit=1)
        await acknowledge()
        catalog, scoped, _ = await self._slash_catalog(
            invocation, plural
        )
        if catalog is None:
            await answer(
                slack_commands.SKILLS_UNAVAILABLE_NOTICE if is_skill
                else slack_commands.subagents_unavailable_notice(self._bot_display_name())
            )
            return f"failed:{plural}-list-unavailable"
        if is_skill:
            items = capability_catalog.usable_skills(
                catalog, scoped.get("agent_skills_available"),
                scoped.get("agent_skills_unavailable", ()),
            )
            names = [str(item.get("name") or "") for item in items]
        else:
            names = capability_catalog.usable_subagents(
                catalog, scoped.get("agent_subagents_available")
            )
            items = names
        if len(parts) < 2 or not parts[1].strip():
            if not names:
                await answer(
                    slack_commands.skills_text(items) if is_skill
                    else slack_commands.subagents_text(names)
                )
                return f"answered:{plural}-use-unavailable"
            await self._answer_task_form(
                kind, invocation, answer,
                "Select a skill and enter one task." if is_skill
                else "Select a subagent and enter one task.",
                names=names,
                initial_name=parts[0] if parts else "",
            )
            return f"answered:{plural}-use-form"
        name, task = parts
        selected = (
            capability_catalog.find_skill(items, name) if is_skill
            else capability_catalog.find_name(names, name)
        )
        if not selected:
            await answer(
                slack_commands.skill_unknown_text(name, self._slash_command())
                if is_skill else slack_commands.subagent_unknown_text(
                    name, self._slash_command()
                )
            )
            return f"refused:{kind}-not-usable-here"
        outcome = await self._run_ask(
            invocation, task, acknowledge, answer,
            skill=selected if is_skill else "",
            subagent=selected if not is_skill else "",
        )
        return f"{outcome} {kind}={selected}"

    async def _required_items_refusal_in_dm(
        self,
        invocation: Invocation,
        dm_channel: str,
        anchor_ts: str,
        skill: str,
        subagent: str,
        answer: Callable[[str], Any],
    ) -> str:
        """Check the destination scope after posting the DM anchor."""
        bot = self._bot_display_name()
        scoped = self._slash_scoped_params(
            dm_channel, invocation.user_id, CHAT_TYPE_IM, self._identity.team_id
        )
        for kind, name, available, unavailable, refusal in (
            (
                "skill", skill, scoped.get("agent_skills_available"),
                scoped.get("agent_skills_unavailable", ()), slack_commands.skill_not_in_dm_text,
            ),
            (
                "subagent", subagent, scoped.get("agent_subagents_available"), (),
                slack_commands.subagent_not_in_dm_text,
            ),
        ):
            if not name or capability_catalog.scope_allows(name, available, unavailable):
                continue
            text = refusal(name, bot_name=bot)
            await self._rewrite_anchor(dm_channel, anchor_ts, text)
            if invocation.channel_id != dm_channel:
                await answer(text)
            return f"refused:{kind}-not-allowed-in-the-dm"
        return ""

    async def _run_ask(
        self,
        invocation: Invocation,
        question: str,
        acknowledge: Callable[..., Any],
        answer: Callable[[str], Any],
        skill: str = "",
        subagent: str = "",
    ) -> str:
        """Post a private question, then dispatch it as a DM message turn."""
        if not question.strip():
            await acknowledge()
            await self._answer_task_form(
                "ask", invocation, answer, "Enter a question for a private answer."
            )
            return "answered:ask-form"
        direct = chat_type_from_chat_id(invocation.channel_id) == CHAT_TYPE_IM
        channel_id = invocation.channel_id
        if direct:
            source = context = ""
            await acknowledge()
            target = channel_id
        else:
            source, thread_ts = (
                ("", "") if subagent else self._channel_source_session(invocation)
            )
            if subagent:
                context = slack_commands.CONTEXT_TASK
            elif not source:
                context = self._ask_context_unsourced
            elif thread_ts:
                context = slack_commands.CONTEXT_THREAD
            else:
                context = slack_commands.CONTEXT_CHANNEL
            notice = (
                slack_commands.subagent_starting_in_dm_notice(self._bot_display_name())
                if subagent else slack_commands.ask_answering_in_dm_notice(self._bot_display_name())
            )
            if not await acknowledge(notice):
                await answer(notice)
            # Posting to a user ID opens the bot DM without the im:write scope.
            target = invocation.user_id
        if self._client is None:
            await answer(slack_commands.ASK_NOT_POSTED_NOTICE)
            return "failed:no-client"
        command_label = slack_commands.command_label(
            self._slash_command(), skill, subagent
        )
        try:
            if direct:
                anchor = slack_commands.anchor_text(question, command_label)
            else:
                anchor = slack_commands.side_anchor_text(
                    question, channel_id, context,
                    self._thread_permalink(channel_id, thread_ts),
                    command=command_label,
                )
            posted = await self._client.chat_postMessage(
                channel=target, text=anchor, unfurl_links=False, unfurl_media=False,
            )
        except Exception as exc:  # noqa: BLE001
            code = slack_error_code(exc)
            if direct and code in _NOT_THE_BOTS_CONVERSATION:
                await answer(slack_commands.ask_needs_dm_text(
                    self._slash_command(), bot_name=self._bot_display_name()
                ))
                return f"refused:ask-outside-the-bot-dm ({code})"
            self._log.warning(
                "Slack %s could not %s: %s: %s",
                self._slash_command(),
                "post the question" if direct else "open the DM answer",
                invocation.identity(),
                slack_failure_reason(exc),
            )
            await answer(slack_commands.ASK_NOT_POSTED_NOTICE)
            return f"failed:anchor-not-posted ({code or type(exc).__name__})"
        dm_channel = (
            channel_id if direct else str((posted or {}).get("channel") or "").strip()
        )
        anchor_ts = str((posted or {}).get("ts") or "").strip()
        if not dm_channel or not anchor_ts:
            await answer(slack_commands.ask_not_dispatched_notice(self._bot_display_name()))
            return "failed:anchor-without-ts"

        refusal = await self._required_items_refusal_in_dm(
            invocation, dm_channel, anchor_ts, skill, subagent, answer
        )
        if refusal:
            return refusal
        if direct:
            return await self._dispatch_dm_ask(
                invocation, dm_channel, anchor_ts, question,
                text=question, skill=skill, subagent=subagent, answer=answer,
            )
        session_id = f"slack_{invocation.team_id}_{dm_channel}_{anchor_ts}"
        if source:
            forked = await self._fork_side_session(invocation, source, session_id)
            if forked:
                session_id = forked
            else:
                context = slack_commands.CONTEXT_NONE
                await self._rewrite_anchor(
                    dm_channel,
                    anchor_ts,
                    slack_commands.side_anchor_text(
                        question,
                        channel_id,
                        slack_commands.CONTEXT_NONE,
                        command=command_label,
                    ),
                )
        self._remember_side_session(invocation.team_id, dm_channel, anchor_ts, session_id)

        return await self._dispatch_dm_ask(
            invocation, dm_channel, anchor_ts, question,
            text=slack_commands.side_question_text(
                question, channel_id, command_label
            ),
            skill=skill, subagent=subagent, answer=answer,
            context=context, clear_session_extensions=True,
        )

    async def _dispatch_dm_ask(
        self,
        invocation: Invocation,
        dm_channel: str,
        anchor_ts: str,
        question: str,
        *,
        text: str,
        skill: str,
        subagent: str,
        answer: Callable[[str], Any],
        context: str = "",
        clear_session_extensions: bool = False,
    ) -> str:
        """Dispatch a posted DM anchor through the normal message path."""
        outcome = await self._dispatch_anchor(
            invocation, dm_channel, anchor_ts, question,
            text=text, skill=skill, subagent=subagent,
            clear_session_extensions=clear_session_extensions,
        )
        if outcome.startswith(("dispatched:", "queued:", "steered:")):
            return f"asked:{context} {outcome}" if context else f"asked:{outcome}"
        if not outcome.startswith("refused:"):
            await answer(
                slack_commands.ask_not_dispatched_notice(self._bot_display_name())
            )
        return f"failed:{outcome}"

    async def _rewrite_anchor(self, channel_id: str, ts: str, text: str) -> None:
        """Correct the DM message that opened a side conversation. Best effort."""
        if self._client is None:
            return
        try:
            await self._client.chat_update(channel=channel_id, ts=ts, text=text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Slack could not correct a %s message: %s",
                self._slash_command(),
                slack_failure_reason(exc),
            )

    def _thread_permalink(self, channel_id: str, thread_ts: str) -> str:
        """Build a thread link from the workspace host, if known."""
        if not (self._identity.host and channel_id and thread_ts):
            return ""
        return (
            f"https://{self._identity.host}/archives/{channel_id}"
            f"/p{thread_ts.replace('.', '')}"
        )
