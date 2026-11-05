from belle_agent.message_decisions import KIND_DIRECT, KIND_PUBLIC, message_entry
from belle_agent.platforms.base import TRIGGER_DM, TRIGGER_MENTION, InboundMessage
from belle_agent.platforms.decision_facts import inbound_decision_facts

KINDS = {"D": KIND_DIRECT, "O": KIND_PUBLIC}


def _inbound(**fields):
    return InboundMessage(
        platform="p", workspace="W1", conversation="C1", message_id="M2", sender="U1", text="hello", **fields
    )


def test_a_direct_message_is_told_with_its_author_the_bot_and_how_it_addressed_the_bot():
    inbound = _inbound(chat_type="D", trigger=TRIGGER_DM, files=({"name": "a.md"},))

    assert inbound_decision_facts(inbound, author="@ann", bot_name="belle", kinds=KINDS) == {
        "channel": "C1",
        "bot_name": "belle",
        "last_message": message_entry("@ann", "hello", ts="M2", thread_ts="", file_count=1),
        "conversation_kind": KIND_DIRECT,
        "identity": {
            "workspace": "W1", "channel": "C1", "ts": "M2", "thread_ts": "", "user": "U1",
            "chat_type": "D", "is_dm": True, "trigger": TRIGGER_DM,
        },
    }


def test_a_message_in_a_thread_names_its_thread_and_is_no_direct_message():
    facts = inbound_decision_facts(
        _inbound(chat_type="O", trigger=TRIGGER_MENTION, thread="M1"), author="U1", bot_name="belle", kinds=KINDS
    )

    assert facts["last_message"]["thread_ts"] == facts["identity"]["thread_ts"] == "M1"
    assert facts["conversation_kind"] == KIND_PUBLIC
    assert facts["identity"]["is_dm"] is False


def test_a_chat_type_the_platform_names_no_kind_for_is_unknown():
    facts = inbound_decision_facts(_inbound(chat_type="G"), author="U1", bot_name="belle", kinds=KINDS)

    assert facts["conversation_kind"] == "unknown"


def test_platform_details_override_only_the_message_and_identity():
    last = message_entry("<@U1>", "<@BOT> hello", ts="M2", thread_ts="", attachment_count=1)
    identity = {"team": "W1", "channel": "C1", "ts": "M2"}

    facts = inbound_decision_facts(
        _inbound(chat_type="O"), author="<@U1>", bot_name="belle <@BOT>", kinds=KINDS,
        last_message=last, identity=identity,
    )

    assert facts == {
        "channel": "C1", "bot_name": "belle <@BOT>", "last_message": last,
        "conversation_kind": KIND_PUBLIC, "identity": identity,
    }


def test_loopback_preserves_original_message_and_private_conversation_facts():
    from belle_agent.message_decisions import KIND_PRIVATE
    from belle_agent.platforms.loopback.platform import LoopbackPlatform

    inbound = _inbound(chat_type="channel", trigger=TRIGGER_MENTION, thread="root", files=({"name": "photo"},))
    facts = LoopbackPlatform().decision_facts(inbound)

    assert facts == inbound_decision_facts(
        inbound, author=inbound.sender, bot_name="the assistant", kinds={"channel": KIND_PRIVATE}
    )
    assert facts["last_message"] == message_entry("U1", "hello", ts="M2", thread_ts="root", file_count=1)
    assert facts["identity"]["workspace"] == "W1"
    assert facts["identity"]["trigger"] == TRIGGER_MENTION
