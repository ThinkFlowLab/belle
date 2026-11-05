"""The slash command of the Slack connector on the platform port.

``SlackCommandInteractions`` holds the command as Slack shows it: the parsing,
the answers, the pickers and the forms. It asks its class what runs and what
waits, who may stop or remove it, what can be listed, and how a private
question is admitted. The JiuwenSwarm channel answers from its harness. This
class answers from the conversation host, through the operations of
``belle_agent.host.commands`` and the records of the platform port, so the
command is the same on every backend. What a backend does not provide is
answered with a sentence that says so.

A stop and a removal are handed to the host as records. The host decides who
may, by one rule for the typed command, the picker, the activity card's
button and Slack's own button.
"""

import functools
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from belle_agent.backend import CATALOG_EXPERTS, TurnScope
from belle_agent.commands import grammar as command_grammar
from belle_agent.models import ConfiguredModels
from belle_agent.platforms.base import TRIGGER_DM, TRIGGER_NONE, InboundDrop, InboundMessage
from belle_agent.platforms.slack import commands as slack_commands
from belle_agent.platforms.slack import task_forms as slack_task_forms
from belle_agent.platforms.slack.admission import SlackQueuedMessage, SlackTurnInitiator
from belle_agent.platforms.slack.command_ui import (
    Invocation,
    SlackCommandInteractions,
)
from belle_agent.platforms.slack.identity import CHAT_TYPE_IM, chat_type_from_chat_id
from belle_agent.platforms.slack.scope_policy import command_token, resolve_bot_name
from belle_agent.scheduling.commands import schedule_command
from belle_agent.scheduling.session_commands import session_command

_NAME = "slack"
_ANY_COMMAND = re.compile(r".*")
# Private questions asked from elsewhere, each a thread of a direct message. The oldest is forgotten first.
MAX_SIDE_CONVERSATIONS = 512


def other_command_notice(command: str) -> str:
    """Answer a slash command of the app that is not the one this deployment serves."""
    return f"This deployment answers `{command}`. Type `{command} help` to see what it can do."


class SlackConnectorCommands(SlackCommandInteractions):
    """The slash command, answered from the conversation host."""

    #: The host, as ``belle_agent.host.commands`` describes it. Given before the connector starts.
    _host: Any = None
    #: The scopes Slack says the app holds, or ``None`` where it did not say.
    _granted_scopes: frozenset[str] | None = None
    # This host copies no conversation into a private question, so one asked from a channel starts without it.
    _ask_context_unsourced = slack_commands.CONTEXT_FRESH

    def bind(self, host: Any) -> None:
        self._host = host

    def _register_commands(self, app: Any) -> None:
        super()._register_commands(app)
        # A manifest can declare a command under another name than the configured one. It is answered too.
        app.command(_ANY_COMMAND)(self._on_other_command)

    async def _on_other_command(self, ack: Any) -> None:
        await ack(other_command_notice(self._slash_command()))

    # ── what the command module reads of its class ───────────────────────

    @functools.cached_property
    def _slash_task_forms(self) -> dict[str, slack_task_forms.TaskForm]:
        return {}

    @functools.cached_property
    def _side_threads(self) -> dict[tuple[str, str], str]:
        """The thread of a direct message that each private question asked from elsewhere lives in."""
        return {}

    def _serving(self) -> bool:
        return self._on_inbound is not None

    def _slash_command(self) -> str:
        return command_token(self.config.command_name)

    def _bot_display_name(self) -> str:
        return resolve_bot_name(self.config.bot_name)

    def is_allowed(self, user_id: str) -> bool:
        return self._host.admits(user_id)

    def _channel_is_allowed(self, channel_id: str) -> bool:
        return self._host.serves(channel_id)

    def _conversation_chat_type(self, channel_id: str) -> str:
        return self._chat_type(channel_id)

    def _at(self, channel_id: str, user_id: str, chat_type: str = "", workspace: str = "") -> InboundMessage:
        """Name a person and a place to the host, as a message of theirs there would."""
        direct = chat_type_from_chat_id(channel_id) == CHAT_TYPE_IM
        return InboundMessage(
            platform=_NAME,
            workspace=workspace or self._identity.team_id,
            conversation=channel_id,
            message_id="",
            sender=user_id,
            text="",
            chat_type=CHAT_TYPE_IM if direct else chat_type or self._chat_type(channel_id),
            trigger=TRIGGER_DM if direct else TRIGGER_NONE,
        )

    def _unprovided(self, sub: slack_commands.Subcommand) -> str:
        return self._host.command_unavailable(sub.words)

    async def _run_schedule_command(
        self, sub: slack_commands.Subcommand, invocation: Invocation, arguments: str, answer: Callable[[str], Any]
    ) -> str:
        at = self._at(invocation.channel_id, invocation.user_id, workspace=invocation.team_id)
        text, outcome = await schedule_command(
            self._host.schedule_toolkit, at, sub, arguments, self._slash_command()
        )
        await answer(text)
        return outcome

    async def _run_session_command(
        self, sub: slack_commands.Subcommand, invocation: Invocation, arguments: str, answer: Callable[[str], Any]
    ) -> str:
        at = replace(
            self._at(invocation.channel_id, invocation.user_id, workspace=invocation.team_id),
            event_id=invocation.event_id,
        )
        text, outcome = await session_command(
            self._host.session_toolkit, at, sub, arguments, self._slash_command()
        )
        await answer(text)
        return outcome

    # ── status ───────────────────────────────────────────────────────────

    async def _answer_status(self, invocation: Invocation, answer: Callable[[str], Any]) -> str:
        """Show the backend's health and this conversation's turns."""
        at = self._at(invocation.channel_id, invocation.user_id)
        capabilities = self._host.backend_capabilities
        started = time.monotonic()
        catalog = await self._host.catalog()
        elapsed = time.monotonic() - started
        running = self._host.running(invocation.team_id, invocation.channel_id)
        event_running, pending_events, lost_events = self._host.events_of(invocation.team_id, invocation.channel_id)
        link = ""
        if len(running) == 1:
            link = self._thread_permalink(invocation.channel_id, running[0].inbound.message_id)
        facts = slack_commands.StatusFacts(
            agent_answered=catalog is not None,
            agent_seconds=elapsed,
            version=" ".join(part for part in (capabilities.backend, capabilities.version) if part),
            model=catalog.models.default if catalog is not None else "",
            model_override=self._host.settled(at).model,
            chat_type=at.chat_type,
            mid_turn=self._host.mid_turn(at),
            running=len(running),
            event_running=event_running,
            running_message_link=link,
            pending_events=pending_events,
            lost_events=lost_events,
            queued=len(self._host.waiting(invocation.team_id, invocation.channel_id)),
            held_calls=self._host.held(invocation.team_id, invocation.channel_id),
            ask_context=slack_commands.CONTEXT_FRESH,
        )
        await answer(slack_commands.status_text(facts, self._slash_command(), bot_name=self._bot_display_name()))
        return "answered:status" + ("" if facts.agent_answered else " agent-silent")

    # ── stop ─────────────────────────────────────────────────────────────

    def _initiator(self, turn: Any) -> SlackTurnInitiator:
        """A held turn in the shape the command module reads a running turn in."""
        started = turn.started_at or turn.queued_at
        return replace(
            self._origin(turn.inbound), request_id=turn.request_id, preview_text=turn.inbound.text, started_at=started
        )

    def _slash_stop_candidates(self, invocation: Invocation, target_ts: str) -> list[tuple[str, SlackTurnInitiator]]:
        return [
            (turn.conversation, self._initiator(turn))
            for turn in self._host.running(invocation.team_id, invocation.channel_id)
            if not target_ts or turn.inbound.message_id == target_ts
        ]

    def _may_stop(self, conversation: str, user_id: str, channel_id: str = "") -> tuple[bool, str]:
        for turn in self._host.running(self._identity.team_id, channel_id):
            if turn.conversation == conversation:
                return self._host.may_stop(turn, user_id)
        return False, "no-turn-running"

    async def _stop_turn(self, conversation: str, entry: SlackTurnInitiator, invocation: Invocation, ground: str) -> bool:
        """Ask for the stop the way the card's button and Slack's own button do."""
        outcome = await self._request_stop(
            invocation.team_id, invocation.channel_id, entry.message_ts, invocation.user_id
        )
        return outcome.startswith("stopped")

    # ── the queue ────────────────────────────────────────────────────────

    def _prune_queued_messages(self) -> None:
        """The host keeps the queue, and expires what waits too long when it admits a message."""

    def _conversation_queue(self, invocation: Invocation) -> list[SlackQueuedMessage]:
        entries = []
        for turn in self._host.waiting(invocation.team_id, invocation.channel_id):
            origin = self._origin(turn.inbound)
            entries.append(
                SlackQueuedMessage(
                    request=SimpleNamespace(id=turn.request_id),
                    session_id=turn.conversation,
                    user_id=origin.user_id,
                    is_dm=origin.is_dm,
                    chat_type=origin.chat_type,
                    team_id=turn.inbound.workspace,
                    channel_id=origin.channel_id,
                    message_ts=origin.message_ts,
                    reply_thread_ts=origin.reply_thread_ts,
                    root_thread_ts=origin.root_thread_ts,
                    queued_at=turn.queued_at,
                    preview_text=turn.inbound.text,
                )
            )
        return entries

    def _may_drop_queued(self, invocation: Invocation, entry: SlackQueuedMessage) -> bool:
        for turn in self._host.waiting(invocation.team_id, invocation.channel_id):
            if turn.inbound.message_id == entry.message_ts:
                return self._host.may_drop(turn, invocation.user_id)[0]
        return False

    async def _drop_queued(self, found: SlackQueuedMessage, invocation: Invocation) -> bool:
        outcome = await self._on_inbound(
            InboundDrop(
                platform=_NAME,
                workspace=invocation.team_id,
                conversation=invocation.channel_id,
                message_id=found.message_ts,
                sender=invocation.user_id,
            )
        )
        return outcome.startswith("removed:")

    # ── catalogs and models ──────────────────────────────────────────────

    def _slash_scoped_params(self, channel_id: str, user_id: str, chat_type: str, workspace: str) -> dict[str, Any]:
        """What the scopes settle there, under the names the command module reads them by."""
        scope: TurnScope = self._host.settled(self._at(channel_id, user_id, chat_type, workspace))
        params: dict[str, Any] = {
            "agent_template_name": scope.expert,
            "agent_skills_required": list(scope.skills_required),
        }
        if scope.skills_available is not None:
            params["agent_skills_available"] = sorted(scope.skills_available)
        if scope.skills_unavailable:
            params["agent_skills_unavailable"] = sorted(scope.skills_unavailable)
        if scope.subagents_available is not None:
            params["agent_subagents_available"] = sorted(scope.subagents_available)
        return params

    async def _slash_catalog(self, invocation: Invocation, kind: str) -> tuple[list[Any] | None, dict[str, Any], bool]:
        scoped = self._slash_scoped_params(
            invocation.channel_id, invocation.user_id, self._chat_type(invocation.channel_id), invocation.team_id
        )
        catalog = await self._host.catalog()
        if catalog is None:
            return None, scoped, True
        if kind == CATALOG_EXPERTS:
            # A backend lists the experts a turn can run as. The command module reads an installed package.
            return [{"id": name, "installed": True, "connection_state": "connected"} for name in catalog.experts], scoped, False
        return list(getattr(catalog, kind)), scoped, False

    def _catalog_caveat(self, kind: str, scoped: Mapping[str, Any]) -> str:
        """Say that the backend does not hold a turn to the list a scope wrote, where it does not."""
        # A field narrows the list where its parameter is set.
        narrowed = {field for field, _ in command_grammar.CATALOG_FILTERS[kind] if f"agent_{field}" in scoped}
        return command_grammar.catalog_caveat(kind, narrowed, self._host.backend_capabilities)

    async def _slash_models(self) -> ConfiguredModels:
        catalog = await self._host.catalog()
        return catalog.models if catalog is not None else ConfiguredModels()

    def _slash_scoped_model(self, invocation: Invocation, chat_type: str) -> str:
        return self._host.scoped_model(self._at(invocation.channel_id, invocation.user_id, chat_type, invocation.team_id))

    @property
    def _model_overrides(self) -> dict[tuple[str, str], str]:
        return self._host.model_choices

    # ── a private question ───────────────────────────────────────────────

    def _channel_source_session(self, invocation: Invocation) -> tuple[str, str]:
        """No conversation is copied into a private question: neither port has an operation that copies one."""
        return "", ""

    def _remember_side_session(self, team_id: str, channel_id: str, root_thread_ts: str, session_id: str) -> None:
        """Keep a private question, and the replies in its thread, in a conversation of their own."""
        self._side_threads[(channel_id, root_thread_ts)] = root_thread_ts
        while len(self._side_threads) > MAX_SIDE_CONVERSATIONS:
            del self._side_threads[next(iter(self._side_threads))]

    async def _dispatch_anchor(
        self,
        invocation: Invocation,
        dm_channel: str,
        anchor_ts: str,
        question: str,
        *,
        text: str,
        skill: str,
        subagent: str,
        clear_session_extensions: bool,
    ) -> str:
        """Hand the posted anchor to the host as a direct message of the person who asked."""
        return await self._deliver(
            {"type": "message", "channel_type": CHAT_TYPE_IM, "channel": dm_channel, "user": invocation.user_id, "ts": anchor_ts},
            {"team_id": invocation.team_id, "event_id": f"slack-ask-{dm_channel}-{anchor_ts}"},
            TRIGGER_DM,
            text,
            skills_required=(skill,) if skill else (),
            subagent_required=(subagent, question) if subagent else None,
        )
