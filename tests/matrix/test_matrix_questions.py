"""Questions as polls on the peer helper: what is asked, what a vote becomes, and what each outcome shows."""

import time

import pytest
from peer import USER, Peer, Room

from belle_agent.backend import APPROVAL, Question
from belle_agent.click_notices import ANSWER_REFUSED_NOTICE, WITHDRAWN_QUESTION_NOTE
from belle_agent.platforms.base import InboundAnswer, QuestionRefused
from belle_agent.platforms.matrix.connector import MatrixPlatform
from belle_agent.platforms.matrix.questions import POLL_END, POLL_RESPONSE, POLL_START, TEXT
from belle_agent.platforms.matrix.settings import MatrixSettings
from belle_agent.tools.host import APPROVE, REJECT

ROOM = "!lounge:syn.example"
ALICE, CAROL = "@alice:syn.example", "@carol:syn.example"
APPROVE_POST = Question(id="q-1", text="May I post in town?", options=(APPROVE, REJECT), kind=APPROVAL, tool="post_message")


class Host:
    """The host's receiver: it records each answer and gives the outcome a check sets."""

    def __init__(self) -> None:
        self.answers: list[InboundAnswer] = []
        self.outcome = "answered session=s"

    async def __call__(self, record):
        self.answers.append(record)
        return self.outcome


def matrix(peer: Peer, host: Host) -> MatrixPlatform:
    settings = MatrixSettings(homeserver="http://syn:8008", user_id=USER, store_dir="/s", store_passphrase="p")
    platform = MatrixPlatform(settings, helper_factory=peer.factory)
    platform._helper, platform.user_id, platform._on_inbound = peer, USER, host
    return platform


@pytest.fixture
def peer() -> Peer:
    found = Peer()
    found.rooms[ROOM] = Room()
    return found


def vote(poll: str, sender: str, *answers: str, age: float = 0.0) -> dict:
    content = {"m.relates_to": {"rel_type": "m.reference", "event_id": poll}, POLL_RESPONSE: {"answers": list(answers)}}
    event = {"type": POLL_RESPONSE, "sender": sender, "event_id": f"$vote-{sender}-{answers}",
             "origin_server_ts": int((time.time() - age) * 1000), "content": content}
    return {"event": "timeline", "seq": 1, "room": ROOM, "encrypted": True, "direct": False, "room_event": event}


def notes(peer: Peer) -> list[dict]:
    return [request["content"] for request in peer.sent() if "m.in_reply_to" in request["content"]["m.relates_to"]]


async def test_an_approval_is_a_single_answer_poll_in_the_thread_of_the_turn(peer):
    poll = await matrix(peer, Host()).ask(ROOM, "$root", APPROVE_POST)
    (sent,) = peer.sent(POLL_START)
    start = sent["content"][POLL_START]
    assert peer.events[poll]["content"] is sent["content"]
    assert start["question"][TEXT] == "May I post in town?" and start["max_selections"] == 1
    assert [answer[TEXT] for answer in start["answers"]] == ["Approve", "Reject"]
    assert sent["content"]["m.relates_to"] == {"rel_type": "m.thread", "event_id": "$root"}


@pytest.mark.parametrize(("options", "said"), [((), "no options"), (tuple(f"o{n}" for n in range(21)), "at most 20 options")])
async def test_a_question_a_poll_cannot_hold_is_refused(peer, options, said):
    with pytest.raises(QuestionRefused, match=said):
        await matrix(peer, Host()).ask(ROOM, "", Question(id="q", text="?", options=options))
    assert peer.sent(POLL_START) == []


async def test_a_vote_answers_and_the_poll_ends_with_who_answered(peer):
    host = Host()
    platform = matrix(peer, host)
    poll = await platform.ask(ROOM, "$root", APPROVE_POST)

    assert (await platform.handle(vote(poll, ALICE, "0"))).startswith("answered")

    (answer,) = host.answers
    assert (answer.sender, answer.question_id, answer.conversation) == (ALICE, "q-1", ROOM)
    assert answer.answer.values == (APPROVE,) and answer.answer.approved is True
    (end,) = peer.sent(POLL_END)
    assert end["content"]["m.relates_to"] == {"rel_type": "m.reference", "event_id": poll}
    (note,) = notes(peer)
    assert note["body"] == f"Answered by {ALICE}: Approve" and note["m.mentions"] == {}
    assert note["m.relates_to"]["event_id"] == "$root" and note["m.relates_to"]["m.in_reply_to"] == {"event_id": poll}

    # A later vote finds the poll ended: nothing is answered and nothing is sent again.
    assert await platform.handle(vote(poll, CAROL, "1")) == "ignored:question-not-waiting"
    assert len(host.answers) == 1 and len(peer.sent(POLL_END)) == 1


async def test_a_refused_vote_is_told_once_to_its_voter_and_the_poll_stays_open(peer):
    host = Host()
    host.outcome = "refused:clicks.approve=user"
    platform = matrix(peer, host)
    poll = await platform.ask(ROOM, "", APPROVE_POST)

    await platform.handle(vote(poll, CAROL, "0"))
    await platform.handle(vote(poll, CAROL, "1"))

    assert [note["body"] for note in notes(peer)] == [f"{CAROL}: {ANSWER_REFUSED_NOTICE}"]
    assert peer.sent(POLL_END) == []
    # Control: an admitted voter still answers the same poll.
    host.outcome = "answered session=s"
    assert (await platform.handle(vote(poll, ALICE, "1"))).startswith("answered")
    assert host.answers[-1].answer.approved is False


async def test_a_vote_the_host_no_longer_waits_for_ends_the_poll_as_withdrawn(peer):
    host = Host()
    host.outcome = "ignored:question-not-waiting"
    platform = matrix(peer, host)
    poll = await platform.ask(ROOM, "", APPROVE_POST)
    await platform.handle(vote(poll, ALICE, "0"))
    assert [note["body"] for note in notes(peer)] == [WITHDRAWN_QUESTION_NOTE] and len(peer.sent(POLL_END)) == 1


async def test_after_a_restart_a_vote_on_an_earlier_poll_ends_it_as_withdrawn(peer):
    poll = await matrix(peer, Host()).ask(ROOM, "$root", APPROVE_POST)
    host = Host()

    assert await matrix(peer, host).handle(vote(poll, ALICE, "0")) == "ignored:question-not-waiting"

    assert host.answers == []
    (note,) = notes(peer)
    assert note["body"] == WITHDRAWN_QUESTION_NOTE and note["m.relates_to"]["event_id"] == "$root"


async def test_a_vote_on_somebody_elses_poll_or_an_old_vote_is_left_alone(peer):
    theirs = peer.add(ROOM, ALICE, {POLL_START: {"answers": []}}, event_type=POLL_START)
    platform = matrix(peer, Host())
    assert await platform.handle(vote(theirs, CAROL, "0")) == "ignored:not-a-poll-of-this-bot"
    poll = await platform.ask(ROOM, "", APPROVE_POST)
    assert await platform.handle(vote(poll, ALICE, "0", age=3600)) == "ignored:older-than-catch-up"
    assert peer.sent(POLL_END) == [] and notes(peer) == []
