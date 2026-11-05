"""Declare what a messaging platform supplies to the conversation host.

A platform normalizes what arrives and performs what the host decides. It
says how a message addressed the bot. It does not decide what follows.

``conversation`` here is the platform's own id for a channel or a direct
message. The session a turn runs in is the host's conversation key.
"""

import importlib
import importlib.util
import pkgutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from belle_agent.admission import policy as admission_policy
from belle_agent.backend import Answer, Question, TurnEvent
from belle_agent.config import BelleConfigError
from belle_agent.scopes import ChannelCapabilities

# How one message addressed the bot. The names match the ``delivery.mode``
# entries a scope writes, so a trigger is compared with them directly.
TRIGGER_NONE = ""
TRIGGER_DM = "dm"
TRIGGER_MENTION = "mention"
TRIGGER_REPLY = admission_policy.TRIGGER_REPLY
MODE_ALL = admission_policy.MODE_ALL
MODE_URL = admission_policy.MODE_URL
MODE_HAS_FILE = admission_policy.MODE_HAS_FILE

# What became of a message, as the host tells the platform. A platform shows it its own way, or not at all.
#: Admitted: a turn will run for it, now or after the one that is running.
MARK_RECEIVED = "received"
MARK_QUEUED = "queued"
#: The turn of a message that waited has started.
MARK_STARTED = "started"
#: Added to the turn that runs in its conversation. It ends as that turn ends.
MARK_STEERED = "steered"
MARK_DONE = "done"
MARK_FAILED = "failed"
MARK_STOPPED = "stopped"
#: Not admitted, although it asked for a turn: its sender may not ask, or the queue is full.
MARK_REFUSED = "refused"
#: A message that waited was removed before its turn started.
MARK_DROPPED = "dropped"
#: Owed an answer, but it reached Belle too long after it was sent, such as after downtime: it starts no turn.
MARK_LATE = "late"
#: What it carries is being added to the second brain. One of the four marks below follows.
MARK_INGESTING = "ingesting"
#: At least one document was added, or was in the corpus already.
MARK_INGESTED = "ingested"
#: No document was added, and at least one failed.
MARK_INGEST_FAILED = "ingest_failed"
#: Every document was declined.
MARK_INGEST_DECLINED = "ingest_declined"
#: No document was found.
MARK_INGEST_EMPTY = "ingest_empty"

#: For a platform that shows marks as reactions: the setting keys of the reactions each mark adds and removes.
MARK_REACTIONS: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    MARK_RECEIVED: (("acknowledgement_emoji",), ()),
    MARK_STEERED: (("acknowledgement_emoji",), ()),
    MARK_QUEUED: (("acknowledgement_emoji", "queued_emoji"), ()),
    MARK_STARTED: ((), ("queued_emoji",)),
    MARK_DROPPED: ((), ("queued_emoji", "acknowledgement_emoji")),
    MARK_REFUSED: (("rejected_emoji",), ()),
    MARK_LATE: (("late_emoji",), ()),
    MARK_DONE: (("completed_emoji",), ("acknowledgement_emoji",)),
    MARK_FAILED: (("failed_emoji",), ("acknowledgement_emoji",)),
    MARK_STOPPED: (("stopped_emoji",), ("acknowledgement_emoji",)),
    MARK_INGESTING: (("brain_react_pending",), ()),
    MARK_INGESTED: (("brain_react_success",), ("brain_react_pending",)),
    MARK_INGEST_FAILED: (("brain_react_failure",), ("brain_react_pending",)),
    MARK_INGEST_DECLINED: (("brain_react_declined",), ("brain_react_pending",)),
    MARK_INGEST_EMPTY: ((), ("brain_react_pending",)),
}
#: The marks of a message whose turn runs, and of one whose turn has ended or will not run.
TYPING_STARTS = frozenset({MARK_RECEIVED, MARK_STARTED})
TYPING_ENDS = frozenset({MARK_DONE, MARK_FAILED, MARK_STOPPED, MARK_DROPPED})


class DeliveryUncertain(RuntimeError):
    """A post may exist, so automatic retries could duplicate it."""


class PlatformUnreachable(BelleConfigError):
    """``start`` raises this when the platform did not answer, or answered that it cannot serve now.

    The connector holds nothing open, and ``start`` can be called again. The text holds no credential.
    """


class QuestionRefused(Exception):
    """``ask`` raises this for a question the platform cannot show as it was put, such as one with more options than
    the platform shows. The message names the limit, so that the question can be put again in a form that fits.
    """


@dataclass(frozen=True)
class InboundMessage:
    """One message, normalized by its platform."""

    platform: str
    workspace: str
    conversation: str
    message_id: str
    sender: str
    text: str
    #: The platform's delivery id. A second delivery repeats it.
    event_id: str = ""
    #: The message that roots the thread, or empty at the top level.
    thread: str = ""
    sender_is_bot: bool = False
    #: The platform's word for the kind of conversation, as scopes match it.
    chat_type: str = ""
    trigger: str = TRIGGER_NONE
    #: An edit of an earlier message. It carries no new request.
    edited: bool = False
    #: It reached Belle later after it was sent than the platform's catch-up setting allows. It starts no turn.
    late: bool = False
    #: The conversation is end-to-end encrypted: its server cannot read it. The second brain ingests
    #: from it only where ``second_brain.ingest.channels`` names its id.
    encrypted: bool = False
    #: What the message carries, each as ``{"uri": <data: URI>, "name": <file name>}``. A platform that
    #: holds content by reference gives an entry a ``uri`` of its own, and loads it in ``fetch_files``.
    files: tuple[Mapping[str, Any], ...] = ()
    #: Where an answer to this message goes, where the platform's own placement rule says other than ``thread``.
    reply_thread: str | None = None
    #: What the platform adds to the turn's text about the message, such as the ids its tools take.
    envelope: str = ""
    #: A conversation of its own beside the one the message sits in, named by the platform: a question
    #: put privately from elsewhere, and its follow-ups. Its turns share no session with the rest.
    side: str = ""
    #: What a person's command requires of this one turn, beside what the scopes require: skills to
    #: load, and a subagent with its task. The host refuses the message where the turn cannot have them.
    skills_required: tuple[str, ...] = ()
    subagent_required: tuple[str, str] | None = None


@dataclass(frozen=True)
class InboundAnswer:
    """A person's answer to a question the platform rendered."""

    platform: str
    workspace: str
    conversation: str
    question_id: str
    sender: str
    answer: Answer = field(default_factory=Answer)


@dataclass(frozen=True)
class InboundStop:
    """A person's request to stop the turn one message started."""

    platform: str
    workspace: str
    conversation: str
    #: The message whose turn is to be stopped.
    message_id: str
    sender: str


@dataclass(frozen=True)
class InboundDrop:
    """A person's request to remove a message that waits for its turn."""

    platform: str
    workspace: str
    conversation: str
    #: The waiting message.
    message_id: str
    sender: str


@dataclass(frozen=True)
class EventKind:
    """What a platform says of one kind of event it reports."""

    #: How many events of this kind a conversation holds as context for its next turn.
    held: int
    #: An event of this kind may start a turn of its own.
    turns: bool = False
    #: It is about one message, and a turn it starts answers in that message's thread.
    anchored: bool = False
    #: One that cannot start its turn at once waits for it. Any other is kept as context.
    waits: bool = False


@dataclass(frozen=True)
class InboundEvent:
    """Something that happened in a conversation and is no message: a reaction, a pin, a change of members."""

    platform: str
    workspace: str
    conversation: str
    #: One of the platform's ``event_kinds``, as ``delivery.events`` names it.
    kind: str
    #: Who did it, which is not who wrote the message it was done to.
    actor: str
    #: The event as a turn is told it, in the platform's own fields. ``render_events`` writes it out.
    record: Mapping[str, Any] = field(default_factory=dict)
    #: The platform's delivery id. A second delivery repeats it.
    event_id: str = ""
    chat_type: str = ""
    #: The conversation is the actor's direct one with the bot.
    direct: bool = False
    actor_is_bot: bool = False
    #: The message the event is about and the thread that message sits in, or empty where it names none.
    anchor: str = ""
    anchor_thread: str = ""


@dataclass(frozen=True)
class PlaceCheck:
    """Whether a conversation and optional thread can receive a later post."""

    exists: bool
    member: bool
    reason: str = ""
    chat_type: str = ""


class KnowsPlaces(Protocol):
    """Optional platform operations for unattended delivery."""

    #: None means the platform validates dynamic workspace names in check_place.
    workspaces: frozenset[str] | None

    async def check_place(self, conversation: str, thread: str, *, workspace: str = "") -> PlaceCheck: ...

    async def can_read(self, user: str, conversation: str, *, workspace: str = "") -> bool: ...

    async def newcomers(self, origin: str, target: str, *, workspace: str = "") -> tuple[str, ...] | None: ...

    async def open_direct_message(self, user: str, *, workspace: str = "") -> str: ...

    def mention(self, user: str) -> str: ...


#: The triggers that address the bot, which the text of the message may no longer show.
_ADDRESSED = (TRIGGER_MENTION, TRIGGER_REPLY)


def envelope(chat_id: str, message_id: str, root_id: str, trigger: str = "") -> str:
    """The ids a turn's tools take, appended to the text of its message, and the trigger where it addressed the bot."""
    root = f", root_message_id: {root_id}" if root_id else ""
    label = f" [trigger: {trigger}]" if trigger in _ADDRESSED else ""
    return f"[chat_id: {chat_id}, message_id: {message_id}{root}]{label}"


#: The host's receiver. It returns the admission outcome, for the platform's log.
OnInbound = Callable[["InboundMessage | InboundAnswer | InboundStop | InboundDrop | InboundEvent"], Awaitable[str]]


class Platform(Protocol):
    """What a platform connector supplies.

    Two more members are a platform's to add or leave out, and the host reads
    them only where a decision endpoint is configured. ``decision_facts(inbound)``
    returns what the decision gate is told of one message, as the keyword
    arguments of ``ShadowDecisionGate.advise``, where the platform knows more
    than the record says. ``recent_messages(conversation, limit, before, *, workspace)``
    is a coroutine that returns the messages before one of them, oldest first,
    as ``message_decisions.message_entry`` writes one.

    ``wait_down()`` is a coroutine a platform adds where it can stop for good
    while the process runs: it returns why, once the platform will serve no more
    until a restart. A platform without it never stops for good. When every
    platform of a host has stopped for good, the launcher exits.

    ``posting_tools`` optionally names additional tools whose message receipts count as delivery.
    ``posting_tool`` remains the default tool named in turn instructions and always counts.

    ``affordances`` is a ``PlatformAffordances`` a platform may declare with
    the facts no other member states. The host reads it through
    ``affordances(platform)``, which adds what the other members state.
    """

    #: The channel name scopes match.
    name: str
    #: The tool a turn posts with, among ``toolkits``. The host counts a post only when the tool's result
    #: is the text of a JSON object with these fields:
    #:
    #: - ``message_id``: a non-empty string, the ID of the first message posted.
    #: - ``chat_id``: a string, the conversation the first message went to, in the form of
    #:   ``InboundMessage.conversation``. A counted post is the turn's reply only where ``chat_id`` is the
    #:   conversation the turn came from, in any thread. After a post elsewhere, the final is still owed there.
    #: - ``part_message_ids``, optional: a non-empty list of non-empty strings, the ID of each message
    #:   posted, in order. Its first item is equal to ``message_id``.
    #: - ``parts``, optional: an ``int`` equal to the number of IDs. This is the length of
    #:   ``part_message_ids``, or 1 when the result has no ``part_message_ids``.
    #: - ``thread_id``, optional: a string.
    #:
    #: An optional field in a different form voids the post. A refusal (``"ok": false``) with these
    #: fields also counts, as when a post fails after it sent some of its messages. When a result does not
    #: count, the host handles the turn's final text as for a turn that posted nothing.
    posting_tool: str
    #: What scopes may match and set here. The host registers it before compiling scopes.
    capabilities: ChannelCapabilities
    #: The chat types open to a whole workspace, for a setting that selects public conversations.
    #: Empty where the platform cannot say.
    public_chat_types: frozenset[str]
    #: Chat types where a completion report may include the requester's result.
    direct_chat_types: frozenset[str]
    #: The command word for a workspace's completion report.
    def command_prefix(self, workspace: str) -> str: ...

    #: The kinds of event this platform reports, as ``delivery.events`` names them. Empty where it reports none.
    event_kinds: Mapping[str, EventKind]
    #: ``post`` can show a text to one person only, as ``ephemeral_to`` asks. Where it cannot, the host sends
    #: no such text, and a mark is the only sign.
    ephemeral_posts: bool

    def bind(self, host: Any) -> None:
        """Take what the host answers a command surface: see ``belle_agent.host.commands``.

        The host calls it once, before ``start``. A platform that gives people no commands keeps nothing.
        """

    async def start(self, on_inbound: OnInbound) -> None:
        """Connect, then hand each normalized inbound record to ``on_inbound``.

        Raise ``PlatformUnreachable`` where a later attempt can connect, and ``BelleConfigError`` where none can.
        """

    async def stop(self) -> None:
        """Disconnect and deliver nothing further."""

    # ``workspace`` names the workspace of the conversation, as the inbound record that led to the action
    # gave it. A conversation id means something in one workspace only, so a platform connected to several
    # acts in the one named and in no other. A platform with one workspace, or none, has no use for it.

    async def post(
        self, conversation: str, thread: str, text: str, *, ephemeral_to: str | None = None, workspace: str = ""
    ) -> str:
        """Deliver text, to one person only when ``ephemeral_to`` names them. Return a message id."""

    async def react(
        self, conversation: str, message_id: str, name: str, remove: bool = False, *, workspace: str = ""
    ) -> None:
        """Add or remove one reaction."""

    async def ask(self, conversation: str, thread: str, question: Question, *, workspace: str = "") -> str:
        """Render a question or an approval prompt. Return its message id.

        A platform that cannot ask raises: :class:`QuestionRefused` for a question it cannot show as it was put.
        A tool call that needs a person's agreement is then refused, since nobody can be asked about it.
        """

    async def mark(self, inbound: InboundMessage, state: str) -> None:
        """Show what became of a message, one of the ``MARK_`` states. A platform with no such surface does nothing."""

    async def fetch_files(self, inbound: InboundMessage) -> tuple[Mapping[str, Any], ...]:
        """Return what a message carries with its content loaded, each as ``{"uri": <data: URI>, "name"}``.

        The host asks once a message has a turn or is selected for ingest, so
        a platform that holds content by reference reads nothing before that.
        A message with both is asked for once, and both get what was read.
        What cannot be read is left out, and the platform says so where the
        message was sent.
        """

    def render_events(self, held: Mapping[str, Any] | None, started_by: InboundEvent | None = None) -> str:
        """Write what a turn is told of events: those its conversation held, and the one that started it.

        ``held`` is what ``InboundEventBuffer.fold`` returned for the conversation, or nothing. A platform
        that reports no event is never asked.
        """

    def toolkits(self, metadata_provider: Callable[[], Mapping[str, Any]]) -> list[Any]:
        """Return toolkits that read the request in flight through ``metadata_provider``.

        A toolkit has ``get_tool_definitions()``, a list of ``{name,
        description, input_params}``, and one coroutine method per name. It
        may also have ``widening_question(name, arguments)``, a coroutine
        that returns what a person must be asked before that call runs, or
        nothing. The tool host asks it before every call of that toolkit. A
        call with a question is not run: the host puts the question to the
        conversation the turn came from, and makes the call once if a person
        agrees.
        """

    def request_metadata(self, inbound: InboundMessage) -> dict[str, Any]:
        """Return the metadata this platform's toolkits read for a turn."""


# ── what a running turn shows ────────────────────────────────────────────

#: What ``conclude`` is told of a turn that ended and said nothing where no reply was owed.
ENDED_SILENT = "silent"


class ShowsTurns(Protocol):
    """What a platform adds to the port to show a turn while it runs. All of it is optional.

    A platform with no such surface has none of these members, and the host
    asks it for nothing. A platform that has them is handed only the events
    of the kinds it lists, and only where the backend reports that kind.
    """

    #: Each kind of ``backend.PROGRESS_EVENTS`` this platform shows as it is configured, with the
    #: setting that turns it on. The host names at start a setting whose kind the backend does not report.
    shows: Mapping[str, str]
    #: Optional, false where absent. True where a card cannot come down without a trace: for a turn whose
    #: reply is optional, the host then hands over no tool event until the turn has posted, and then every
    #: one so far. A turn that stays silent leaves no card.
    cards_wait_for_a_post: bool

    async def begin(self, inbound: InboundMessage) -> None:
        """Optional: the turn of a message has started, and is owed a reply.

        A platform whose only stop control besides a command is on the activity card starts the card here, so a
        turn that reports nothing still shows a stop once it has run for the card's delay. The host calls it for no
        turn whose reply is optional: such a turn posts only through a tool, and that call brings up the card.
        """

    async def progress(self, inbound: InboundMessage, event: TurnEvent) -> None:
        """Show one thing the turn of a message reports while it runs.

        ``tool_called`` and ``tool_result`` say what the turn is doing. ``text_delta`` is the reply
        as it is written: the host hands it over only for a turn whose final it will post, so a
        platform never shows a reply that is then withheld.
        """

    async def conclude(self, inbound: InboundMessage, state: str, final: str = "") -> bool:
        """End what was shown of a turn, once, before the message is marked.

        ``state`` is ``MARK_DONE``, ``MARK_FAILED``, ``MARK_STOPPED`` or ``ENDED_SILENT``. ``final``
        is the reply the host is about to post. A platform that was showing that reply ends it as
        ``final``, whole, and returns true: the host then posts nothing. With no ``final`` the turn
        has nothing more to say, and a reply still on screen is no part of what the turn said.
        Raise ``DeliveryUncertain`` when a post may exist but its delivery cannot be confirmed.
        """


#: Whether a platform can do one thing: always, never, or only as the call finds out. A ``conditional``
#: capability is attempted, and the platform refuses with a reason where it cannot.
Affordance = Literal["present", "absent", "conditional"]
@dataclass(frozen=True)
class PlatformAffordances:
    """Capabilities a platform declares when its members cannot state them."""

    #: A private post may be refused even when ``ephemeral_posts`` is true.
    private_aside: Affordance | None = None
    #: Whether ``ask`` can wait for an answer.
    questions: Affordance = "conditional"
    history: Affordance | None = None
    cards_wait_for_a_post: bool | None = None
    #: How editable cards end for silent or stopped turns, and when they held only the stop.
    silent_card: Literal["delete", "keep"] | None = None
    #: Whether later reply parts join the first part's thread.
    reply_parts_in_thread: bool | None = None


def _declared(platform: Any) -> PlatformAffordances | None:
    declared = getattr(platform, "affordances", None)
    return declared if isinstance(declared, PlatformAffordances) else None


def _stated(platform: Any) -> PlatformAffordances:
    """Read capabilities from the platform's current members."""
    surfaces = getattr(platform, "surfaces", None)
    history = getattr(platform, "history_available", None)
    return PlatformAffordances(
        private_aside="present" if platform.ephemeral_posts else "absent",
        questions="conditional" if callable(getattr(platform, "ask", None)) else "absent",
        history="present" if history is not False and callable(getattr(platform, "recent_messages", None)) else "absent",
        cards_wait_for_a_post=bool(getattr(platform, "cards_wait_for_a_post", False)),
        silent_card=getattr(surfaces, "silent_card", None),
        reply_parts_in_thread=getattr(surfaces, "reply_parts_in_thread", None),
    )


def affordances(platform: Any, *, workspace: str | None = None) -> PlatformAffordances:
    """Read live capabilities, optionally for the addressed installation."""
    if workspace is not None and callable(resolve := getattr(platform, "affordances_for", None)):
        return cast(PlatformAffordances, resolve(workspace))
    stated, declared = _stated(platform), _declared(platform)
    if declared is None:
        return stated
    conditional = stated.private_aside == "present" and declared.private_aside == "conditional"
    return replace(
        declared,
        private_aside="conditional" if conditional else stated.private_aside,
        questions=declared.questions if callable(getattr(platform, "ask", None)) else "absent",
        history=stated.history,
        cards_wait_for_a_post=stated.cards_wait_for_a_post,
        silent_card=stated.silent_card if hasattr(platform, "surfaces") else declared.silent_card,
        reply_parts_in_thread=(
            stated.reply_parts_in_thread if hasattr(platform, "surfaces") else declared.reply_parts_in_thread
        ),
    )


def affordance_contradictions(platform: Any) -> list[str]:
    """Report declarations that conflict with platform members."""
    declared = _declared(platform)
    found = [
        f"{name}: {detail}"
        for name, member in getattr(platform, "members", {}).items()
        for detail in affordance_contradictions(member)
    ]
    if declared is None:
        return found
    stated = _stated(platform)
    if declared.questions != "absent" and not callable(getattr(platform, "ask", None)):
        found.append(f"questions: {declared.questions}, but ask is absent")
    if declared.private_aside is not None and (declared.private_aside == "absent") != (stated.private_aside == "absent"):
        found.append(f"private_aside: {declared.private_aside}, but ephemeral_posts is {platform.ephemeral_posts}")
    if declared.history is not None and declared.history != stated.history:
        found.append(f"history: {declared.history}, but recent_messages is {stated.history}")
    if declared.cards_wait_for_a_post is not None and declared.cards_wait_for_a_post != stated.cards_wait_for_a_post:
        found.append(f"cards_wait_for_a_post: {declared.cards_wait_for_a_post}, but the attribute is {stated.cards_wait_for_a_post}")
    surfaces = getattr(platform, "surfaces", None)
    for name in ("silent_card", "reply_parts_in_thread"):
        if hasattr(surfaces, name) and getattr(declared, name) != getattr(stated, name):
            found.append(f"{name}: {getattr(declared, name)}, but surfaces.{name} is {getattr(stated, name)}")
    return found


def _package(name: str) -> Any:
    """The platform package of that name, or ``None``."""
    if not name.isidentifier() or importlib.util.find_spec(f"{__package__}.{name}") is None:
        return None
    module = importlib.import_module(f"{__package__}.{name}")
    # This module is a package member too, and its factory is this function.
    factory = getattr(module, "create_platform", None)
    return module if factory is not None and factory is not create_platform else None


def platform_names() -> list[str]:
    """Name every platform package, as its configuration section is named."""
    return [module.name for module in pkgutil.iter_modules(importlib.import_module(__package__).__path__) if module.ispkg]


def platform_secret_fields() -> dict[tuple[str, ...], str]:
    """Each secret field of the platform sections, as ``belle_agent.config.SECRET_FIELDS`` lists one.

    A package may expose ``SECRET_FIELDS``: paths in its section, each with an example variable.
    """
    found: dict[tuple[str, ...], str] = {}
    for name in platform_names():
        fields = getattr(importlib.import_module(f"{__package__}.{name}"), "SECRET_FIELDS", {})
        found.update({("platforms", name, *path): example for path, example in fields.items()})
    return found


def validate_platform_config(config: dict[str, Any], config_path: Path) -> None:
    """Run each platform's optional configuration validator before shared validation."""
    for name in platform_names():
        module = importlib.import_module(f"{__package__}.{name}")
        validate = getattr(module, "validate_config", None)
        if validate is not None:
            validate(config, config_path)


def create_platform(config: Mapping[str, Any], name: str, *, state_dir: Path | None = None) -> Platform:
    """Construct the connector of the platform ``name``.

    A platform is a package under ``belle_agent.platforms`` that exposes
    ``create_platform(config, state_dir=None)``. ``state_dir`` is the directory the
    host keeps its state in, where a platform keeps what it must remember across restarts.
    """
    module = _package(name)
    if module is None:
        raise BelleConfigError(f"Belle config platforms.{name} names no platform connector")
    return module.create_platform(config, state_dir=state_dir)


def _unset(missing: list[str]) -> str:
    return f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set"


def _why_off(name: str, section: Mapping[str, Any], module: Any) -> str:
    """Say why a platform section is off, or nothing where it is on. Refuse a section that is on in part.

    A package may expose ``credentials(section)``: each credential the section needs, by the name an operator
    sets it under, and whether it resolves. A package with none is on only with ``enabled: true``.
    """
    enabled = section.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise BelleConfigError(f"Belle config platforms.{name}.enabled must be true, false or empty")
    if enabled is False:
        return "enabled is false"
    credentials = dict(getattr(module, "credentials", lambda section: {})(section))
    missing = [credential for credential, resolves in credentials.items() if not resolves]
    if not credentials:
        return "" if enabled else "it has no credentials, so only enabled: true turns it on"
    if not missing:
        return ""
    if enabled or len(missing) < len(credentials):
        turned_on = "enabled is true" if enabled else "some of its credentials are set"
        raise BelleConfigError(
            f"Belle config platforms.{name} cannot start: {turned_on}, and {_unset(missing)}."
            f" Set {'it' if len(missing) == 1 else 'them'}, or set platforms.{name}.enabled: false"
        )
    return _unset(missing)


def enabled_platforms(config: Mapping[str, Any]) -> list[str]:
    """Name each platform the configuration turns on, in the order its sections are written.

    A section turns its platform on when its credentials resolve. ``enabled: false`` turns it off, and
    ``enabled: true`` makes a missing credential an error, as is a section with only some of them.
    """
    enabled, off = [], []
    for name, section in (config.get("platforms") or {}).items():
        if section is None:
            section = {}
        if not isinstance(section, Mapping):
            raise BelleConfigError(f"Belle config platforms.{name} must be a mapping")
        why = _why_off(name, section, _package(name))
        if why:
            off.append(f"platforms.{name} ({why})")
        else:
            enabled.append(name)
    if not enabled:
        found = "; ".join(off) if off else f"it has no platform section, such as {' or '.join(f'platforms.{name}' for name in platform_names())}"
        raise BelleConfigError(f"Belle config turns on no platform: {found}")
    return enabled
