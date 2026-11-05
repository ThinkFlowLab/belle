"""One platform port over several installations of a platform, each served by a connector of its own.

An installation is one bot of the platform, such as one Mattermost server or one Matrix account. Nothing of
one installation is reachable from another:

- Every action the host asks for names a workspace. The connector of the installation that the workspace
  names performs it, or nothing is done.
- A tool call runs on the toolkits of the installation of its turn, which the turn's metadata names. Those
  hold the credentials of that installation and of no other.

An installation that does not answer at start is started again in the background until it connects. Until
then, an action that names it is refused.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from typing import Any

from belle_agent.backend import Question, TurnEvent
from belle_agent.config import BelleConfigError
from belle_agent.platforms.base import (
    InboundEvent,
    InboundMessage,
    OnInbound,
    PlaceCheck,
    PlatformAffordances,
    PlatformUnreachable,
    affordances,
)
from belle_agent.platforms.tool_routing import RoutedToolkit

#: The wait before an installation that did not answer is started again. It doubles after each attempt, up to the longest.
RETRY_FIRST_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0


class InstallationUnknown(LookupError):
    """An action names a workspace of no installation this process serves. Nothing is sent."""

    def __init__(self, noun: str, workspace: str) -> None:
        super().__init__(f"no served {noun} has the workspace {workspace or '(none)'}, so nothing is sent")


class InstallationConnecting(InstallationUnknown):
    """An action names the workspace of an installation that has not connected yet. Nothing is sent."""

    def __init__(self, noun: str, name: str) -> None:
        LookupError.__init__(self, f"{noun} {name} has not connected yet, so nothing is sent")


class Installations:
    """The platform port over one connector per installation, by the installation's name.

    ``noun`` names one installation in messages, such as ``server``. ``where`` is the
    configuration key of the list. ``installation_of`` gives the name of a workspace's installation, or
    empty. ``metadata_key`` is the key of a turn's metadata that names its installation, and ``unknown_tool``
    answers a tool call whose turn names no installation of this port.

    ``started_elsewhere`` is for a process that runs the tools of turns that another process receives, and
    starts nothing itself. That process starts the installations and gives each turn the installation it came
    from, so a tool call here runs on the installation its turn names, started here or not.
    """

    def __init__(
        self,
        members: Mapping[str, Any],
        *,
        noun: str,
        where: str,
        installation_of: Callable[[str], str],
        metadata_key: str,
        unknown_tool: Callable[[str, str], str | None],
        logger: logging.Logger,
        started_elsewhere: bool = False,
    ) -> None:
        self._members = dict(members)
        self._started_elsewhere = started_elsewhere
        self._where = where
        self._installation_of = installation_of
        self._metadata_key, self._unknown_tool = metadata_key, unknown_tool
        self._logger = logger
        #: Each installation that started, by name.
        self._serving: dict[str, Any] = {}
        #: The task that starts again each installation that did not answer, by name.
        self._connecting: dict[str, asyncio.Task[None]] = {}
        #: Set whenever an installation joins ``_serving`` or leaves ``_connecting``.
        self._changed = asyncio.Event()
        first = next(iter(self._members.values()))
        self.name: str = first.name
        #: One installation, as messages name it, such as ``Mattermost server``.
        self._word, self._noun = noun, f"{self.name.capitalize()} {noun}"
        self.posting_tool = first.posting_tool
        self.posting_tools = tuple(getattr(first, "posting_tools", ()))
        self.capabilities = first.capabilities
        self.public_chat_types = first.public_chat_types
        self.direct_chat_types = first.direct_chat_types
        self.event_kinds = first.event_kinds
        self.ephemeral_posts = first.ephemeral_posts
        self.cards_wait_for_a_post = getattr(first, "cards_wait_for_a_post", False)
        if hasattr(first, "wait_down"):
            # Only a platform that can stop for good says so: the host waits for ever on one without it.
            self.wait_down = self._wait_down

    def bind(self, host: Any) -> None:
        """Every installation asks the host about its own conversations."""
        for member in self._members.values():
            member.bind(host)

    async def start(self, on_inbound: OnInbound) -> None:
        """Start every installation once, then return.

        One that cannot be served is named. One that did not answer is started again in the background, and
        is served once it connects. Where none started, the start fails.
        """
        failures: list[str] = []
        unreachable: dict[str, str] = {}
        for name, member in self._members.items():
            try:
                await self._start_one(name, member, on_inbound)
            except PlatformUnreachable as exc:
                unreachable[name] = str(exc)
                continue
            except Exception as exc:  # noqa: BLE001 - one installation that cannot start does not take the others down
                failures.append(f"{name}: {self._refusal(name, exc)}")
                await member.stop()
        if not self._serving:
            for name in unreachable:
                await self._members[name].stop()
            told = failures + [f"{name}: {reason}" for name, reason in unreachable.items()]
            raise BelleConfigError(f"no {self._word} of {self._where} can be served. " + "; ".join(told))
        for name, reason in unreachable.items():
            self._logger.error(
                "%s %s is NOT CONNECTED: %s. It is started again in the background", self._noun.lower(), name, reason
            )
            task = asyncio.create_task(self._reconnect(name, on_inbound), name=f"{self.name}-{name}-reconnect")
            task.add_done_callback(self._reconnect_ended)
            self._connecting[name] = task
        log = self._logger.error if failures or unreachable else self._logger.info
        log(
            "%s serves %d of %d %s: %s%s%s",
            self.name, len(self._serving), len(self._members), f"{self._word}s", ", ".join(self._serving),
            f". Not served: {'; '.join(failures)}" if failures else "",
            f". Connecting: {', '.join(unreachable)}" if unreachable else "",
        )

    async def _start_one(self, name: str, member: Any, on_inbound: OnInbound) -> None:
        """Start one installation and serve it, or raise why it is not served."""
        await member.start(self.inbound_of(member, on_inbound))
        await self.refuse_twice_served(name, member)
        self._serving[name] = member
        self._changed.set()

    def _refusal(self, name: str, exc: Exception) -> str:
        reason = str(exc) if isinstance(exc, BelleConfigError) else type(exc).__name__
        self._logger.error("%s %s is NOT SERVED: %s", self._noun.lower(), name, reason)
        return reason

    async def _reconnect(self, name: str, on_inbound: OnInbound) -> None:
        """Start an installation that did not answer again, waiting longer after each attempt, until it is served or refused."""
        member = self._members[name]
        delay = RETRY_FIRST_SECONDS
        try:
            while True:
                await asyncio.sleep(delay)
                try:
                    await self._start_one(name, member, on_inbound)
                except PlatformUnreachable as exc:
                    delay = min(delay * 2, RETRY_MAX_SECONDS)
                    self._logger.warning(
                        "%s %s is still not connected: %s; next attempt in %.0f s", self._noun.lower(), name, exc, delay
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 - a refusal ends the attempts at this installation only
                    self._refusal(name, exc)
                    await member.stop()
                    return
                self._logger.info("%s %s is served now", self._noun.lower(), name)
                return
        finally:
            if self._connecting.get(name) is asyncio.current_task():
                del self._connecting[name]
            self._changed.set()

    def _reconnect_ended(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            self._logger.error("%s: the attempts to connect ended with an error", task.get_name(), exc_info=error)

    @property
    def connecting(self) -> frozenset[str]:
        """The installations that did not answer at start and are started again in the background."""
        return frozenset(self._connecting)

    async def refuse_twice_served(self, name: str, member: Any) -> None:
        """Raise ``BelleConfigError`` where ``member`` is an installation that another entry already serves."""

    def inbound_of(self, member: Any, on_inbound: OnInbound) -> OnInbound:
        """What an installation hands its records to. Here, the host's own ``on_inbound``."""
        return on_inbound

    def installation_of_turn(self, value: str) -> str:
        """The name of the installation that a turn's ``metadata_key`` value names. Here, the value is the name."""
        return value

    @property
    def affordances(self) -> PlatformAffordances | None:
        """Return the connector's shared capability declaration."""
        declared = getattr(next(iter(self._members.values())), "affordances", None)
        return declared if isinstance(declared, PlatformAffordances) else None

    def affordances_for(self, workspace: str) -> PlatformAffordances:
        """Read the addressed installation without borrowing another installation's features."""
        return affordances(self._member(workspace))

    @property
    def history_available(self) -> bool:
        """Whether an installation can supply decision history."""
        return any(affordances(member).history != "absent" for member in self._members.values())

    @property
    def members(self) -> Mapping[str, Any]:
        """The connector of each installation, by name, in the order written."""
        return self._members

    @property
    def serving(self) -> Mapping[str, Any]:
        """Each installation that started, by name."""
        return self._serving

    async def stop(self) -> None:
        """Stop every installation, and the attempts to connect those that did not answer."""
        connecting, self._connecting = self._connecting, {}
        for task in connecting.values():
            task.cancel()
        for name, task in connecting.items():
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if name not in self._serving:
                await self._members[name].stop()
        serving, self._serving = self._serving, {}
        for member in serving.values():
            await member.stop()

    async def _wait_down(self) -> str:
        """Return why, once every installation served has stopped for good and none is still connecting."""
        downs: dict[str, asyncio.Future[str]] = {}
        try:
            while True:
                for name, member in self._serving.items():
                    if name not in downs:
                        downs[name] = asyncio.ensure_future(member.wait_down())
                waiting = [down for down in downs.values() if not down.done()]
                if not waiting and not self._connecting:
                    return "; ".join(f"{name}: {down.result()}" for name, down in downs.items())
                self._changed.clear()
                changed = asyncio.ensure_future(self._changed.wait())
                try:
                    await asyncio.wait([*waiting, changed], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
        finally:
            for down in downs.values():
                down.cancel()

    def _member(self, workspace: str) -> Any:
        name = self._installation_of(workspace)
        member = self._serving.get(name)
        if member is None:
            if name in self._connecting:
                raise InstallationConnecting(self._noun, name)
            # Never the first installation, or one that has such a conversation: that would be a guess.
            raise InstallationUnknown(self._noun, workspace)
        return member

    def command_prefix(self, workspace: str) -> str:
        return str(self._member(workspace).command_prefix(workspace))

    async def post(
        self, conversation: str, thread: str, text: str, *, ephemeral_to: str | None = None, workspace: str = ""
    ) -> str:
        """Post with the installation of ``workspace``. See ``Platform.post``."""
        return str(await self._member(workspace).post(conversation, thread, text, ephemeral_to=ephemeral_to))

    async def react(
        self, conversation: str, message_id: str, name: str, remove: bool = False, *, workspace: str = ""
    ) -> None:
        """React with the installation of ``workspace``."""
        await self._member(workspace).react(conversation, message_id, name, remove)

    async def ask(self, conversation: str, thread: str, question: Question, *, workspace: str = "") -> str:
        """Ask with the installation of ``workspace``."""
        return str(await self._member(workspace).ask(conversation, thread, question, workspace=workspace))

    async def mark(self, inbound: InboundMessage, state: str) -> None:
        """Mark a message with the installation it came from."""
        await self._member(inbound.workspace).mark(inbound, state)

    @property
    def shows(self) -> Mapping[str, str]:
        """What a running turn shows is one setting for every installation."""
        return dict(next(iter(self._members.values())).shows)

    async def begin(self, inbound: InboundMessage) -> None:
        """Start what a turn shows with the installation of its message, where that installation shows it."""
        begin = getattr(self._member(inbound.workspace), "begin", None)
        if begin is not None:
            await begin(inbound)

    async def progress(self, inbound: InboundMessage, event: TurnEvent) -> None:
        """Show a turn's event with the installation of its message."""
        await self._member(inbound.workspace).progress(inbound, event)

    async def conclude(self, inbound: InboundMessage, state: str, final: str = "") -> bool:
        """End what a turn showed, with the installation of its message."""
        return bool(await self._member(inbound.workspace).conclude(inbound, state, final))

    async def fetch_files(self, inbound: InboundMessage) -> tuple[Mapping[str, Any], ...]:
        """Read a message's files with the installation it came from."""
        return tuple(await self._member(inbound.workspace).fetch_files(inbound))

    def render_events(self, held: Mapping[str, Any] | None, started_by: InboundEvent | None = None) -> str:
        """The words a turn is told events in are the same for every installation."""
        return str(next(iter(self._members.values())).render_events(held, started_by))

    def decision_facts(self, inbound: InboundMessage) -> dict[str, Any]:
        """What the decision gate is told of a message, by the installation it came from."""
        return dict(self._member(inbound.workspace).decision_facts(inbound))

    async def recent_messages(self, conversation: str, limit: int, before: str, *, workspace: str = "") -> list[dict[str, Any]]:
        """The messages before ``before``, read with the installation of ``workspace``."""
        member = self._member(workspace)
        recent = getattr(member, "recent_messages", None)
        if not callable(recent) or affordances(member).history == "absent":
            raise NotImplementedError(f"{self._noun} {self._installation_of(workspace)} has no decision history")
        return list(await recent(conversation, limit, before))

    def _runs_tools(self, value: str) -> bool:
        """Whether a tool call of a turn whose ``metadata_key`` is ``value`` runs here.

        Only on an installation that started: one that failed its start or is still connecting acts for no
        turn. Where another process starts them, it made the turn, so the installation started there.
        """
        name = self.installation_of_turn(value)
        return name in self._members if self._started_elsewhere else name in self._serving

    def toolkits(self, metadata_provider: Callable[[], Mapping[str, Any]]) -> list[Any]:
        """One set of tools, each call run on the toolkit of its turn's installation."""
        per_member = {name: member.toolkits(metadata_provider) for name, member in self._members.items()}
        routed = []
        for kits in zip(*per_member.values(), strict=True):
            by_name = dict(zip(per_member, kits, strict=True))

            def select(value: str, choices: Mapping[str, Any] = by_name) -> Any | None:
                return choices.get(self.installation_of_turn(value)) if self._runs_tools(value) else None

            routed.append(
                RoutedToolkit(
                    kits[0],
                    select,
                    metadata_provider,
                    self._metadata_key,
                    self._unknown_tool,
                )
            )
        return routed

    async def resolve_content_attachment(self, ref: str, metadata: Mapping[str, Any]) -> dict[str, str]:
        """Read a file for a content tool, with the installation of the turn ``metadata`` describes."""
        value = str(metadata.get(self._metadata_key) or "")
        name = self.installation_of_turn(value)
        member = self._members.get(name) if self._runs_tools(value) else None
        if member is None:
            if name in self._connecting:
                raise InstallationConnecting(self._noun, name)
            raise InstallationUnknown(self._noun, value)
        return dict(await member.resolve_content_attachment(ref, metadata))

    def request_metadata(self, inbound: InboundMessage) -> dict[str, Any]:
        """The metadata of a turn's tool calls, which names its installation."""
        return dict(self._member(inbound.workspace).request_metadata(inbound))

    async def check_place(self, conversation: str, thread: str, *, workspace: str = "") -> PlaceCheck:
        return await self._member(workspace).check_place(conversation, thread, workspace=workspace)

    async def can_read(self, user: str, conversation: str, *, workspace: str = "") -> bool:
        return bool(await self._member(workspace).can_read(user, conversation, workspace=workspace))

    async def newcomers(self, origin: str, target: str, *, workspace: str = "") -> tuple[str, ...] | None:
        return await self._member(workspace).newcomers(origin, target, workspace=workspace)

    async def open_direct_message(self, user: str, *, workspace: str = "") -> str:
        return str(await self._member(workspace).open_direct_message(user, workspace=workspace))

    def mention(self, user: str) -> str:
        return str(next(iter(self._members.values())).mention(user))
