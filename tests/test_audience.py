"""Audience bounds use plain facts independently of platform payloads."""

import pytest

from belle_agent.audience import (
    Audience,
    PlaceWords,
    audience_newcomers,
    audience_within,
    ephemeral_refusal,
    read_permitted,
    read_refusal,
    unattended_write_refusal,
    widening_question,
    workspace_question,
    write_permitted,
)


@pytest.mark.parametrize(
    "inner,outer,permitted",
    [
        (Audience(False, frozenset({"alice"}), False), Audience(False, frozenset({"alice", "bob"}), False), True),
        (Audience(False, frozenset({"bob"}), False), Audience(False, frozenset({"alice"}), False), False),
        (Audience(False, frozenset({"alice"}), True), Audience(True, frozenset(), False), False),
        (Audience(False, frozenset({"alice"}), False), Audience(True, frozenset(), False), True),
        (Audience(True, frozenset(), False), Audience(False, frozenset({"alice"}), False), False),
    ],
)
def test_audience_bounds_and_information_flow(inner, outer, permitted):
    assert audience_within(inner, outer) is permitted
    assert read_permitted(inner, outer) is permitted
    assert write_permitted(outer, inner) is permitted


def test_newcomers_are_sorted_and_workspace_expansion_is_unbounded():
    original = Audience(False, frozenset({"alice"}), False)
    expanded = Audience(False, frozenset({"alice", "charlie", "bob"}), False)
    workspace = Audience(True, frozenset(), False)
    assert audience_newcomers(expanded, original) == ("bob", "charlie")
    assert audience_newcomers(workspace, original) is None
    assert audience_newcomers(original, workspace) is None


PUBLIC = Audience(True, frozenset(), False)
SHARED = Audience(False, frozenset({"alice", "outsider"}), True)
SMALL = Audience(False, frozenset({"alice"}), False)
PAIR = Audience(False, frozenset({"alice", "bob"}), False)


@pytest.mark.parametrize(
    "source,target,code",
    [
        (SMALL, PAIR, None),
        (PAIR, PUBLIC, None),
        (PUBLIC, PAIR, "source_is_public_and_target_is_not"),
        (PAIR, SHARED, "target_reaches_outside_workspace"),
        (PAIR, SMALL, "source_members_not_in_target"),
    ],
)
def test_a_read_refusal_names_why_and_counts_who_blocks_it(source, target, code):
    refused = read_refusal(source, target, "here", "there")

    assert (refused and refused[0]) == code
    if code == "source_members_not_in_target":
        # One member blocks, and is counted, not named.
        assert refused[1].startswith("1 member(s) of here are not in there") and "bob" not in refused[1]


def test_a_question_about_a_widening_post_names_five_people_and_counts_the_rest():
    outsiders = tuple(f"u{index}" for index in range(7))

    question = widening_question("there", outsiders, lambda who: f"@{who}")

    assert "by 7 people who are not in this conversation: @u0, @u1, @u2, @u3, @u4 and 2 more." in question
    assert "1 person who is not" in widening_question("there", ("u0",), str)


@pytest.mark.parametrize(
    "newcomers,names_a_person,start",
    [(None, False, "there reaches people"), (("u1",), True, "a direct message with there"), (("u1", "u2"), False, "2 member(s)")],
)
def test_a_post_nobody_can_be_asked_about_is_refused_with_who_it_would_reach(newcomers, names_a_person, start):
    assert unattended_write_refusal("there", newcomers, names_a_person).startswith(start)


def test_an_ephemeral_post_goes_only_to_the_requester():
    assert ephemeral_refusal("u1", "u1") is None
    assert ephemeral_refusal("u1", "")[0] == "ephemeral_requires_a_requester"
    assert ephemeral_refusal("u2", "u1")[0] == "ephemeral_requires_the_requester"


def test_the_public_sentences_take_the_words_of_the_platform():
    words = PlaceWords("public room", "anybody who can join the room", "anybody who can join a public room")

    assert workspace_question("town") == (
        "This message would go to town, which is a public channel. Anybody in this workspace can read it there, now"
        " or after they join. This conversation is not public. Post it there?"
    )
    question = workspace_question("town", words)
    assert "which is a public room. Anybody who can join the room can read it there" in question
    _, detail = read_refusal(Audience(True, frozenset(), False), Audience(False, frozenset({"bob"}), False), "here", "there", words)
    assert detail.startswith("here is a public room, so anybody who can join the room can read")
    assert "there is not readable by anybody who can join a public room" in detail and "workspace" not in detail
