"""The ``!belle`` command on Matrix: what is a command, the answers in the command's thread, and private questions."""

import json
import time
from types import SimpleNamespace

import pytest
from peer import USER, Peer, Room

from belle_agent.backend import BackendCapabilities, Catalog, TurnScope
from belle_agent.click_notices import STOP_ACCEPTED_NOTICE
from belle_agent.models import ConfiguredModels
from belle_agent.platforms.base import InboundDrop, InboundMessage, InboundStop
from belle_agent.platforms.matrix.connector import MatrixPlatform
from belle_agent.platforms.matrix.settings import MatrixSettings

ROOM, DM = "!lounge:syn.example", "!dm-bob:syn.example"
BOB = "@bob:syn.example"
RUNNING, WAITING = "$running", "$waiting"


def _turn(message_id: str, text: str = "a long question") -> SimpleNamespace:
    return SimpleNamespace(inbound=SimpleNamespace(message_id=message_id, sender=BOB, text=text, reply_thread=message_id))


class Host:
    """The host's command operations, as a connector's command surface reaches them."""

    backend_capabilities = BackendCapabilities(
        backend="scripted", catalog=frozenset({"models", "skills", "subagents", "experts"}),
        scope_fields=frozenset({"skills_required", "subagent_required"}),
    )

    def __init__(self) -> None:
        self.model_choices: dict = {}
        self.allowed, self.served = True, True

    def admits(self, user):
        return self.allowed

    def serves(self, room):
        return self.served

    def command_unavailable(self, words):
        return ""

    def running(self, workspace, room):
        return [_turn(RUNNING)]

    def waiting(self, workspace, room):
        return [_turn(WAITING, "later please")]

    def held(self, workspace, room):
        return 0

    def settled(self, at):
        return TurnScope(skills_available=frozenset({"notes"}))

    def scoped_model(self, at):
        return ""

    async def catalog(self):
        return Catalog(skills=({"name": "notes", "description": "Keep notes.", "installed": True},),
                       subagents=("reviewer",), models=ConfiguredModels(names=("fast", "deep")))


@pytest.fixture
def world():
    peer = Peer()
    peer.rooms[DM] = Room(direct=True, joined_members=2)
    peer.dms[BOB] = DM
    settings = MatrixSettings(homeserver="http://syn:8008", user_id=USER, store_dir="/s", store_passphrase="p", typing_indicator=False)
    platform = MatrixPlatform(settings, helper_factory=peer.factory)
    platform._helper, platform.user_id = peer, USER
    host, records = Host(), []

    async def receive(record):
        records.append(record)
        return "dispatched:turn" if isinstance(record, InboundMessage) else "stopped session=s"

    platform.bind(host)
    platform._on_inbound = receive
    return SimpleNamespace(platform=platform, peer=peer, host=host, records=records)


async def send(world, body: str, *, room: str = ROOM, html: str = "", sender: str = BOB) -> str:
    content = {"msgtype": "m.text", "body": body}
    if html:
        content.update(format="org.matrix.custom.html", formatted_body=html)
    event_id = f"$c{time.monotonic_ns()}"
    event = {"event": "timeline", "seq": 1, "room": room, "encrypted": True, "direct": room == DM,
             "room_event": {"type": "m.room.message", "sender": sender, "event_id": event_id,
                            "origin_server_ts": int(time.time() * 1000), "content": content}}
    world.event_id = event_id
    return await world.platform.handle(event)


def answers(world, room: str = ROOM) -> list[dict]:
    return [r["content"] for r in world.peer.sent() if r["room"] == room]


@pytest.mark.parametrize("body", ["!belle help", "/belle help", "!BELLE help"])
async def test_a_command_is_answered_in_its_thread_in_reply_and_never_reaches_the_host_as_a_message(world, body):
    assert await send(world, body) == "answered:help"
    (answer,) = answers(world)
    assert answer["body"].startswith("**Belle commands**") and "`!belle ask <question>`" in answer["body"]
    assert answer["m.relates_to"] == {"m.in_reply_to": {"event_id": world.event_id}, "rel_type": "m.thread",
                                      "event_id": world.event_id, "is_falling_back": False}
    assert answer["m.mentions"] == {} and world.records == []


async def test_a_command_after_a_mention_of_the_bot_counts_and_one_later_in_the_text_does_not(world):
    html = f'<a href="https://matrix.to/#/{USER}">belle</a>: !belle status'
    assert await send(world, "belle: !belle status", html=html) == "answered:status"
    assert await send(world, f"{USER} !belle status") == "answered:status"
    assert (await send(world, "please run !belle status")).startswith("dispatched")
    assert (await send(world, "!other help")).startswith("dispatched")


async def test_schedule_command_uses_the_authenticated_matrix_room(world):
    calls = []

    class Toolkit:
        async def invoke_at(self, name, arguments, *, at):
            calls.append((name, arguments, at))
            return json.dumps({"ok": True, "jobs": []})

    world.host.schedule_toolkit = Toolkit()
    assert await send(world, "!belle schedule list") == "answered:schedule-list"
    assert "No scheduled jobs" in answers(world)[-1]["body"]
    name, arguments, at = calls[0]
    assert (name, arguments, at.platform, at.workspace, at.conversation, at.sender) == (
        "schedule_list", {}, "matrix", "default", ROOM, BOB
    )


async def test_schedule_command_refuses_when_the_belle_engine_is_unavailable(world):
    world.host.command_unavailable = lambda words: "Belle scheduling is not active" if words[0] == "schedule" else ""
    assert await send(world, "!belle schedule list") == "refused:unavailable"
    assert "Belle scheduling is not active" in answers(world)[-1]["body"]


async def test_a_person_who_may_not_ask_gets_the_refused_mark_and_an_unserved_room_gets_nothing(world):
    world.host.allowed = False
    assert await send(world, "!belle help") == "refused:allow_from"
    assert [r["type"] for r in world.peer.sent("m.reaction")] == ["m.reaction"] and answers(world) == []
    world.host.allowed, world.host.served = True, False
    assert await send(world, "!belle help") == "refused:room-not-served"
    assert world.peer.sent() == []


async def test_stop_names_a_message_by_id_or_by_link_and_the_host_decides(world):
    link = f"https://matrix.to/#/{ROOM}/{RUNNING.replace('$', '%24')}?via=syn.example"
    assert (await send(world, f"!belle stop {link}")).startswith("stopped")
    assert world.records == [InboundStop("matrix", "default", ROOM, RUNNING, BOB)]
    assert answers(world)[-1]["body"] == STOP_ACCEPTED_NOTICE
    assert await send(world, "!belle stop not-an-id") == "refused:no-message-id"


async def test_the_queue_is_listed_and_a_waiting_message_removed(world):
    assert await send(world, "!belle queue list") == "answered:queue-list"
    assert f"`{WAITING}` by {BOB}: later please" in answers(world)[-1]["body"]
    await send(world, f"!belle queue drop {WAITING}")
    assert world.records == [InboundDrop("matrix", "default", ROOM, WAITING, BOB)]


async def test_a_private_question_from_a_room_is_asked_in_the_direct_room_and_nothing_of_it_goes_to_the_room(world):
    assert (await send(world, "!belle ask what is my secret plan")).startswith("asked:dispatched")

    (anchor,) = answers(world, DM)
    assert "what is my secret plan" in anchor["body"] and f"https://matrix.to/#/{ROOM}" in anchor["body"]
    anchor_id = next(event_id for event_id, event in world.peer.events.items() if event["content"] is anchor)
    (asked,) = world.records
    assert (asked.conversation, asked.message_id, asked.chat_type, asked.trigger) == (DM, anchor_id, "direct", "dm")
    assert (asked.thread, asked.reply_thread, asked.side) == (anchor_id, anchor_id, anchor_id)
    assert asked.text.endswith("what is my secret plan")
    (told,) = answers(world)
    assert told["body"] == "Answering in your direct room with Belle. Nothing is posted here."
    # A reply in that thread of the direct room belongs to the private conversation.
    assert world.platform.commands.side(DM, anchor_id) == anchor_id


async def test_a_private_question_in_the_direct_room_stays_at_its_top_level(world):
    await send(world, "!belle skills use notes keep this", room=DM)
    (asked,) = world.records
    assert (asked.conversation, asked.thread, asked.side, asked.skills_required) == (DM, "", "", ("notes",))


async def test_the_models_and_the_catalogs_are_answered(world):
    assert await send(world, "!belle models list") == "answered:models-list"
    assert "fast" in answers(world)[-1]["body"]
    assert await send(world, "!belle skills list") == "answered:skills-list"
    assert "notes" in answers(world)[-1]["body"]
