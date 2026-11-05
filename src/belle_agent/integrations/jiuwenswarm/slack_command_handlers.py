"""Slash commands and private task dispatch for JiuwenSwarm's Slack channel."""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from jiuwenswarm.extensions.sdk.host_types import Message, ReqMethod

from belle_agent.integrations.jiuwenswarm.config import configured_models, slack_agent_request_params
from belle_agent.integrations.jiuwenswarm.slack_routing import slack_session_id
from belle_agent.models import ConfiguredModels
from belle_agent.platforms.slack import command_ui
from belle_agent.platforms.slack import commands as slack_commands
from belle_agent.platforms.slack.admission import SlackQueuedMessage as _SlackQueuedMessage
from belle_agent.platforms.slack.identity import CHAT_TYPE_IM
from belle_agent.scopes import CLICK_QUEUE_DROP, SESSION_CHANNEL, click_rule

logger = logging.getLogger("belle_agent.integrations.jiuwenswarm.slack_channel")

_SLASH_AGENT_TIMEOUT_SECONDS = 30.0
_STATUS_AGENT_TIMEOUT_SECONDS = 8.0
_MAX_SIDE_SESSIONS = 512
# The runtime request behind each catalog a command reads: its method, the reply field and its parameters.
_CATALOG_REQUESTS: dict[str, tuple[ReqMethod, str, dict[str, Any] | None]] = {
    "skills": (ReqMethod.SKILLS_LIST, "skills", None),
    "experts": (ReqMethod.AGENT_TEMPLATES_LIST, "templates", {"filter": "local"}),
    "subagents": (ReqMethod.SUBAGENTS_LIST, "subagents", None),
}


class SlackCommandsMixin(command_ui.SlackCommandInteractions):
    """Answer slash commands using the host channel's transport and turn state."""

    def _channel_model_name(
        self, channel_id: str, user_id: str = "", chat_type: str = ""
    ) -> str:
        """Resolve the scoped or temporary model for this conversation."""
        scoped_name = self._settled(channel_id, user_id, chat_type).model_name or ""
        key = (self._identity.team_id, channel_id)
        name = scoped_name or getattr(self, "_model_overrides", {}).get(key, "")
        if not name:
            return ""

        known = configured_models()
        if not known.known or known.accepts(name):
            return name

        if not scoped_name:
            self._model_overrides.pop(key, None)
            logger.warning(
                "channels.slack: temporary model %r is no longer configured", name
            )
            return ""

        reported_key = (channel_id, name)
        if reported_key not in self._reported_missing_models:
            self._reported_missing_models.add(reported_key)
            logger.warning(
                "channels.slack: the model pinned for %s (%r) is no longer in"
                " models.defaults (%s); it was there when the channel was"
                " configured, so a model has been renamed or removed since."
                " That channel runs on %s until the setting -- a scope's"
                " agent.model_name -- or the model list is corrected",
                channel_id,
                name,
                known.describe(),
                known.describe_fallback(),
            )
        return ""

    async def _slash_models(self) -> ConfiguredModels:
        """The models a command may name."""
        return configured_models()

    def _slash_scoped_model(
        self, invocation: command_ui.Invocation, chat_type: str
    ) -> str:
        """The model a scope selects for this person here, or ``""``."""
        return self._settled(
            invocation.channel_id, invocation.user_id, chat_type,
            workspace=invocation.team_id,
        ).model_name or ""

    async def _stop_turn(
        self,
        session_id: str,
        entry: Any,
        invocation: command_ui.Invocation,
        ground: str,
    ) -> bool:
        """Stop the turn a command chose. ``False`` when it had ended first."""
        request_id = self._activity_request_aliases.get(
            entry.request_id, entry.request_id
        )
        record = await self._claim_stop_for_turn(
            session_id, entry, invocation.channel_id
        )
        if record is None:
            return False

        logger.info(
            "[SlackChannel] stopping a turn through %s: session_id=%s "
            "request_id=%s user=%s ground=%s",
            self._slash_command(),
            session_id,
            request_id,
            invocation.user_id,
            ground,
        )
        await self._dispatch_stop(
            {"team_id": invocation.team_id},
            record,
            session_id=session_id,
            user_id=invocation.user_id,
        )
        await self._close_stopped_turn(record, session_id, request_id)
        return True

    def _may_drop_queued(
        self, invocation: command_ui.Invocation, entry: _SlackQueuedMessage
    ) -> bool:
        if invocation.user_id == entry.user_id:
            return True
        rule = click_rule(
            self.config.scopes,
            channel="slack",
            chat=invocation.channel_id,
            kind=CLICK_QUEUE_DROP,
        )
        return rule is not None and rule.permits(invocation.user_id)

    async def _answer_status(
        self,
        invocation: command_ui.Invocation,
        answer: Callable[[str], Any],
    ) -> str:
        """Show runtime health and this conversation's turn state."""
        started = time.monotonic()
        reply = await self._request_agent(
            ReqMethod.COMMAND_STATUS,
            invocation,
            params={"action": "overview"},
            timeout=_STATUS_AGENT_TIMEOUT_SECONDS,
        )
        elapsed = time.monotonic() - started
        payload = reply.payload if reply is not None and reply.ok else None
        payload = payload if isinstance(payload, Mapping) else {}
        facts = self._status_facts(
            invocation,
            agent_answered=reply is not None and reply.ok,
            agent_seconds=elapsed,
            version=str(payload.get("version") or ""),
            model=str(payload.get("model") or ""),
            provider=str(payload.get("provider") or ""),
        )
        text = slack_commands.status_text(
            facts, self._slash_command(), bot_name=self._bot_display_name()
        )
        await answer(text)
        return "answered:status" + ("" if facts.agent_answered else " agent-silent")

    def _conversation_queue(
        self, invocation: command_ui.Invocation
    ) -> list[_SlackQueuedMessage]:
        """Waiting messages in this workspace and channel, across its threads."""
        entries = (
            entry
            for queue in self._admission.queued_messages.values()
            for entry in queue
            if entry.team_id == invocation.team_id
            and entry.channel_id == invocation.channel_id
        )
        return sorted(entries, key=lambda entry: entry.queued_at)

    async def _drop_queued(
        self, found: _SlackQueuedMessage, invocation: command_ui.Invocation
    ) -> bool:
        """Take a waiting message out of its queue. ``False`` when it had left first."""
        queue = self._admission.queued_messages.get(found.session_id, [])
        if found not in queue:
            return False
        queue.remove(found)
        if not queue:
            self._admission.queued_messages.pop(found.session_id, None)
        await self._unmark_dropped(
            found,
            queued_behind=any(
                entry.root_thread_ts == found.root_thread_ts
                for entry in self._conversation_queue(invocation)
            ),
            running=any(
                initiator.channel_id == found.channel_id
                and initiator.root_thread_ts == found.root_thread_ts
                for initiator in self._admission.turn_initiators.values()
            ),
        )
        return True

    def _slash_scoped_params(
        self, channel_id: str, user_id: str, chat_type: str, workspace: str
    ) -> dict[str, str | list[str]]:
        """What the scopes settle for one person in one conversation."""
        return slack_agent_request_params(
            self.config, channel_id, user_id=user_id,
            chat_type=chat_type, workspace=workspace,
        )

    async def _slash_catalog(
        self,
        invocation: command_ui.Invocation,
        kind: str,
    ) -> tuple[list[Any] | None, dict[str, str | list[str]], bool]:
        """Ask the runtime for one catalog: ``skills``, ``experts`` or ``subagents``."""
        method, field, params = _CATALOG_REQUESTS[kind]
        chat_type = self._conversation_chat_type(invocation.channel_id)
        scoped = self._slash_scoped_params(
            invocation.channel_id, invocation.user_id, chat_type, self._identity.team_id
        )
        reply = await self._request_agent(
            method, invocation, chat_type=chat_type, params=params
        )
        payload = reply.payload if reply is not None else None
        catalog = payload.get(field) if isinstance(payload, Mapping) else None
        valid = reply is not None and reply.ok and isinstance(catalog, list)
        return catalog if valid else None, scoped, reply is None

    async def _request_agent(
        self,
        method: ReqMethod,
        invocation: command_ui.Invocation,
        *,
        chat_type: str = "",
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Message | None:
        """Wait for a non-streamed runtime reply without posting it to Slack."""
        if timeout is None:
            timeout = _SLASH_AGENT_TIMEOUT_SECONDS
        request_id = f"slack-cmd-{uuid.uuid4().hex}"
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._agent_replies[request_id] = future
        metadata = {
            "user_id": invocation.user_id,
            "slack_team_id": invocation.team_id,
            "slack_channel_id": invocation.channel_id,
            "slack_user_id": invocation.user_id,
        }
        if chat_type:
            metadata["slack_channel_type"] = chat_type
        req = self._request(
            request_id,
            session_id=None,
            chat_id=invocation.channel_id,
            user_id=invocation.user_id,
            req_method=method,
            params=dict(params or {}),
            metadata=metadata,
        )
        try:
            await self._route_request(req)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            logger.warning(
                "Slack %s request %s had no reply within %.0fs: %s",
                method.value,
                request_id,
                timeout,
                invocation.identity(),
            )
            return None
        finally:
            self._agent_replies.pop(request_id, None)

    def _settle_agent_reply(self, msg: Message) -> bool:
        """Settle a command reply and consume late or progress events."""
        request_id = str(getattr(msg, "id", "") or "")
        if not request_id.startswith("slack-cmd-"):
            return False
        future = self._agent_replies.get(request_id)
        if future is not None and msg.type == "res" and not future.done():
            future.set_result(msg)
        return True

    async def _dispatch_anchor(
        self,
        invocation: command_ui.Invocation,
        dm_channel: str,
        anchor_ts: str,
        question: str,
        *,
        text: str,
        skill: str,
        subagent: str,
        clear_session_extensions: bool,
    ) -> str:
        """Admit a posted DM anchor as the message it stands for. Return the admission outcome."""
        return await self._handle_slack_event(
            {
                "type": "message",
                "channel_type": CHAT_TYPE_IM,
                "channel": dm_channel,
                "user": invocation.user_id,
                "text": text,
                "ts": anchor_ts,
            },
            {
                "team_id": invocation.team_id,
                "event_id": f"slack-ask-{dm_channel}-{anchor_ts}",
            },
            is_dm=True,
            trigger="dm",
            required_skills=(skill,) if skill else (),
            required_subagent=(subagent, question) if subagent else None,
            clear_session_extensions=clear_session_extensions,
        )

    def _channel_source_session(
        self, invocation: command_ui.Invocation
    ) -> tuple[str, str]:
        """Return the channel session and thread to fork, if known."""
        channel_id = invocation.channel_id
        chat_type = self._conversation_chat_type(channel_id)
        if self._session_scope(channel_id, invocation.user_id, chat_type) == SESSION_CHANNEL:
            channel_wide = slack_session_id(
                team_id=invocation.team_id,
                channel_id=channel_id,
                user_id=invocation.user_id,
                root_thread_ts="",
                is_dm=False,
                channel_wide=True,
            )
            return channel_wide, ""
        return self._channel_sessions.get(channel_id, ("", ""))

    def _note_channel_session(
        self, channel_id: str, session_id: str, root_thread_ts: str
    ) -> None:
        """Record the session a channel message is about to start a turn in."""
        if channel_id and session_id:
            self._channel_sessions[channel_id] = (session_id, root_thread_ts)

    def _side_session(self, team_id: str, channel_id: str, root_thread_ts: str) -> str:
        """The side conversation a DM thread belongs to, or ``""``."""
        if not root_thread_ts:
            return ""
        return self._side_sessions.get((team_id, channel_id, root_thread_ts), "")

    def _remember_side_session(
        self, team_id: str, channel_id: str, root_thread_ts: str, session_id: str
    ) -> None:
        """Route a DM thread's replies to ``session_id``, forgetting the oldest past the cap."""
        self._side_sessions[(team_id, channel_id, root_thread_ts)] = session_id
        while len(self._side_sessions) > _MAX_SIDE_SESSIONS:
            self._side_sessions.pop(next(iter(self._side_sessions)))

    async def _fork_side_session(
        self,
        invocation: command_ui.Invocation,
        source: str,
        target: str,
    ) -> str:
        """Fork ``source`` into a side conversation named ``target``; its id, or ``""``."""
        reply = await self._request_agent(
            ReqMethod.SESSION_FORK,
            invocation,
            params={
                "source_session_id": source,
                "target_session_id": target,
                "title": (
                    f"{self._slash_command()} {slack_commands.ASK.name}"
                    f" from {invocation.channel_id}"
                ),
                "session_equipment_override": {
                    "agent_template_name": "",
                    "plugin_names": [],
                    "mcp": [],
                },
            },
        )
        payload = reply.payload if reply is not None else None
        forked = (
            str(payload.get("session_id") or "").strip()
            if reply is not None and reply.ok and isinstance(payload, Mapping)
            else ""
        )
        if not forked:
            logger.warning(
                "Slack %s could not fork %s, answering without its context: %s: %s",
                self._slash_command(),
                source,
                invocation.identity(),
                (payload or {}).get("error") if isinstance(payload, Mapping) else "no reply",
            )
        return forked
