"""Run Belle's turns on an ACP v1 agent, through the Unix socket of the relay in front of it."""

import asyncio
import base64
import logging
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from acp import RequestError
from acp.schema import (
    AgentCapabilities,
    AllowedOutcome,
    AudioContentBlock,
    BlobResourceContents,
    DeniedOutcome,
    EmbeddedResourceContentBlock,
    HttpHeader,
    HttpMcpServer,
    ImageContentBlock,
    PermissionOption,
    PromptCapabilities,
    RequestPermissionResponse,
    TextContentBlock,
    TextResourceContents,
    ToolCallUpdate,
)
from pydantic import ValidationError

from belle_agent.backend import (
    APPROVAL,
    CONVERSATION_UNAVAILABLE,
    DUPLICATE,
    EVENT_ACCEPTED,
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    EVENT_QUESTION,
    EVENT_STEP,
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    OP_ACTIVE,
    OP_ANSWER,
    OP_CANCEL,
    OP_CONTINUE,
    OP_PENDING,
    OP_RECOVER,
    RECOVERY_NOT_SUBMITTED,
    RECOVERY_RUNNING,
    RECOVERY_TERMINAL,
    Answer,
    BackendCapabilities,
    BackendError,
    Catalog,
    PeekHistoryPart,
    Question,
    Recovery,
    ToolHost,
    TurnEvent,
    TurnRequest,
    read_data_uri,
)
from belle_agent.integrations.acp.link import AcpError, Link, open_link
from belle_agent.integrations.acp.settings import MOUNT_NONE, MOUNT_SESSION, PER_CONVERSATION, Settings
from belle_agent.integrations.acp.state import State, terminal
from belle_agent.integrations.acp.turns import TurnText, translate
from belle_agent.tools.host import APPROVE, REJECT, approves
from belle_agent.tools.server import ToolServer

logger = logging.getLogger(__name__)

#: The name of Belle's tool server in a session mount.
MOUNT_NAME = "belle"
ALREADY_RUNNING = "A turn is already running in this conversation."
ALREADY_RAN = "This request has already run in this conversation."
STREAM_LOST = "The ACP connection closed before the turn ended, so its outcome is unknown."
UNRECORDED = "Belle keeps no record of this request, so its outcome is unknown."
UNRESOLVED = (
    "The agent did not end the turn within cancel_timeout_seconds of session/cancel, so its outcome is unknown."
)
NOT_RECORDED = "Belle could not record the request before submitting it, so it submitted nothing"
READER_LEFT = "The turn's reader left, so Belle cancelled the turn."

_CANCELLED = DeniedOutcome(outcome="cancelled")
#: The ``reject_once`` or ``session/cancel`` that came first in a turn, for a ``cancelled`` stop reason.
_REJECT, _CANCEL = "reject", "cancel"


class _Unavailable(Exception):
    """The conversation's session is gone and the request needs it."""


@dataclass
class _Held:
    """A permission request that waits for a person."""

    question: Question
    allow: str
    reject: str
    link: Link
    rpc_id: Any
    answer: asyncio.Future[Any]


@dataclass
class _Turn:
    conversation: str
    request_id: str
    #: Opens the turn's connection and session. A stop before the prompt cancels it.
    setup: asyncio.Task[tuple[Link, str]]
    withhold_final: bool = False
    queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    link: Link | None = None
    session: str = ""
    prompt: asyncio.Task[Any] | None = None
    #: Set once the SDK has written the turn's ``session/prompt``.
    submitted: asyncio.Event = field(default_factory=asyncio.Event)
    text: TurnText = field(default_factory=TurnText)
    held: dict[str, _Held] = field(default_factory=dict)
    asked: int = 0
    cancelled: bool = False
    first_stop: str = ""
    #: Set when ``session/cancel`` did not end the turn in time.
    unresolved: asyncio.Event = field(default_factory=asyncio.Event)
    ended: TurnEvent | None = None

    @property
    def accepts_actions(self) -> bool:
        """Whether this submitted prompt can still authorize a tool or permission."""
        return (
            self.submitted.is_set()
            and not self.cancelled
            and self.ended is None
            and self.prompt is not None
            and not self.prompt.done()
            and self.link is not None
            and self.link.alive
        )


def _key(conversation: str, request_id: str) -> str:
    return f"{conversation} {request_id}"


def _refusal(exc: RequestError) -> str:
    """The code and message of an agent's error. Its data can echo a request, whose mounts hold the tool token."""
    return f"error {exc.code}: {exc}"


def _unsupported(method: str) -> RequestError:
    return RequestError(
        -32601, "Method not found", {"method": method, "details": "Belle offers no file system and no terminal"}
    )


class _Callbacks:
    """The client side of one connection: what the agent may ask Belle."""

    def __init__(self, backend: "AcpBackend", link: Link) -> None:
        self._backend, self._link = backend, link

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        await self._backend._update(self._link, session_id, update)

    async def request_permission(
        self, options: list[PermissionOption], session_id: str, tool_call: ToolCallUpdate, **kwargs: Any
    ) -> RequestPermissionResponse:
        rpc_id = self._link.permission_ids.pop((session_id, tool_call.tool_call_id), None)
        return await self._backend._permission(self._link, rpc_id, session_id, options, tool_call)

    # Belle advertises no client capability, so an agent that calls one gets a precise error and nothing runs.
    async def read_text_file(self, **kwargs: Any) -> None:
        raise _unsupported("fs/read_text_file")

    async def write_text_file(self, **kwargs: Any) -> None:
        raise _unsupported("fs/write_text_file")

    async def create_terminal(self, **kwargs: Any) -> None:
        raise _unsupported("terminal/create")

    async def terminal_output(self, **kwargs: Any) -> None:
        raise _unsupported("terminal/output")

    async def release_terminal(self, **kwargs: Any) -> None:
        raise _unsupported("terminal/release")

    async def wait_for_terminal_exit(self, **kwargs: Any) -> None:
        raise _unsupported("terminal/wait_for_exit")

    async def kill_terminal(self, **kwargs: Any) -> None:
        raise _unsupported("terminal/kill")


class AcpBackend:
    """``belle_agent.backend.Backend`` on one ACP v1 agent behind a relay socket, as one profile describes it.

    Each conversation is one ACP session. ``state_dir`` keeps each conversation's session and the requests that
    ran, across restarts.
    """

    def __init__(self, settings: Settings, state_dir: Path) -> None:
        self._settings = settings
        self._profile = settings.profile
        self._state = State(state_dir / f"acp_{self._profile.name}.json", self._profile.remembered_requests)
        #: Open connections: one under ``""`` when shared, else one per conversation.
        self._links: dict[str, Link] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        #: Conversation to its running turn, and (connection, session id) to its submitted turn.
        self._turns: dict[str, _Turn] = {}
        self._live: dict[tuple[Link, str], _Turn] = {}
        #: The binding line of a per-conversation connection, to its conversation.
        self._bindings: dict[str, str] = {}
        self._agent_capabilities = AgentCapabilities()
        self._agent = ""
        self._tool_server: ToolServer | None = None
        self._tools_url = ""

    def capabilities(self) -> BackendCapabilities:
        """Report what ACP gives on every harness. ACP enforces no field of a turn's scope."""
        return BackendCapabilities(
            backend="acp",
            operations=frozenset({OP_CANCEL, OP_ANSWER, OP_PENDING, OP_ACTIVE, OP_RECOVER, OP_CONTINUE}),
            tool_name_format=self._profile.tool_name_format,
            reports=frozenset({EVENT_TEXT_DELTA, EVENT_STEP, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT}),
            version=self._agent,
        )

    async def start(self, tools: ToolHost) -> None:
        """Serve the tools, read the state, and initialize the agent once. Each failure raises an :class:`AcpError`."""
        if self._profile.connection == PER_CONVERSATION and self._profile.binding_meta:
            raise AcpError(
                "binding.meta needs connection: shared; use binding.header with connection: per_conversation"
            )
        try:
            if self._profile.tools_mount != MOUNT_NONE:
                await self._serve_tools(tools)
            self._state.load()
            link = await self._open("")
            if self._profile.connection == PER_CONVERSATION:
                await link.close()
            else:
                self._links[""] = link
            mcp = self._agent_capabilities.mcp_capabilities
            if self._profile.tools_mount == MOUNT_SESSION and not (mcp and mcp.http):
                raise AcpError(
                    f"profile {self._profile.name} has tools_mount: {MOUNT_SESSION}, and the agent at"
                    f" {self._profile.socket} does not advertise mcpCapabilities.http"
                )
            logger.info(
                "acp: %s at %s, serving tools at %s",
                self._agent or "agent",
                self._profile.socket,
                self._tools_url or "no address: the profile offers the agent no tool",
            )
            for loss in self._losses():
                logger.warning("acp: %s", loss)
        except BaseException:
            await self.close()
            raise

    def _losses(self) -> list[str]:
        """What this agent and profile do not give, each with what a conversation loses by it."""
        caps = self._agent_capabilities
        takes = caps.prompt_capabilities or PromptCapabilities()
        agent = f"the agent at {self._profile.socket}"
        losses = ["ACP reports no usage and offers no health check"]
        if not (caps.load_session or (caps.session_capabilities and caps.session_capabilities.resume)):
            losses.append(
                f"{agent} advertises neither session/resume nor session/load,"
                " so a conversation loses its earlier context when Belle restarts"
            )
        if not takes.embedded_context:
            kept = [kind for kind, taken in (("image", takes.image), ("audio", takes.audio)) if taken]
            losses.append(
                f"{agent} takes {' and '.join(kept)} files only, so Belle refuses a message with any other file"
                if kept
                else f"{agent} takes no file, so Belle refuses a message with a file"
            )
        if self._profile.connection == PER_CONVERSATION:
            losses.append(
                f"profile {self._profile.name} opens one agent process per conversation, and no idle limit ends one:"
                " each runs until Belle stops"
            )
        return losses

    async def _serve_tools(self, tools: ToolHost) -> None:
        settings = self._settings
        self._tool_server = ToolServer(
            tools,
            self._conversation_of,
            host=settings.tools_bind,
            port=settings.tools_port,
            advertised_url=settings.tools_url,
            token=settings.tools_token,
        )
        try:
            await self._tool_server.start()
        except OSError as exc:
            raise AcpError(
                f"the tool server cannot listen on {settings.tools_bind}:{settings.tools_port}: {exc}"
            ) from exc
        self._tools_url = self._tool_server.url

    async def _open(self, conversation: str) -> Link:
        """Open a connection. A per-conversation one sends a new binding line for ``conversation`` first."""
        binding = secrets.token_urlsafe(16) if self._profile.connection == PER_CONVERSATION else ""
        link = await open_link(
            self._profile.socket,
            lambda link: _Callbacks(self, link),
            binding=binding,
            timeout=self._profile.start_timeout,
        )
        link.on_cancel_request = lambda rpc_id: self._withdrawn(link, rpc_id)
        link.on_prompt_sent = lambda session: self._prompt_sent(link, session)
        if binding and conversation:
            self._bindings[binding] = conversation
        self._agent_capabilities, self._agent = link.capabilities, link.agent
        return link

    async def _link(self, conversation: str) -> Link:
        """The connection of a conversation, opened again when it closed."""
        key = conversation if self._profile.connection == PER_CONVERSATION else ""
        async with self._opening.setdefault(key, asyncio.Lock()):
            link = self._links.get(key)
            if link is not None and link.alive:
                return link
            if link is not None:
                await self._drop(key, link)
            link = self._links[key] = await self._open(conversation)
            return link

    async def _drop(self, key: str, link: Link) -> None:
        """Close ``link``. Forget it under ``key`` only while it is still the open connection there."""
        if self._links.get(key) is link:
            del self._links[key]
        self._bindings.pop(link.binding, None)
        await link.close()

    def _mounts(self, link: Link) -> list[HttpMcpServer]:
        if self._profile.tools_mount != MOUNT_SESSION:
            return []
        assert self._tool_server is not None, "the backend is not started"
        headers = [HttpHeader(name="Authorization", value=f"Bearer {self._tool_server.token}")]
        if self._profile.binding_header:
            headers.append(HttpHeader(name=self._profile.binding_header, value=link.binding))
        # HttpMcpServer, not McpServerHttp: SDK 0.12.1 drops the latter from session/new without a word.
        return [HttpMcpServer(type="http", name=MOUNT_NAME, url=self._tools_url, headers=headers)]

    async def _attach(self, conversation: str, existing_only: bool) -> tuple[Link, str]:
        """Open the conversation's connection and session within ``start_timeout_seconds``."""
        async with asyncio.timeout(self._profile.start_timeout):
            link = await self._link(conversation)
            return link, await self._session(link, conversation, existing_only)

    async def _session(self, link: Link, conversation: str, existing_only: bool) -> str:
        """Open the conversation's session on ``link``: its own when the agent restores it, else a new one."""
        known = self._state.session(conversation)
        if known is not None and known in link.sessions:
            return known
        profile, caps = self._profile, link.capabilities
        if known is not None:
            resume = caps.session_capabilities is not None and caps.session_capabilities.resume is not None
            try:
                if resume:
                    await link.conn.resume_session(session_id=known, cwd=profile.cwd, mcp_servers=self._mounts(link))
                elif caps.load_session:
                    # The replay reaches no turn: the session is not live until its prompt is sent.
                    await link.conn.load_session(session_id=known, cwd=profile.cwd, mcp_servers=self._mounts(link))
                else:
                    raise RequestError(-32601, "the agent advertises neither session/resume nor session/load")
                link.sessions.add(known)
                return known
            except RequestError as exc:
                lost = _refusal(exc)
            if existing_only:
                raise _Unavailable(lost)
            logger.warning(
                "acp: the agent did not restore session %s, so conversation %s starts a new one without its earlier"
                " context: %s",
                known,
                conversation,
                lost,
            )
        elif existing_only:
            raise _Unavailable("Belle has no session of this conversation")
        session: str = (await link.conn.new_session(cwd=profile.cwd, mcp_servers=self._mounts(link))).session_id
        link.sessions.add(session)
        self._state.set_session(conversation, session)
        return session

    def _prompt(self, request: TurnRequest) -> list[Any]:
        """The prompt's content blocks. A file of a type the agent does not take raises ``ValueError``."""
        blocks: list[Any] = [TextContentBlock(type="text", text=request.text)]
        takes = self._agent_capabilities.prompt_capabilities or PromptCapabilities()
        for entry in request.files:
            name = str(entry.get("name") or "file")
            mime, content = read_data_uri(str(entry.get("uri") or ""))
            data = base64.b64encode(content).decode("ascii")
            family = mime.partition("/")[0]
            if family == "image" and takes.image:
                blocks.append(ImageContentBlock(type="image", data=data, mime_type=mime))
            elif family == "audio" and takes.audio:
                blocks.append(AudioContentBlock(type="audio", data=data, mime_type=mime))
            elif takes.embedded_context:
                uri = f"file:///{quote(name)}"
                try:
                    resource: Any = TextResourceContents(uri=uri, mime_type=mime, text=content.decode("utf-8"))
                except UnicodeDecodeError:
                    resource = BlobResourceContents(uri=uri, mime_type=mime, blob=data)
                blocks.append(EmbeddedResourceContentBlock(type="resource", resource=resource))
            else:
                raise ValueError(f"The agent takes no {mime} content in a prompt, so Belle sent nothing: {name}")
        return blocks

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        """Run one turn: one ``session/prompt`` on the conversation's session, and its updates to its end."""
        conversation = request.conversation
        key = _key(conversation, request.request_id)
        if self._state.request(key) is not None:
            yield TurnEvent(EVENT_FAILED, text=ALREADY_RAN, data={DUPLICATE: True})
            return
        if conversation in self._turns:
            yield TurnEvent(EVENT_FAILED, text=ALREADY_RUNNING)
            return
        try:
            blocks = self._prompt(request)
        except ValueError as exc:
            yield TurnEvent(EVENT_FAILED, text=str(exc))
            return
        try:
            self._state.admit(key)
        except OSError as exc:
            yield TurnEvent(EVENT_FAILED, text=f"{NOT_RECORDED}: {exc}")
            return
        setup = asyncio.create_task(self._attach(conversation, request.existing_conversation_only))
        turn = self._turns[conversation] = _Turn(conversation, request.request_id, setup, request.scope.withhold_final)
        try:
            async for event in self._drive(turn, blocks):
                yield event
        finally:
            await self._finish(turn)
        assert turn.ended is not None
        yield turn.ended

    async def _drive(self, turn: _Turn, blocks: list[Any]) -> AsyncIterator[TurnEvent]:
        """Submit the prompt and yield its progress. The turn's terminal event is left in ``turn.ended``."""
        await asyncio.wait({turn.setup})
        if turn.cancelled:
            turn.ended = TurnEvent(EVENT_INTERRUPTED)
            return
        try:
            turn.link, turn.session = turn.setup.result()
        except _Unavailable as exc:
            logger.warning("acp: conversation %s is unavailable: %s", turn.conversation, exc)
            turn.ended = TurnEvent(EVENT_FAILED, text=CONVERSATION_UNAVAILABLE)
            return
        except TimeoutError:
            turn.ended = TurnEvent(
                EVENT_FAILED,
                text=f"The ACP agent did not open the conversation's session within {self._profile.start_timeout:g}s"
                " (start_timeout_seconds)",
            )
            return
        except (AcpError, RequestError, OSError, ValidationError) as exc:
            # The agent refused, the connection failed, or the agent's answer was malformed.
            reason = f"{type(exc).__name__}: {exc}"
            turn.ended = TurnEvent(EVENT_FAILED, text=f"The turn could not start on the ACP agent: {reason}")
            return
        self._live[turn.link, turn.session] = turn
        turn.prompt = asyncio.create_task(turn.link.conn.prompt(session_id=turn.session, prompt=blocks))
        if await self._submitted(turn):
            # A stop from now on sends session/cancel after the prompt, so the agent reads it.
            yield TurnEvent(EVENT_ACCEPTED)
        unresolved = asyncio.create_task(turn.unresolved.wait())
        try:
            while True:
                getter = asyncio.create_task(turn.queue.get())
                await asyncio.wait({getter, turn.prompt, unresolved}, return_when=asyncio.FIRST_COMPLETED)
                if not getter.done():
                    getter.cancel()
                    await asyncio.gather(getter, return_exceptions=True)
                if getter.done() and not getter.cancelled():
                    for event in self._events(getter.result(), turn):
                        yield event
                    continue
                if unresolved.done():
                    turn.ended = TurnEvent(EVENT_FAILED, text=UNRESOLVED)
                    return
                # ClientSideConnection.prompt waits for every session update handler in flight
                # (_session_updates.wait) before it returns, and each handler puts its update in the unbounded
                # queue. So the queue holds every update of the turn by now.
                while not turn.queue.empty():
                    for event in self._events(turn.queue.get_nowait(), turn):
                        yield event
                turn.ended = self._ended(turn)
                return
        finally:
            unresolved.cancel()

    def _events(self, item: Any, turn: _Turn) -> list[TurnEvent]:
        return item if isinstance(item, list) else translate(item, turn.text)

    def _ended(self, turn: _Turn) -> TurnEvent:
        """The terminal event of a prompt that returned or failed."""
        assert turn.prompt is not None
        try:
            response = turn.prompt.result()
        except RequestError as exc:
            # The error's code names it. Its message and data stay out of the log.
            failed = {"failure_kind": "backend", "error_type": str(exc.code)}
            return TurnEvent(EVENT_FAILED, text=f"The agent failed the turn: {_refusal(exc)}", data=failed)
        # The connection closed, the agent ended, or its answer was malformed.
        except (OSError, ValidationError) as exc:
            logger.warning("acp: the prompt of conversation %s ended without an answer: %r", turn.conversation, exc)
            return TurnEvent(EVENT_FAILED, text=STREAM_LOST)
        stop = response.stop_reason
        if stop == "end_turn":
            return TurnEvent(EVENT_SUCCEEDED, text=turn.text.final)
        if stop == "cancelled" and turn.first_stop:
            # A person's stop, or their refusal of an action after which the agent ended the turn.
            return TurnEvent(EVENT_INTERRUPTED, data={"stop_reason": stop, "after": turn.first_stop})
        return TurnEvent(
            EVENT_FAILED,
            text=f"The agent ended the turn with stop reason {stop}",
            data={"stop_reason": stop, "error_type": stop},
        )

    async def _finish(self, turn: _Turn) -> None:
        """End what the turn holds: its held requests, a prompt still running, and its place in the maps."""
        if not turn.setup.done():
            turn.setup.cancel()
            await asyncio.gather(turn.setup, return_exceptions=True)
        self._answer_held(turn)
        prompt = turn.prompt
        unended = False
        if prompt is not None and not prompt.done():
            if not turn.unresolved.is_set():
                # The reader left before the end: the turn is stopped, as the contract asks.
                await self._stop(turn)
            unended = not prompt.done()
            prompt.cancel()
            await asyncio.gather(prompt, return_exceptions=True)
        # Only now: the stop needs the turn here to see its prompt written.
        if turn.link is not None:
            self._live.pop((turn.link, turn.session), None)
        # Before the turn's place is free, so that the conversation's next turn cannot take the connection.
        if unended:
            await self._leave(turn)
        elif turn.link is not None and not turn.link.alive:
            await self._drop(turn.conversation if self._profile.connection == PER_CONVERSATION else "", turn.link)
        if turn.ended is None:
            turn.ended = (
                TurnEvent(EVENT_FAILED, text=UNRESOLVED) if unended else TurnEvent(EVENT_INTERRUPTED, text=READER_LEFT)
            )
        if turn.ended.kind == EVENT_FAILED and turn.session:
            # The session leads to the agent's own record of the failure. The state file keeps it for a recovery.
            turn.ended = replace(turn.ended, data={**turn.ended.data, "backend_session": turn.session})
        self._turns.pop(turn.conversation, None)
        ended = turn.ended
        if turn.withhold_final and ended.kind == EVENT_SUCCEEDED:
            ended = replace(ended, text="")  # The contract lets a backend keep a withheld final out of storage.
        self._state.end(_key(turn.conversation, turn.request_id), ended)

    async def _leave(self, turn: _Turn) -> None:
        """Retire the session of a prompt that has not ended, so that its late messages reach no later turn.

        A per-conversation connection closes, and the relay ends the agent process. On a shared connection, the
        session's updates, permission requests and tool calls name a session that no turn runs in.
        """
        assert turn.link is not None
        if self._profile.connection == PER_CONVERSATION:
            logger.warning(
                "acp: the turn of conversation %s did not end, so Belle closes its connection and the relay ends"
                " the agent process",
                turn.conversation,
            )
            await self._drop(turn.conversation, turn.link)
            return
        logger.warning(
            "acp: the turn of conversation %s did not end, so Belle leaves session %s, and the conversation's next"
            " turn starts a new one without its earlier context",
            turn.conversation,
            turn.session,
        )
        turn.link.sessions.discard(turn.session)
        self._state.drop_session(turn.conversation)

    def _answer_held(self, turn: _Turn) -> None:
        """Answer each held permission request ``cancelled``: no answer of a person reaches it any more."""
        for held in turn.held.values():
            if not held.answer.done():
                held.answer.set_result(_CANCELLED)
        turn.held.clear()

    async def _submitted(self, turn: _Turn) -> bool:
        """Wait until the SDK has written the turn's prompt, the prompt ended unsent, or the turn is unresolved.

        Return whether the prompt was written.
        """
        assert turn.prompt is not None
        written = asyncio.create_task(turn.submitted.wait())
        unresolved = asyncio.create_task(turn.unresolved.wait())
        try:
            await asyncio.wait({written, unresolved, turn.prompt}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            written.cancel()
            unresolved.cancel()
        return turn.submitted.is_set()

    def _prompt_sent(self, link: Link, session: str) -> None:
        turn = self._live.get((link, session))
        if turn is not None:
            turn.submitted.set()

    async def _stop(self, turn: _Turn) -> bool:
        """Send ``session/cancel`` after the prompt. Return whether the prompt ended within the profile's deadline.

        The deadline covers the wait for the prompt's write, too: an agent that reads nothing never takes it.
        """
        assert turn.link is not None and turn.prompt is not None
        turn.cancelled = True
        turn.first_stop = turn.first_stop or _CANCEL
        try:
            async with asyncio.timeout(self._profile.cancel_timeout):
                if await self._submitted(turn):
                    with suppress(ConnectionError):  # The prompt fails as well, with an unknown outcome.
                        await turn.link.conn.cancel(session_id=turn.session)
                self._answer_held(turn)
                await asyncio.wait({turn.prompt})
        except TimeoutError:
            self._answer_held(turn)
            return False
        return True

    async def cancel(self, conversation: str) -> bool:
        """Stop the conversation's running turn. Return whether it ended within the profile's deadline."""
        turn = self._turns.get(conversation)
        if turn is None:
            return False
        if turn.prompt is None:
            # Not submitted yet: the turn ends before it is.
            turn.cancelled = True
            turn.setup.cancel()
            return True
        if await self._stop(turn):
            return True
        turn.unresolved.set()
        return False

    async def _update(self, link: Link, session: str, update: Any) -> None:
        turn = self._live.get((link, session))
        if turn is None:
            # A session/load replay, or an update after the turn ended: no turn of Belle reads it.
            logger.debug("acp: an update of session %s reaches no running turn", session)
            return
        await turn.queue.put(update)

    async def _permission(
        self, link: Link, rpc_id: Any, session: str, options: list[PermissionOption], call: ToolCallUpdate
    ) -> RequestPermissionResponse:
        """Hold a permission request as a person's question until they answer, the turn ends or it expires."""
        turn = self._live.get((link, session))
        if turn is None or not turn.accepts_actions:
            # Nothing is approved outside a running turn.
            return RequestPermissionResponse(outcome=_CANCELLED)
        allow = next((option.option_id for option in options if option.kind == "allow_once"), None)
        reject = next((option.option_id for option in options if option.kind == "reject_once"), None)
        if allow is None or reject is None:
            # Only these two map to a person's approve and reject without a guess.
            kinds = ", ".join(option.kind for option in options) or "none"
            raise RequestError.invalid_params(
                {
                    "details": f"Belle answers a permission request that offers allow_once and reject_once. This one offers: {kinds}"
                }
            )
        turn.asked += 1
        tool = call.title or ""
        question = Question(
            id=f"{turn.request_id}:permission-{turn.asked}",
            text=f"Allow {tool}?" if tool else "Allow the agent's action?",
            options=(APPROVE, REJECT),
            kind=APPROVAL,
            tool=tool,
        )
        held = turn.held[question.id] = _Held(
            question, allow, reject, link, rpc_id, asyncio.get_running_loop().create_future()
        )
        await turn.queue.put([TurnEvent(EVENT_QUESTION, question=question)])
        try:
            outcome = await asyncio.wait_for(asyncio.shield(held.answer), self._profile.permission_timeout)
        except TimeoutError:
            logger.warning("acp: question %s expired, so its request is answered cancelled", question.id)
            outcome = _CANCELLED
        finally:
            turn.held.pop(question.id, None)
        return RequestPermissionResponse(outcome=outcome)

    def _withdrawn(self, link: Link, rpc_id: Any) -> None:
        """The agent withdrew a request with ``$/cancel_request``: its held question gets ``cancelled``."""
        for turn in self._turns.values():
            for held in turn.held.values():
                if held.link is link and held.rpc_id == rpc_id and not held.answer.done():
                    held.answer.set_result(_CANCELLED)

    async def answer(self, conversation: str, question_id: str, answer: Answer) -> None:
        """Give a person's answer to the permission request it was asked for. A late answer runs nothing."""
        turn = self._turns.get(conversation)
        held = turn.held.get(question_id) if turn is not None else None
        if turn is None or not turn.accepts_actions or held is None or held.answer.done():
            logger.warning(
                "acp: no permission request waits for this answer, so nothing runs: conversation=%s question=%s",
                conversation,
                question_id,
            )
            return
        if approves(answer):
            held.answer.set_result(AllowedOutcome(outcome="selected", option_id=held.allow))
            return
        turn.first_stop = turn.first_stop or _REJECT
        held.answer.set_result(AllowedOutcome(outcome="selected", option_id=held.reject))

    async def pending(self, conversation: str) -> list[Question]:
        """The permission requests of the conversation's turn that wait for a person."""
        turn = self._turns.get(conversation)
        return [held.question for held in turn.held.values()] if turn is not None else []

    async def active(self) -> list[str]:
        """The conversations with a turn running in this process. The relay ends a turn with its connection."""
        return sorted(self._turns)

    async def recover(self, request: TurnRequest) -> Recovery:
        """Read what the state file recorded of a request. It never says ``not_submitted`` without proof."""
        turn = self._turns.get(request.conversation)
        if turn is not None and turn.request_id == request.request_id:
            return Recovery(RECOVERY_RUNNING)
        entry = self._state.request(_key(request.conversation, request.request_id))
        if entry is None and not self._state.dropped:
            # Recorded before anything is sent, so a request missing from a complete record was never sent.
            return Recovery(RECOVERY_NOT_SUBMITTED)
        if entry is None:
            # The window dropped it, or the file was unreadable.
            return Recovery(RECOVERY_TERMINAL, TurnEvent(EVENT_FAILED, text=UNRECORDED))
        return Recovery(RECOVERY_TERMINAL, terminal(entry) or TurnEvent(EVENT_FAILED, text=STREAM_LOST))

    async def retained(self, conversation: str) -> bool:
        """Whether the conversation's session is open, or the agent restores it now."""
        if self._state.session(conversation) is None:
            return False
        try:
            await self._attach(conversation, True)
        except (_Unavailable, AcpError, RequestError, ConnectionError, TimeoutError):
            return False
        return True

    async def _conversation_of(self, meta: Mapping[str, Any], headers: Mapping[str, str]) -> str:
        """Name the conversation of a tool call from its binding, while that conversation's turn runs."""
        if self._profile.binding_header:
            conversation = self._bindings.get(headers.get(self._profile.binding_header.lower(), ""), "")
        else:
            session = str(meta.get(self._profile.binding_meta, ""))
            turns = [turn for (_, session_id), turn in self._live.items() if session_id == session]
            # Metadata binding is supported only on a shared connection.
            conversation = turns[0].conversation if len(turns) == 1 else ""
        turn = self._turns.get(conversation)
        return conversation if turn is not None and turn.accepts_actions else ""

    async def steer(self, request: TurnRequest) -> bool:
        """ACP v1 gives a running turn no further message."""
        return False

    async def ask(self, conversation: str, question: Question) -> str:
        """Not offered: ACP v1 has no question that Belle puts to the agent's turn."""
        raise BackendError("the ACP backend puts no question of its own")

    async def peek_history(self, conversation: str, request_id: str) -> Sequence[PeekHistoryPart]:
        """ACP v1 has no read of a turn's earlier text."""
        return ()

    async def forget(self, conversation: str) -> None:
        """Not offered: the pinned ACP SDK has no client method for session/delete."""
        raise BackendError("the ACP backend deletes no session")

    async def catalog(self) -> Catalog:
        """ACP v1 lists no skills, experts, subagents or models in a form the catalog reads."""
        return Catalog()

    def shutting_down(self) -> None:
        """Nothing to do: the relay ends each turn with its connection."""

    async def close(self) -> None:
        """Close every connection and the tool server, each even when another fails. A second call does nothing.

        Raise the first failure afterwards.
        """
        try:
            failures = await asyncio.gather(
                *(self._drop(key, link) for key, link in list(self._links.items())), return_exceptions=True
            )
        finally:
            if self._tool_server is not None:
                server, self._tool_server = self._tool_server, None
                await server.close()
        for failure in failures:
            if isinstance(failure, BaseException):
                raise failure
