"""Questions and approvals as Discord components, and what a click on one of Belle's becomes.

A question is a message with buttons, one per option, where it has at most five options; with more it has
one string select. A question with no option has one button that opens a form (a modal) with a text field.
A component's ``custom_id`` names this start, the question's record and the option by its index, never the
option's value. A click reaches Belle as an ``INTERACTION_CREATE`` over the gateway, which names the person
who clicked; it is acknowledged before the host is asked, as Discord wants within 3 seconds. The host
decides whether that person may answer or stop. The person is told what became of the click by an
ephemeral followup, and an answered or stale question loses its components.
"""

import asyncio
import contextlib
import itertools
import logging
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from belle_agent.backend import APPROVAL, Answer, Question
from belle_agent.click_notices import (
    ANSWER_REFUSED_NOTICE,
    APPROVAL_LABELS,
    STOP_STALE_NOTICE,
    WITHDRAWN_QUESTION_NOTE,
    answered_note,
    stop_notice,
)
from belle_agent.platforms.base import InboundAnswer, InboundStop, OnInbound, QuestionRefused
from belle_agent.platforms.discord.affordances import MESSAGE_LIMIT, QUESTION_OPTIONS_MAX
from belle_agent.platforms.discord.capabilities import NAME
from belle_agent.platforms.discord.client import DiscordClient, DiscordError
from belle_agent.platforms.discord.render import RENDERER
from belle_agent.tools.host import APPROVE, REJECT

logger = logging.getLogger(__name__)

#: Component types, button styles, interaction types and callback types, as Discord numbers them.
ACTION_ROW, BUTTON, STRING_SELECT, TEXT_INPUT = 1, 2, 3, 4
PRIMARY, SECONDARY, DANGER = 1, 2, 4
INTERACTION_COMMAND, INTERACTION_COMPONENT, INTERACTION_MODAL = 2, 3, 5
DEFERRED_UPDATE, MODAL = 6, 9
#: The flag of a message only the person who interacted sees.
EPHEMERAL = 64
#: Discord's limits (``QUESTION_OPTIONS_MAX``, the options of a select, in ``affordances``): buttons in one action row, characters of a button's label and of
#: a select option's label, of a modal's title and a text input's label, and of a text input's value.
BUTTONS_MAX = 5
BUTTON_LABEL_MAX = 80
OPTION_LABEL_MAX = 100
TEXT_ANSWER_MAX = 4000
#: What a component's ``custom_id`` starts with, then this start's nonce, the kind and its fields.
PREFIX = "belle"
KIND_ANSWER, KIND_SELECT, KIND_TEXT, KIND_MODAL, KIND_STOP = "a", "s", "t", "m", "x"
_STYLES = {APPROVE: PRIMARY, REJECT: DANGER}
TEXT_BUTTON_LABEL = "Answer"
MODAL_TITLE = "Your answer"
TEXT_INPUT_ID = "answer"

NO_PERSON_NOTICE = "This click names no person, so it answers nothing."
NO_OPTION_NOTICE = "This component names no option of the question."
NOT_SERVING_NOTICE = "Belle is not taking clicks right now. Try again in a moment."

#: The conversation and chat type of a channel or thread, as the connector knows it.
ConversationOf = Callable[[str], Awaitable[tuple[str, str]]]


@dataclass(frozen=True)
class _Shown:
    """One question on screen, and what its components mean."""

    question: Question
    conversation: str
    workspace: str
    labels: tuple[str, ...]
    channel: str
    message: str


def _row(*components: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": ACTION_ROW, "components": list(components)}


def closed(note: str) -> dict[str, Any]:
    """The edit that keeps a question's text, takes its components off and shows ``note`` beneath it."""
    return {"components": [], "embeds": [{"description": note}]}


class Questions:
    """The questions one connector posted, and the clicks on their components."""

    def __init__(
        self,
        client: DiscordClient,
        application_id: Callable[[], str],
        conversation_of: ConversationOf,
        on_inbound: Callable[[], OnInbound | None],
        *,
        workspace: Callable[[str], str] = lambda guild: guild,
        pending_questions_max: int = 256,
        question_expiry_seconds: int = 86400,
    ) -> None:
        self._workspace = workspace
        self._client = client
        self._application_id = application_id
        self._conversation_of = conversation_of
        self._on_inbound = on_inbound
        self._pending_questions_max = pending_questions_max
        self._question_expiry_seconds = question_expiry_seconds
        #: Names this start in every ``custom_id``, so a component of an earlier start is told apart.
        self.nonce = secrets.token_hex(4)
        self._keys = itertools.count(1)
        #: Each question waiting, by the key its components name.
        self._shown: OrderedDict[str, _Shown] = OrderedDict()
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}

    def _id(self, kind: str, *fields: object) -> str:
        return ":".join((PREFIX, self.nonce, kind, *map(str, fields)))

    def components(self, key: str, question: Question) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
        """The action rows of a question and the label of each option. Refuse what Discord cannot show."""
        if not question.options:
            return [_row({"type": BUTTON, "style": PRIMARY, "label": TEXT_BUTTON_LABEL,
                          "custom_id": self._id(KIND_TEXT, key)})], ()
        approval = question.kind == APPROVAL
        labels = tuple(APPROVAL_LABELS.get(option, option) if approval else option for option in question.options)
        if len(labels) > QUESTION_OPTIONS_MAX:
            raise QuestionRefused(
                f"a Discord question offers at most {QUESTION_OPTIONS_MAX} options (one select); this one has {len(labels)}"
            )
        for index, label in enumerate(labels):
            if not label.strip() or len(label) > OPTION_LABEL_MAX:
                raise QuestionRefused(
                    f"option {index} of a Discord question must be 1 to {OPTION_LABEL_MAX} characters; it has {len(label)}"
                )
        if len(labels) <= BUTTONS_MAX and all(len(label) <= BUTTON_LABEL_MAX for label in labels):
            buttons = [
                {"type": BUTTON, "label": label, "custom_id": self._id(KIND_ANSWER, key, index),
                 "style": _STYLES.get(option, SECONDARY) if approval else SECONDARY}
                for index, (option, label) in enumerate(zip(question.options, labels, strict=True))
            ]
            return [_row(*buttons)], labels
        select = {"type": STRING_SELECT, "custom_id": self._id(KIND_SELECT, key), "min_values": 1, "max_values": 1,
                  "options": [{"label": label, "value": str(index)} for index, label in enumerate(labels)]}
        return [_row(select)], labels

    async def ask(self, conversation: str, channel: str, question: Question, *, workspace: str) -> str:
        """Post a question in ``channel``, its components on its last message. Return that message's id."""
        key = str(next(self._keys))
        rows, labels = self.components(key, question)
        # A message may hold components alone, so a question with no text is its components.
        payloads = [dict(part.payload) for part in RENDERER.parts(question.text, MESSAGE_LIMIT)] if question.text.strip() else []
        payloads = payloads or [{}]
        payloads[-1]["components"] = rows
        message_id = ""
        for payload in payloads:
            posted = await self._client.mapping("POST", f"/channels/{channel}/messages", json=payload)
            message_id = str(posted.get("id") or "")
            if not message_id:
                raise DiscordError("bad_response", "POST", f"/channels/{channel}/messages")
        self._shown[key] = _Shown(question, conversation, workspace, labels, channel, message_id)
        self._expiry_tasks[key] = asyncio.create_task(self._expire(key))
        try:
            while len(self._shown) > self._pending_questions_max:
                oldest = next(iter(self._shown))
                await self._close_shown(oldest)
        except asyncio.CancelledError:
            await self._close_shown(key)
            raise
        logger.info("discord question posted: question=%s channel=%s message=%s", question.id, channel, message_id)
        return message_id

    async def close(self) -> None:
        """Remove pending controls and cancel expiry timers."""
        keys = list(self._shown)
        for task in self._expiry_tasks.values():
            task.cancel()
        tasks, self._expiry_tasks = list(self._expiry_tasks.values()), {}
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for key in keys:
            try:
                await self._close_shown(key)
            except Exception:
                logger.exception("discord could not remove question controls during shutdown")

    async def withdraw(self, question_id: str) -> None:
        """Forget one host question and remove its controls, even if Discord refuses the edit."""
        key = next((key for key, shown in self._shown.items() if shown.question.id == question_id), None)
        if key is not None:
            await self._close_shown(key)

    async def _expire(self, key: str) -> None:
        await asyncio.sleep(self._question_expiry_seconds)
        await self._close_shown(key)

    async def _close_shown(self, key: str) -> None:
        shown = self._shown.pop(key, None)
        task = self._expiry_tasks.pop(key, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if shown is None:
            return
        try:
            await self._client.mapping("PATCH", f"/channels/{shown.channel}/messages/{shown.message}",
                                       json={"components": []})
        except DiscordError as exc:
            logger.warning("discord could not remove stale question controls: %s", exc)

    def stop_button(self, label: str, *, message_id: str) -> dict[str, Any]:
        """A button that asks to stop the turn of one message."""
        return {"type": BUTTON, "style": DANGER, "label": label, "custom_id": self._id(KIND_STOP, message_id)}

    def owns(self, interaction: Mapping[str, Any]) -> bool:
        """Whether an interaction is a click on a component or a form of Belle's."""
        data = interaction.get("data")
        custom_id = str(data.get("custom_id") or "") if isinstance(data, Mapping) else ""
        return interaction.get("type") in (INTERACTION_COMPONENT, INTERACTION_MODAL) and custom_id.startswith(f"{PREFIX}:")

    async def on_interaction(self, interaction: Mapping[str, Any]) -> str:
        """Acknowledge a click or a form, hand it to the host, and tell the person. Log what became of it."""
        outcome = await self._interaction(interaction)
        data = interaction.get("data")
        logger.info(
            "discord click: %s channel=%s user=%s outcome=%s",
            (str(data.get("custom_id") or "-") if isinstance(data, Mapping) else "-"),
            interaction.get("channel_id") or "-", _person(interaction)[0] or "-", outcome,
        )
        return outcome

    async def _interaction(self, interaction: Mapping[str, Any]) -> str:
        data = interaction.get("data")
        data = data if isinstance(data, Mapping) else {}
        _, nonce, kind, *fields = [*str(data.get("custom_id") or "").split(":"), "", "", ""][:5]
        fields = [field for field in fields if field]
        current = nonce == self.nonce
        shown = self._shown.get(fields[0]) if current and fields and kind != KIND_STOP else None
        # The form is the acknowledgement of the button that opens it. Every other click is acknowledged first.
        if kind == KIND_TEXT and shown is not None and self._on_inbound() is not None:
            modal = {"custom_id": self._id(KIND_MODAL, fields[0]), "title": MODAL_TITLE, "components": [_row({
                "type": TEXT_INPUT, "custom_id": TEXT_INPUT_ID, "style": 2, "label": MODAL_TITLE,
                "required": True, "max_length": TEXT_ANSWER_MAX,
            })]}
            return await self._respond(interaction, {"type": MODAL, "data": modal}) or "asked:text-form-opened"
        if failed := await self._respond(interaction, {"type": DEFERRED_UPDATE}):
            return failed
        sender, who = _person(interaction)
        if not sender:
            # Nobody can be held to a rule, so nobody's click is taken: the guard fails closed.
            await self._tell(interaction, NO_PERSON_NOTICE)
            return "refused:no-user"
        if not current:
            # A component of an earlier start, whose question or turn this process never knew.
            await self._withdraw(interaction, stop=kind == KIND_STOP)
            return "ignored:button-of-another-start"
        on_inbound = self._on_inbound()
        if on_inbound is None:
            await self._tell(interaction, NOT_SERVING_NOTICE)
            return "dropped:platform-not-started"
        conversation, _ = await self._conversation_of(str(interaction.get("channel_id") or ""))
        # The guild the click came from, as Discord states it: the host holds an answer to the question's own.
        workspace = self._workspace(str(interaction.get("guild_id") or "") or "direct")
        if kind == KIND_STOP:
            outcome = await on_inbound(InboundStop(
                platform=NAME, workspace=workspace, conversation=conversation,
                message_id=fields[0] if fields else "", sender=sender,
            ))
            if not outcome.startswith(("stopped", "refused")):
                await self._edit(interaction, {"components": []})
            await self._tell(interaction, stop_notice(outcome)[0])
            return outcome
        if shown is None:
            await self._withdraw(interaction, stop=False)
            return "ignored:question-not-waiting"
        index, value = _chosen(kind, fields, data, shown)
        if value is None:
            await self._tell(interaction, NO_OPTION_NOTICE)
            return "refused:no-such-option"
        question = shown.question
        outcome = await on_inbound(InboundAnswer(
            platform=NAME, workspace=workspace, conversation=conversation, question_id=question.id, sender=sender,
            answer=Answer(values=(value,), approved=value == APPROVE if question.kind == APPROVAL else None),
        ))
        key = fields[0]
        if outcome.startswith("answered"):
            self._forget(key)
            label = shown.labels[index] if index is not None else value
            await self._edit(interaction, closed(answered_note(label, f"@{who}" if who else "")))
            return outcome
        if outcome.startswith("ignored:question-not-waiting"):
            if (conversation, workspace) == (shown.conversation, shown.workspace):
                # A stale click must not answer a question twice. Its components come off.
                self._forget(key)
                await self._edit(interaction, closed(WITHDRAWN_QUESTION_NOTE))
            await self._tell(interaction, WITHDRAWN_QUESTION_NOTE)
            return outcome
        # Refused: the question stays open for somebody who may answer it.
        await self._tell(interaction, ANSWER_REFUSED_NOTICE if outcome.startswith("refused") else NOT_SERVING_NOTICE)
        return outcome

    async def _respond(self, interaction: Mapping[str, Any], body: Mapping[str, Any]) -> str:
        """Acknowledge an interaction. Return the outcome where Discord refused it, else empty."""
        try:
            await self._client.respond(str(interaction.get("id") or ""), str(interaction.get("token") or ""), body)
        except DiscordError as exc:
            # Past 3 seconds the person already sees the click as failed, and may click again.
            logger.warning("discord interaction not acknowledged: %s", exc)
            return f"dropped:not-acknowledged ({exc.code})"
        return ""

    async def _tell(self, interaction: Mapping[str, Any], text: str) -> None:
        """Tell the person who clicked, and nobody else."""
        try:
            await self._client.followup(self._application_id(), str(interaction.get("token") or ""),
                                        {"content": text, "flags": EPHEMERAL, "allowed_mentions": {"parse": []}})
        except DiscordError as exc:
            logger.warning("discord could not tell the person who clicked: %s", exc)

    async def _edit(self, interaction: Mapping[str, Any], body: Mapping[str, Any]) -> None:
        try:
            await self._client.edit_response(self._application_id(), str(interaction.get("token") or ""), body)
        except DiscordError as exc:
            logger.warning("discord could not take the components off a clicked message: %s", exc)

    def _forget(self, key: str) -> None:
        self._shown.pop(key, None)
        task = self._expiry_tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def _withdraw(self, interaction: Mapping[str, Any], *, stop: bool) -> None:
        """Take the components off a message nothing waits on any more, and tell the person who clicked."""
        # A turn's card keeps what it shows. A question also says that it no longer waits.
        await self._edit(interaction, {"components": []} if stop else closed(WITHDRAWN_QUESTION_NOTE))
        await self._tell(interaction, STOP_STALE_NOTICE if stop else WITHDRAWN_QUESTION_NOTE)


def _person(interaction: Mapping[str, Any]) -> tuple[str, str]:
    """The id and the shown name of the person who interacted: ``member.user`` in a guild, ``user`` in a DM."""
    member = interaction.get("member")
    user = member.get("user") if isinstance(member, Mapping) else interaction.get("user")
    user = user if isinstance(user, Mapping) else {}
    return str(user.get("id") or ""), str(user.get("global_name") or user.get("username") or "")


def _chosen(kind: str, fields: list[str], data: Mapping[str, Any], shown: _Shown) -> tuple[int | None, str | None]:
    """The option index and value a click chose, or the text a form gave. ``None`` where it names none."""
    if kind == KIND_MODAL:
        text = _text_value(data.get("components"))
        return (None, text) if text and not shown.question.options else (None, None)
    raw = fields[1] if kind == KIND_ANSWER and len(fields) > 1 else ""
    if kind == KIND_SELECT:
        values = data.get("values")
        raw = str(values[0]) if isinstance(values, list) and len(values) == 1 else ""
    if not raw.isdigit() or not 0 <= int(raw) < len(shown.question.options):
        return None, None
    return int(raw), shown.question.options[int(raw)]


def _text_value(components: Any) -> str:
    """The value of the form's text input, wherever its rows nest it."""
    for component in components if isinstance(components, list) else ():
        if not isinstance(component, Mapping):
            continue
        if component.get("custom_id") == TEXT_INPUT_ID and isinstance(component.get("value"), str):
            return str(component["value"])
        nested = component.get("components") or ([component["component"]] if "component" in component else [])
        if found := _text_value(nested):
            return found
    return ""
