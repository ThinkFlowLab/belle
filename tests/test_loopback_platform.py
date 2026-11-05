"""The loopback platform records what it is asked to do and refuses what leaves its conversation."""

import asyncio
import importlib
import json

import pytest

from belle_agent.backend import APPROVAL, Question
from belle_agent.config import BelleConfigError, load_config
from belle_agent.platforms.base import InboundMessage, create_platform
from belle_agent.platforms.loopback import control


@pytest.fixture
def platform(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("platforms: {loopback: {enabled: true}}\n", encoding="utf-8")
    return create_platform(load_config(path), "loopback")


def _toolkit(platform, metadata):
    (toolkit,) = platform.toolkits(lambda: metadata)
    return toolkit


def test_the_platform_is_built_from_the_platform_setting(platform):
    assert platform.name == "loopback"
    assert platform.capabilities.channel == "loopback"
    names = [definition["name"] for definition in _toolkit(platform, {}).get_tool_definitions()]
    assert names == ["post_message", "react_to_message", "read_file"]
    assert platform.posting_tool in names
    post_card = _toolkit(platform, {}).get_tool_definitions()[0]
    assert "confirmed destination" in post_card["description"]


def test_tool_definitions_have_the_shape_every_toolkit_uses(platform):
    for definition in _toolkit(platform, {}).get_tool_definitions():
        assert set(definition) == {"name", "description", "input_params"}
        assert definition["input_params"]["type"] == "object"
        assert set(definition["input_params"]["required"]) <= set(definition["input_params"]["properties"])


async def test_every_outbound_action_is_logged_in_order(platform):
    posted = await platform.post("C1", "root", "hello")
    await platform.post("C1", "", "only you", ephemeral_to="U1")
    await platform.react("C1", "m1", "eyes")
    await platform.react("C1", "m1", "eyes", remove=True)
    await platform.ask("C1", "root", Question(id="q1", text="Which one?", options=("a", "b")))
    await platform.ask("C1", "root", Question(id="q2", text="Allow rm?", kind=APPROVAL, tool="rm"))

    assert [action.kind for action in platform.log] == [
        "post",
        "ephemeral",
        "reaction",
        "reaction",
        "question",
        "approval",
    ]
    assert platform.log[0].message_id == posted
    assert (platform.log[1].to, platform.log[3].removed) == ("U1", True)
    assert platform.log[5].question.tool == "rm"


async def test_an_injected_record_reaches_the_host_and_its_outcome_comes_back(platform):
    received = []

    async def on_inbound(record):
        received.append(record)
        return "dispatched:dm"

    record = InboundMessage(platform="loopback", workspace="W1", conversation="D1", message_id="m1", sender="U1", text="hi")
    assert await platform.inject(record) == "dropped:platform-not-started"

    await platform.start(on_inbound)
    assert await platform.inject(record) == "dispatched:dm"
    assert received == [record]


async def test_the_toolkit_acts_only_in_the_conversation_its_metadata_names(platform):
    inbound = InboundMessage(
        platform="loopback", workspace="W1", conversation="C1", message_id="m1", sender="U1", text="hi", thread="root"
    )
    await platform.start(lambda _: asyncio.sleep(0, result="ok"))
    await platform.inject(inbound)
    toolkit = _toolkit(platform, platform.request_metadata(inbound))

    assert json.loads(await toolkit.post_message(text="leak", chat_id="C2"))["error"] == "outside_this_conversation"
    assert json.loads(await toolkit.post_message(text="  "))["error"] == "nothing_to_post"
    assert platform.log == []

    posted = json.loads(await toolkit.post_message(text="here", chat_id="C1"))
    reacted = json.loads(await toolkit.react_to_message(emoji=":eyes:"))

    assert (posted["ok"], posted["chat_id"]) == (True, "C1")
    assert (reacted["message_id"], reacted["emoji"]) == ("m1", "eyes")
    assert [(action.kind, action.conversation, action.thread) for action in platform.log] == [
        ("post", "C1", "root"),
        ("reaction", "C1", ""),
    ]
    assert [action.workspace for action in platform.log] == ["W1", "W1"]


async def test_posts_resolve_reply_ids_only_inside_the_workspace_and_conversation(platform):
    first = InboundMessage("loopback", "W1", "C1", "m1", "U1", "root")
    second = InboundMessage("loopback", "W2", "C1", "m1", "U1", "root")
    child = InboundMessage("loopback", "W1", "C1", "m2", "U1", "child", thread="m1")
    other_conversation = InboundMessage("loopback", "W1", "C2", "m3", "U1", "other")
    await platform.start(lambda _: asyncio.sleep(0, result="ok"))
    await platform.inject(first)
    await platform.inject(second)
    await platform.inject(child)
    await platform.inject(other_conversation)

    w1 = _toolkit(platform, platform.request_metadata(first))
    w2 = _toolkit(platform, platform.request_metadata(second))
    missing = json.loads(await w1.post_message(text="bad", reply_to_message_id="absent"))
    foreign = json.loads(await w2.post_message(text="bad", reply_to_message_id="m2"))
    other = json.loads(await w1.post_message(text="bad", reply_to_message_id="m3"))
    assert missing["error"] == foreign["error"] == other["error"] == "reply_target_not_found"
    assert platform.log == []

    posted = json.loads(await w1.post_message(text="reply", reply_to_message_id="m2"))
    top_level = json.loads(await w2.post_message(text="top level"))
    assert posted == {
        "ok": True,
        "chat_id": "C1",
        "message_id": "out-1",
        "part_message_ids": ["out-1"],
        "parts": 1,
        "thread_id": "m1",
    }
    assert top_level["chat_id"] == "C1" and top_level["thread_id"] == ""
    assert [(item.workspace, item.conversation, item.thread, item.message_id) for item in platform.log] == [
        ("W1", "C1", "m1", "out-1"),
        ("W2", "C1", "", "out-2"),
    ]


async def test_reactions_refuse_unknown_and_foreign_messages_without_logging(platform):
    own = InboundMessage("loopback", "W1", "C1", "m1", "U1", "own")
    foreign_workspace = InboundMessage("loopback", "W2", "C1", "m2", "U1", "foreign")
    foreign_conversation = InboundMessage("loopback", "W1", "C2", "m3", "U1", "foreign")
    await platform.start(lambda _: asyncio.sleep(0, result="ok"))
    for record in (own, foreign_workspace, foreign_conversation):
        await platform.inject(record)
    toolkit = _toolkit(platform, platform.request_metadata(own))

    for message_id in ("missing", "m2", "m3"):
        refused = json.loads(await toolkit.react_to_message(emoji="eyes", message_id=message_id))
        assert refused["error"] == "reaction_target_not_found"
    assert platform.log == []
    assert json.loads(await toolkit.react_to_message(emoji="eyes", message_id="m1"))["ok"] is True
    assert [item.message_id for item in platform.log] == ["m1"]


async def test_a_confirmable_destination_is_described_and_keeps_post_text(platform):
    inbound = InboundMessage("loopback", "W1", "C1", "m1", "U1", "hello")
    platform.confirmable.add(("W1", "C2"))
    toolkit = _toolkit(platform, platform.request_metadata(inbound))

    assert await toolkit.widening_question("post_message", {"chat_id": "C2", "text": "hello"})
    posted = json.loads(await toolkit.post_message(text="  hello  ", chat_id="C2"))
    assert posted["ok"] and posted["chat_id"] == "C2"
    assert platform.log[-1].text == "  hello  "
    assert json.loads(await toolkit.post_message(text=7))["error"] == "text_invalid"
    assert json.loads(await toolkit.post_message(text="hello", chat_id=7))["error"] == "chat_id_invalid"
    assert json.loads(await toolkit.react_to_message(emoji="eyes", remove="false"))["error"] == "remove_invalid"
    assert len(platform.log) == 1


async def test_a_tool_can_reply_to_a_message_the_host_posted(platform):
    inbound = InboundMessage("loopback", "W1", "C1", "m1", "U1", "hello")
    host_message = await platform.post("C1", "", "host answer", workspace="W1")
    toolkit = _toolkit(platform, platform.request_metadata(inbound))

    result = json.loads(await toolkit.post_message(text="follow-up", reply_to_message_id=host_message))

    assert result["ok"] and result["thread_id"] == host_message
    assert platform.log[-1].thread == host_message


async def test_the_toolkit_refuses_a_call_that_belongs_to_no_conversation(platform):
    toolkit = _toolkit(platform, {})

    for result in (await toolkit.post_message(text="hello"), await toolkit.react_to_message(emoji="eyes")):
        assert json.loads(result)["error"] == "trusted_conversation_context_required"
    assert platform.log == []


# The module that holds the port sits beside the platforms and is not one.
@pytest.mark.parametrize("name", ["nowhere", "base"])
def test_a_platform_without_a_connector_is_a_configuration_error(name):
    with pytest.raises(BelleConfigError, match=f"platforms.{name} names no platform connector"):
        create_platform({}, name)


async def test_a_person_drives_the_platform_through_its_control_socket(tmp_path, capsys, monkeypatch):
    path = tmp_path / "config.yaml"
    socket = tmp_path / "control.sock"
    monkeypatch.setenv("SOCKET_DIRECTORY", str(tmp_path))
    path.write_text("platforms: {loopback: {enabled: true, control: '${SOCKET_DIRECTORY}/control.sock'}}\n", encoding="utf-8")
    platform = create_platform(load_config(path), "loopback")
    received: list[InboundMessage] = []

    async def on_inbound(record):
        received.append(record)
        await platform.post(record.conversation, record.thread, f"echo {record.text}")
        return "dispatched"

    await platform.start(on_inbound)
    try:
        for arguments in (["dm", "U1", "hello"], ["channel", "C1", "U2", "anyone?"], ["channel", "--mention", "C1", "U2", "you"], ["log"]):
            assert await asyncio.to_thread(control.main, ["--socket", str(socket), *arguments]) == 0
    finally:
        await platform.stop()

    assert [(m.conversation, m.sender, m.chat_type, m.trigger, m.text) for m in received] == [
        ("D-U1", "U1", "dm", "dm", "hello"),
        ("C1", "U2", "channel", "", "anyone?"),
        ("C1", "U2", "channel", "mention", "you"),
    ]
    assert len({m.message_id for m in received}) == 3 and all(m.workspace for m in received)
    assert received[0].workspace in platform.workspaces
    assert await platform.can_read("U1", "D-U1", workspace=received[0].workspace)
    target = await platform.check_place("D-U1", "new", workspace=received[0].workspace)
    assert target.exists and target.member
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert printed[:3] == [{"outcome": "dispatched"}] * 3
    assert [(action["kind"], action["conversation"], action["text"]) for action in printed[3:]] == [
        ("post", "D-U1", "echo hello"),
        ("post", "C1", "echo anyone?"),
        ("post", "C1", "echo you"),
    ]
    # Stopped, the platform listens nowhere.
    assert not socket.exists()
    restarted = create_platform(load_config(path), "loopback")
    assert await restarted.can_read("U1", "D-U1", workspace=control.WORKSPACE)
    assert not await restarted.can_read("U2", "D-U1", workspace=control.WORKSPACE)
    target = await restarted.check_place("D-U1", "new", workspace=control.WORKSPACE)
    assert target.exists and target.member
    assert not (await restarted.check_place("D-U1", "old-thread", workspace=control.WORKSPACE)).exists


def test_a_message_injected_by_hand_gets_an_id_no_earlier_process_gave():
    fields = {"conversation": "C1", "sender": "U1", "text": "hello"}
    first = [control.inbound("loopback", "channel", fields).message_id for _ in range(3)]
    # A restart is a module loaded again. The host still remembers the ids it handled before it.
    again = importlib.reload(control)
    second = [again.inbound("loopback", "channel", fields).message_id for _ in range(3)]

    assert len(set(first + second)) == 6


def test_a_platform_with_no_control_setting_listens_nowhere(platform):
    assert platform._control_path == ""
