"""Questions and approvals as Mattermost buttons, and what a click on a button of Belle's becomes.

A question is a post with one message attachment, and one button per
option. A button's ``context`` names the question's record and the option
by its index, never the option's value, and holds the listener's secret of
this start. A click reaches Belle as a request from the server, which names
the person who clicked. The host decides whether that person may answer or
stop. The person is told what became of the click privately, through the
click's own ephemeral answer, and an answered or stale question loses its
buttons through the click's ``update``.
"""

import logging
from collections import OrderedDict
from collections.abc import Callable, Mapping
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
from belle_agent.platforms.mattermost.actions import ActionListener
from belle_agent.platforms.mattermost.capabilities import NAME
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError, created_post_id
from belle_agent.tools.host import APPROVE, REJECT

logger = logging.getLogger(__name__)

#: What a button's context names: an option of a question, or the stop of a turn.
KIND_ANSWER = "answer"
KIND_STOP = "stop"
#: Questions posted and still waiting for a click. The oldest is forgotten first.
MAX_SHOWN_QUESTIONS = 32
_STYLES = {APPROVE: "primary", REJECT: "danger"}

NO_PERSON_NOTICE = "This click names no person, so it answers nothing."
NO_OPTION_NOTICE = "This button names no option of the question."
NOT_SERVING_NOTICE = "Belle is not taking clicks right now. Try again in a moment."


@dataclass(frozen=True)
class _Shown:
    """One question on screen, and what its buttons mean."""

    question: Question
    conversation: str
    workspace: str
    labels: tuple[str, ...]


def closed(message: str, note: str) -> dict[str, Any]:
    """The ``update`` that keeps a question's text and puts a note in place of its buttons."""
    return {"message": message, "props": {"attachments": [{"text": note}]}}


class Questions:
    """The questions one connector posted, and the clicks on its buttons."""

    def __init__(
        self, client: MattermostClient, listener: ActionListener, on_inbound: Callable[[], OnInbound | None]
    ) -> None:
        self._client = client
        self._listener = listener
        self._on_inbound = on_inbound
        self._shown: OrderedDict[str, _Shown] = OrderedDict()

    async def ask(self, conversation: str, thread: str, question: Question, *, workspace: str) -> str:
        """Post a question with one button per option. Return its post id. A question with no option is refused."""
        if not question.options:
            raise QuestionRefused("a question with no options needs a typed answer, which a button cannot give")
        approval = question.kind == APPROVAL
        labels = tuple(APPROVAL_LABELS.get(option, option) if approval else option for option in question.options)
        actions = [
            {
                "id": f"option{index}",
                "name": label,
                "type": "button",
                "style": _STYLES.get(option, "default") if approval else "default",
                "integration": self._listener.integration(kind=KIND_ANSWER, record=question.id, option=index),
            }
            for index, (option, label) in enumerate(zip(question.options, labels, strict=True))
        ]
        posted = await self._client.request(
            "POST",
            "/posts",
            json={"channel_id": conversation, "root_id": thread, "message": question.text,
                  "props": {"attachments": [{"actions": actions}]}},
        )
        post_id = created_post_id(posted)
        while len(self._shown) >= MAX_SHOWN_QUESTIONS:
            self._shown.popitem(last=False)
        self._shown[question.id] = _Shown(question, conversation, workspace, labels)
        logger.info("mattermost question posted: question=%s channel=%s post=%s", question.id, conversation, post_id)
        return post_id

    def stop_button(self, label: str, *, workspace: str, message_id: str) -> dict[str, Any]:
        """A button that asks to stop the turn of one message."""
        return {
            "id": "stop",
            "name": label,
            "type": "button",
            "style": "danger",
            "integration": self._listener.integration(kind=KIND_STOP, workspace=workspace, message=message_id),
        }

    async def on_click(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Answer a click as the server applies it. Log what became of it."""
        answer, outcome = await self.click(request)
        logger.info(
            "mattermost click: post=%s channel=%s user=%s outcome=%s",
            request.get("post_id") or "-", request.get("channel_id") or "-", request.get("user_id") or "-", outcome,
        )
        return answer

    async def click(self, request: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        """What the server is to apply for a click, and what became of the click."""
        sender = str(request.get("user_id") or "").strip()
        if not sender:
            # Nobody can be held to a rule, so nobody's click is taken: the guard fails closed.
            return {"ephemeral_text": NO_PERSON_NOTICE}, "refused:no-user"
        context = self._listener.context(request)
        if context is None:
            # A button of an earlier start, whose question or turn this process never knew. Its kind only
            # chooses the words: nothing else of the context is read.
            raw = request.get("context")
            stop = isinstance(raw, Mapping) and raw.get("kind") == KIND_STOP
            return await self._withdrawn(request, stop=stop), "ignored:button-of-another-start"
        on_inbound = self._on_inbound()
        if on_inbound is None:
            return {"ephemeral_text": NOT_SERVING_NOTICE}, "dropped:platform-not-started"
        conversation = str(request.get("channel_id") or "")
        if context.get("kind") == KIND_STOP:
            outcome = await on_inbound(
                InboundStop(
                    platform=NAME, workspace=str(context.get("workspace") or ""), conversation=conversation,
                    message_id=str(context.get("message") or ""), sender=sender,
                )
            )
            return {"ephemeral_text": stop_notice(outcome)[0]}, outcome
        shown = self._shown.get(str(context.get("record") or ""))
        if shown is None:
            return await self._withdrawn(request, stop=False), "ignored:question-not-waiting"
        index = context.get("option")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(shown.labels):
            return {"ephemeral_text": NO_OPTION_NOTICE}, "refused:no-such-option"
        value = shown.question.options[index]
        outcome = await on_inbound(
            InboundAnswer(
                platform=NAME,
                workspace=shown.workspace,
                conversation=conversation,
                question_id=shown.question.id,
                sender=sender,
                answer=Answer(values=(value,), approved=value == APPROVE if shown.question.kind == APPROVAL else None),
            )
        )
        if outcome.startswith("answered"):
            self._shown.pop(shown.question.id, None)
            who = str(request.get("user_name") or "")
            note = answered_note(shown.labels[index], f"@{who}" if who else "")
            return {"update": closed(shown.question.text, note)}, outcome
        if outcome.startswith("ignored:question-not-waiting"):
            if conversation != shown.conversation:
                # Not the post the question was asked on: that one keeps its buttons.
                return {"ephemeral_text": WITHDRAWN_QUESTION_NOTE}, outcome
            # A stale click must not answer a question twice. Its buttons come off.
            self._shown.pop(shown.question.id, None)
            return {"update": closed(shown.question.text, WITHDRAWN_QUESTION_NOTE),
                    "ephemeral_text": WITHDRAWN_QUESTION_NOTE}, outcome
        if outcome.startswith("refused"):
            # The question stays open for somebody who may answer it.
            return {"ephemeral_text": ANSWER_REFUSED_NOTICE}, outcome
        return {"ephemeral_text": NOT_SERVING_NOTICE}, outcome

    async def _withdrawn(self, request: Mapping[str, Any], *, stop: bool) -> dict[str, Any]:
        """Take the buttons off a post that nothing waits on any more, and tell the person who clicked."""
        notice = STOP_STALE_NOTICE if stop else WITHDRAWN_QUESTION_NOTE
        try:
            post = await self._client.request("GET", f"/posts/{request.get('post_id') or '-'}")
        except MattermostError as exc:
            logger.warning("mattermost could not read a clicked post to take its buttons off: %s", exc)
            return {"ephemeral_text": notice}
        message = str((post if isinstance(post, Mapping) else {}).get("message") or "")
        # A turn's card keeps its text. A question also says that it no longer waits.
        update = {"message": message, "props": {}} if stop else closed(message, notice)
        return {"update": update, "ephemeral_text": notice}
