"""Ask Matrix questions with MSC3381 polls and handle their votes.

Element displays these unstable poll events. Votes follow room encryption.
Reactions do not, so reactions cannot answer questions. The host checks each
voter and option index. The bot closes the poll and replies without a
notification. A vote on an unknown poll withdraws that poll.
"""

import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from belle_agent.backend import APPROVAL, Answer, Question
from belle_agent.click_notices import (
    ANSWER_REFUSED_NOTICE,
    APPROVAL_LABELS,
    WITHDRAWN_QUESTION_NOTE,
    answered_note,
)
from belle_agent.platforms.base import InboundAnswer, OnInbound, QuestionRefused
from belle_agent.platforms.matrix import render
from belle_agent.platforms.matrix.capabilities import NAME
from belle_agent.platforms.matrix.helper import HelperError
from belle_agent.tools.host import APPROVE

logger = logging.getLogger(__name__)

# Unstable event names supported by Element 1.12.
POLL_START = "org.matrix.msc3381.poll.start"
POLL_RESPONSE = "org.matrix.msc3381.poll.response"
POLL_END = "org.matrix.msc3381.poll.end"
POLL_DISCLOSED = "org.matrix.msc3381.poll.disclosed"
TEXT = "org.matrix.msc1767.text"
#: MSC3381's maximum options per poll.
MAX_OPTIONS = 20
#: Maximum pending questions; the oldest expires first.
MAX_SHOWN_QUESTIONS = 32


class Account(Protocol):
    """What the questions need of the connector."""

    user_id: str

    @property
    def workspace(self) -> str:
        """The account's name: the workspace of each of its conversations."""
        ...

    async def call(self, op: str, **fields: Any) -> dict[str, Any]:
        """One request of the helper. Raise ``HelperError`` where it refuses."""
        ...

    async def send_event(self, conversation: str, event_type: str, content: Mapping[str, Any]) -> str:
        """Send one event, and return its id."""
        ...


@dataclass(frozen=True)
class _Shown:
    """One question on screen, and the label of each option."""

    question: Question
    room: str
    thread: str
    labels: tuple[str, ...]


def _thread_of(content: Mapping[str, Any]) -> str:
    relates = content.get("m.relates_to")
    if isinstance(relates, Mapping) and relates.get("rel_type") == "m.thread":
        return str(relates.get("event_id") or "")
    return ""


class Questions:
    """The polls one connector asked, and the votes on them."""

    def __init__(self, account: Account) -> None:
        self._account = account
        self._shown: OrderedDict[str, _Shown] = OrderedDict()
        #: Each poll and voter already told that their vote does not count.
        self._told: set[tuple[str, str]] = set()

    async def ask(self, conversation: str, thread: str, question: Question) -> str:
        """Send a poll with one answer per option. Return its event id.

        A question with no option, or with more than ``MAX_OPTIONS``, is refused: a poll cannot hold it.
        """
        if not question.options:
            raise QuestionRefused("a question with no options needs a typed answer, which a poll cannot give")
        if len(question.options) > MAX_OPTIONS:
            raise QuestionRefused(
                f"a Matrix poll holds at most {MAX_OPTIONS} options, and this question has {len(question.options)}"
            )
        labels = tuple(
            APPROVAL_LABELS.get(option, option) if question.kind == APPROVAL else option for option in question.options
        )
        fallback = question.text + "".join(f"\n{index}. {label}" for index, label in enumerate(labels, 1))
        content: dict[str, Any] = {
            POLL_START: {
                "question": {TEXT: question.text, "body": question.text, "msgtype": "m.text"},
                "kind": POLL_DISCLOSED,
                "max_selections": 1,
                "answers": [{"id": str(index), TEXT: label} for index, label in enumerate(labels)],
            },
            TEXT: fallback,
        }
        if thread:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread}
        poll = await self._account.send_event(conversation, POLL_START, content)
        while len(self._shown) >= MAX_SHOWN_QUESTIONS:
            self._shown.popitem(last=False)
        self._shown[poll] = _Shown(question, conversation, thread, labels)
        logger.info("matrix question asked: question=%s room=%s poll=%s", question.id, conversation, poll)
        return poll

    async def vote(self, room: str, event: Mapping[str, Any], on_inbound: OnInbound | None) -> str:
        """Hand a vote on one of the bot's polls to the host, and show what became of it. Return the outcome."""
        content = event.get("content")
        content = content if isinstance(content, Mapping) else {}
        relates = content.get("m.relates_to")
        poll = str(relates.get("event_id") or "") if isinstance(relates, Mapping) else ""
        sender = str(event.get("sender") or "")
        response = content.get(POLL_RESPONSE)
        answers = response.get("answers") if isinstance(response, Mapping) else None
        if not poll or not sender or not isinstance(answers, list):
            return "ignored:malformed-vote"
        shown = self._shown.get(poll)
        if shown is None or shown.room != room:
            # A vote relates to an event of its own room only, so a poll of another room is not the one asked.
            return await self._stale(room, poll)
        if not answers:
            return "ignored:vote-withdrawn"  # Element sends no answer when a person takes their vote back.
        index = int(answers[0]) if str(answers[0]).isdigit() else -1
        if not 0 <= index < len(shown.labels):
            return "refused:no-such-option"
        if on_inbound is None:
            return "dropped:platform-not-started"
        value = shown.question.options[index]
        approved = value == APPROVE if shown.question.kind == APPROVAL else None
        outcome = await on_inbound(
            InboundAnswer(
                platform=NAME, workspace=self._account.workspace, conversation=room, question_id=shown.question.id, sender=sender,
                answer=Answer(values=(value,), approved=approved),
            )
        )
        if outcome.startswith("answered"):
            self._shown.pop(poll, None)
            await self._close(room, poll, shown.thread, answered_note(shown.labels[index], sender))
        elif outcome.startswith("ignored:question-not-waiting"):
            # A late vote must not answer a question twice. The poll ends.
            self._shown.pop(poll, None)
            await self._close(room, poll, shown.thread, WITHDRAWN_QUESTION_NOTE)
        elif outcome.startswith("refused") and (poll, sender) not in self._told:
            # The poll stays open for somebody who may answer it. Each voter is told once.
            self._told.add((poll, sender))
            await self._note(room, poll, shown.thread, f"{sender}: {ANSWER_REFUSED_NOTICE}")
        return outcome

    async def _stale(self, room: str, poll: str) -> str:
        """End a poll of the bot's that no record holds, unless it has ended already. A poll of anybody else is left alone."""
        try:
            found = await self._account.call("event", room=room, event_id=poll)
        except HelperError as exc:
            logger.warning("matrix could not read the poll %s of a vote: %s", poll, exc)
            return "ignored:poll-unreadable"
        start = found["room_event"]
        if start.get("type") != POLL_START or start.get("sender") != self._account.user_id:
            return "ignored:not-a-poll-of-this-bot"
        ended = await self._account.call("relations", room=room, event_id=poll, rel_type="m.reference", limit=50)
        if any((item.get("room_event") or {}).get("type") == POLL_END and (item.get("room_event") or {}).get("sender")
               == self._account.user_id for item in ended["chunk"]):
            return "ignored:question-not-waiting"  # Its poll ended already: nothing is sent again.
        content = start.get("content")
        await self._close(room, poll, _thread_of(content if isinstance(content, Mapping) else {}), WITHDRAWN_QUESTION_NOTE)
        return "ignored:question-not-waiting"

    async def _close(self, room: str, poll: str, thread: str, note: str) -> None:
        """End a poll, so nobody votes on it again, and say why in a reply to it."""
        try:
            await self._account.send_event(
                room, POLL_END, {"m.relates_to": {"rel_type": "m.reference", "event_id": poll}, POLL_END: {}, TEXT: note}
            )
        except HelperError as exc:
            logger.warning("matrix could not end the poll %s: %s", poll, exc)
        await self._note(room, poll, thread, note)

    async def _note(self, room: str, poll: str, thread: str, note: str) -> None:
        """A notice in reply to a poll, in its thread, that notifies nobody."""
        try:
            await self._account.send_event(room, "m.room.message", render.note(note, poll, thread))
        except HelperError as exc:
            logger.warning("matrix could not reply to the poll %s: %s", poll, exc)

