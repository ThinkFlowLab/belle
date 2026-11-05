"""Audience bounds for reading and sharing conversation content."""

from collections.abc import Callable
from typing import NamedTuple

#: How many people a question about a widening post names, before it counts the rest.
MAX_NAMED_OUTSIDERS = 5


class PlaceWords(NamedTuple):
    """How a platform names its conversations in the sentences of this module."""

    #: A conversation that anybody may read, after "a": ``public channel``.
    public: str
    #: Who reads a public conversation: ``anybody in this workspace``.
    readers: str
    #: Who reads every public conversation, after "readable by": ``the whole workspace``.
    everybody: str


#: The words of Slack and Mattermost.
CHANNEL_WORDS = PlaceWords("public channel", "anybody in this workspace", "the whole workspace")


class Audience(NamedTuple):
    """A conversation's current or future audience."""

    workspace_wide: bool
    members: frozenset[str]
    external: bool


def audience_within(inner: Audience, outer: Audience) -> bool:
    """Test whether everyone in ``inner`` is also in ``outer``.

    External users exceed a workspace audience. Future public members exceed a finite list.
    """
    if outer.workspace_wide:
        return not inner.external
    return not inner.workspace_wide and inner.members <= outer.members


def read_permitted(source: Audience, target: Audience) -> bool:
    """Allow reading T into S when audience(S) is within audience(T)."""
    return audience_within(source, target)


def write_permitted(source: Audience, target: Audience) -> bool:
    """Allow writing S into T when audience(T) is within audience(S)."""
    return audience_within(target, source)


def audience_newcomers(inner: Audience, outer: Audience) -> tuple[str, ...] | None:
    """Return newly reached IDs, or ``None`` for workspace audiences."""
    return None if outer.workspace_wide or inner.workspace_wide else tuple(sorted(inner.members - outer.members))


def read_refusal(
    source: Audience, target: Audience, source_name: str, target_name: str, words: PlaceWords = CHANNEL_WORDS
) -> tuple[str, str] | None:
    """Why ``target``'s content may not be read into ``source``, as a code and a detail, or ``None`` when it may.

    The detail counts the members that block a read and never names them: the target's members may be private.
    """
    if read_permitted(source, target):
        return None
    if source.workspace_wide:
        return (
            "source_is_public_and_target_is_not",
            (
                f"{source_name} is a {words.public}, so {words.readers} can"
                f" read what is posted here, now or after they join."
                f" {target_name} is not readable by {words.everybody}, so"
                f" answering here would publish it to people who cannot read it"
                f" themselves. Only another {words.public} may be read into a"
                f" public one"
            ),
        )
    if target.external:
        return (
            "target_reaches_outside_workspace",
            (
                f"{target_name} is shared with people outside this workspace, so"
                f" its audience is not this workspace's, and {source_name} cannot"
                f" stand in for it. This is a property of the conversation"
                f" rather than a membership to change"
            ),
        )
    blocking = len(source.members - target.members)
    return (
        "source_members_not_in_target",
        (
            f"{blocking} member(s) of {source_name} are not in {target_name};"
            f" answering here would show them {target_name} content they cannot"
            f" read themselves"
        ),
    )


def direct_message_question(person: str) -> str:
    """Ask before a post goes to a direct message with ``person``, as the platform writes a name."""
    return (
        f"This message would be sent privately to {person}, who is not"
        f" in this conversation. Nobody here will see it. Send it?"
    )


def workspace_question(target: str, words: PlaceWords = CHANNEL_WORDS) -> str:
    """Ask before a post goes to a public conversation from one that is not public. ``target`` is as the platform writes it."""
    return (
        f"This message would go to {target}, which is a {words.public}."
        f" {words.readers[:1].upper()}{words.readers[1:]} can read it there, now or after they"
        f" join. This conversation is not public. Post it there?"
    )


def external_question(target: str) -> str:
    """Ask before a post goes to a conversation shared outside the workspace. ``target`` is as the platform writes a channel."""
    return (
        f"This message would go to {target}, which is shared with people"
        f" outside this workspace. They can read it there. Post it there?"
    )


def widening_question(target: str, outsiders: tuple[str, ...], mention: Callable[[str], str]) -> str:
    """Ask before a post in ``target``, as the platform writes a channel, reaches ``outsiders``, each written by ``mention``."""
    count = len(outsiders)
    named = ", ".join(mention(who) for who in outsiders[:MAX_NAMED_OUTSIDERS])
    if count > MAX_NAMED_OUTSIDERS:
        named += f" and {count - MAX_NAMED_OUTSIDERS} more"
    person = "person who is" if count == 1 else "people who are"
    return (
        f"This message would go to {target}, where it will also be seen"
        f" by {count} {person} not in this conversation: {named}."
        f" Post it there?"
    )


def unattended_write_refusal(target: str, newcomers: tuple[str, ...] | None, names_a_person: bool) -> str:
    """Why a post that reaches new readers is refused where nobody can be asked: a scheduled run."""
    if newcomers is None:
        return (
            f"{target} reaches people who cannot read the conversation"
            f" this run delivers into, and they cannot be listed: it is"
            f" either a public channel the whole workspace can read or a"
            f" conversation shared outside this workspace. That is a"
            f" question for the person who asked, and a scheduled run has"
            f" nobody to ask. Post into the conversation the run delivers"
            f" into instead"
        )
    if names_a_person:
        return (
            f"a direct message with {target} would be read by somebody"
            f" this turn is not being run for, and this turn has nobody to"
            f" put that to -- a scheduled run has no requester. Post into"
            f" the conversation the run delivers into instead"
        )
    return (
        f"{len(newcomers)} member(s) of {target} are not in the"
        f" conversation this run delivers into, so posting there would show"
        f" this message to people who cannot read it. That is a question"
        f" for the person who asked, and this turn has nobody to ask -- a"
        f" scheduled run has no requester"
    )


#: Why an ephemeral post may not leave the conversation its turn came from.
EPHEMERAL_LEAVES_THIS_CONVERSATION = (
    "ephemeral_leaves_this_conversation",
    (
        "an ephemeral message is shown to one person inside one"
        " conversation and is the only kind of message nobody can go"
        " back and check, so it is confined to the conversation this"
        " turn came from"
    ),
)


def ephemeral_refusal(to: str, requester: str) -> tuple[str, str] | None:
    """Why an ephemeral post may not be shown to ``to``, or ``None`` when ``to`` is the turn's requester."""
    if not requester:
        return (
            "ephemeral_requires_a_requester",
            (
                "an ephemeral message can only be sent to the person whose"
                " message started this turn, and this turn was not started"
                " by anybody -- a scheduled run has no requester. Post an"
                " ordinary message instead"
            ),
        )
    if to != requester:
        return (
            "ephemeral_requires_the_requester",
            (
                "an ephemeral message can only be sent to the person whose"
                " message started this turn. It cannot be edited, cannot be"
                " deleted, never enters the conversation's history, and is"
                " invisible to everybody else -- including whoever asked --"
                " so one sent to the wrong person cannot be noticed or"
                " undone. Post an ordinary message, or address this person"
                " in it"
            ),
        )
    return None
