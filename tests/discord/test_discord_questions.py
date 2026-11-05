"""Questions on Discord: buttons, a select, a form for free text, the Stop button, and what each click becomes."""

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
from discord_transport import BOT_ID, TOKEN, FakeGateway, FakeRest, hello, ready

from belle_agent.backend import APPROVAL, Question
from belle_agent.click_notices import (
    ANSWER_REFUSED_NOTICE,
    STOP_ACCEPTED_NOTICE,
    STOP_STALE_NOTICE,
    WITHDRAWN_QUESTION_NOTE,
)
from belle_agent.platforms.base import InboundAnswer, InboundStop, QuestionRefused, affordance_contradictions
from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.questions import (
    BUTTON,
    DEFERRED_UPDATE,
    EPHEMERAL,
    INTERACTION_COMPONENT,
    INTERACTION_MODAL,
    MODAL,
    NO_OPTION_NOTICE,
    NO_PERSON_NOTICE,
    QUESTION_OPTIONS_MAX,
    STRING_SELECT,
)
from belle_agent.platforms.discord.settings import DiscordSettings

APP = "1000000000000000116"
GUILD = "1000000000000000200"
CHANNEL = "1000000000000000300"
THREAD = "1000000000000000400"
ALICE = "1000000000000000700"
MESSAGE = "1000000000000000800"
INTERACTION = "1000000000000000900"
SECRET = "aW50ZXJhY3Rpb24tdG9rZW4tc2VjcmV0"


@asynccontextmanager
async def _running_platform():
    rest = FakeRest(serve=True)
    rest.gateway()
    rest.channels[CHANNEL] = {"id": CHANNEL, "type": 0, "guild_id": GUILD}
    rest.channels[THREAD] = {"id": THREAD, "type": 11, "parent_id": CHANNEL, "owner_id": BOT_ID, "guild_id": GUILD}

    def opens(socket):
        socket.feed(hello())
        socket.feed(ready())

    client = DiscordClient(TOKEN, transport=rest.transport(), open_socket=FakeGateway(opens).open)
    found = DiscordPlatform(DiscordSettings(token=TOKEN, typing_indicator=False), client=client)
    found.received = []
    found.outcome = "answered:test"

    async def on_inbound(record):
        found.received.append(record)
        return found.outcome

    await found.start(on_inbound)
    found.rest = rest
    try:
        yield found
    finally:
        await found.stop()


@pytest.fixture
async def platform():
    async with _running_platform() as found:
        yield found


def _click(custom_id, *, user=ALICE, channel=THREAD, kind=INTERACTION_COMPONENT, **data):
    interaction = {"id": INTERACTION, "application_id": APP, "type": kind, "token": SECRET, "guild_id": GUILD,
                   "channel_id": channel, "data": {"custom_id": custom_id, **data}}
    if user:
        interaction["member"] = {"user": {"id": user, "username": "alice"}}
    return interaction


def _components(rest):
    (_, body, _) = rest.posted[-1]
    return body["components"][0]["components"]


def _sent(rest, method, part):
    return [request.body for request in rest.requests if request.method == method and part in request.path]


async def test_a_question_with_few_options_is_one_button_each_in_the_thread_of_its_turn(platform):
    message_id = await platform.ask(CHANNEL, THREAD, Question("q1", "Which colour?", ("red", "blue")), workspace=GUILD)

    channel, body, posted_id = platform.rest.posted[-1]
    assert (channel, body["content"], message_id) == (THREAD, "Which colour?", posted_id)
    assert [(button["type"], button["label"]) for button in _components(platform.rest)] == [(BUTTON, "red"), (BUTTON, "blue")]
    assert all(len(button["custom_id"]) <= 100 and "red" not in button["custom_id"] for button in _components(platform.rest))


@pytest.mark.parametrize("kind,conversation", [(11, CHANNEL), (12, THREAD)])
async def test_a_click_is_acknowledged_first_then_answers_and_the_buttons_come_off(platform, kind, conversation):
    platform.rest.channels[THREAD]["type"] = kind
    await platform.ask(conversation, THREAD, Question("q1", "Which colour?", ("red", "blue")), workspace=GUILD)
    blue = _components(platform.rest)[1]["custom_id"]
    before_click = len(platform.rest.requests)

    outcome = await platform.questions.on_interaction(_click(blue))

    assert outcome == "answered:test"
    assert platform.rest.requests[before_click].path == f"/interactions/{INTERACTION}/{SECRET}/callback"
    assert _sent(platform.rest, "POST", "/callback") == [{"type": DEFERRED_UPDATE}]
    (answer,) = platform.received
    assert isinstance(answer, InboundAnswer)
    assert (answer.question_id, answer.answer.values, answer.conversation, answer.workspace, answer.sender) == (
        "q1", ("blue",), conversation, GUILD, ALICE)
    assert _sent(platform.rest, "PATCH", "/messages/@original") == [
        {"components": [], "embeds": [{"description": "Answered by @alice: blue"}]}]
    assert await platform.questions.on_interaction(_click(blue)) == "ignored:question-not-waiting"
    assert len(platform.received) == 1


async def test_an_approval_is_two_buttons_and_its_verdict_reaches_the_host(platform):
    await platform.ask(CHANNEL, THREAD, Question("q2", "Run it?", ("approve", "reject"), kind=APPROVAL), workspace=GUILD)
    approve, reject = _components(platform.rest)

    await platform.questions.on_interaction(_click(reject["custom_id"]))

    assert (approve["label"], approve["style"], reject["label"], reject["style"]) == ("Approve", 1, "Reject", 4)
    assert platform.received[0].answer.approved is False


async def test_more_than_five_options_are_one_select_and_its_choice_is_the_option(platform):
    options = tuple(f"option {n}" for n in range(QUESTION_OPTIONS_MAX))
    await platform.ask(CHANNEL, THREAD, Question("q3", "Pick one", options), workspace=GUILD)
    (select,) = _components(platform.rest)

    await platform.questions.on_interaction(_click(select["custom_id"], values=["7"]))

    assert (select["type"], len(select["options"]), select["max_values"]) == (STRING_SELECT, 25, 1)
    assert platform.received[0].answer.values == ("option 7",)


async def test_a_malformed_multi_value_select_does_not_answer_or_close_the_question(platform):
    await platform.ask(CHANNEL, THREAD, Question("q-select", "Pick one", tuple(f"option {n}" for n in range(6))), workspace=GUILD)
    select = _components(platform.rest)[0]

    outcome = await platform.questions.on_interaction(_click(select["custom_id"], values=["1", "2"]))

    assert outcome == "refused:no-such-option" and platform.received == []
    assert _sent(platform.rest, "PATCH", "/messages/@original") == []
    assert _sent(platform.rest, "POST", f"/webhooks/{APP}/{SECRET}")[-1]["content"] == NO_OPTION_NOTICE


async def test_a_question_discord_cannot_show_is_refused_by_name_and_nothing_is_posted(platform):
    with pytest.raises(QuestionRefused, match="at most 25 options"):
        await platform.ask(CHANNEL, THREAD, Question("q4", "Pick", tuple(str(n) for n in range(26))), workspace=GUILD)
    with pytest.raises(QuestionRefused, match="option 1 of a Discord question must be 1 to 100 characters; it has 101"):
        await platform.ask(CHANNEL, THREAD, Question("q5", "Pick", ("short", "x" * 101)), workspace=GUILD)

    assert platform.rest.posted == []


async def test_a_label_too_long_for_a_button_makes_the_question_a_select(platform):
    await platform.ask(CHANNEL, THREAD, Question("q6", "Pick", ("short", "y" * 90)), workspace=GUILD)

    assert _components(platform.rest)[0]["type"] == STRING_SELECT


async def test_a_question_with_no_options_opens_a_form_and_its_text_is_the_answer(platform):
    await platform.ask(CHANNEL, THREAD, Question("q7", "What should the title be?"), workspace=GUILD)
    (button,) = _components(platform.rest)

    opened = await platform.questions.on_interaction(_click(button["custom_id"]))
    (callback,) = _sent(platform.rest, "POST", "/callback")
    form = callback["data"]
    text_input = form["components"][0]["components"][0]
    submitted = _click(form["custom_id"], kind=INTERACTION_MODAL,
                       components=[{"type": 1, "components": [{"type": 4, "custom_id": text_input["custom_id"],
                                                                "value": "Balloon notes"}]}])
    outcome = await platform.questions.on_interaction(submitted)

    assert (opened, callback["type"], text_input["max_length"], text_input["required"]) == (
        "asked:text-form-opened", MODAL, 4000, True)
    assert outcome == "answered:test" and platform.received[0].answer.values == ("Balloon notes",)
    assert _sent(platform.rest, "POST", "/callback")[-1] == {"type": DEFERRED_UPDATE}


async def test_a_refused_click_is_told_to_the_clicker_alone_and_the_question_stays_open(platform):
    await platform.ask(CHANNEL, THREAD, Question("q8", "Which?", ("a", "b")), workspace=GUILD)
    first = _components(platform.rest)[0]["custom_id"]
    platform.outcome = "refused:clicks-rule"

    await platform.questions.on_interaction(_click(first, user="1000000000000000711"))
    assert platform.received[0].sender == "1000000000000000711"
    platform.outcome = "answered:test"
    second = await platform.questions.on_interaction(_click(first))

    (told,) = _sent(platform.rest, "POST", f"/webhooks/{APP}/{SECRET}")
    assert (told["content"], told["flags"]) == (ANSWER_REFUSED_NOTICE, EPHEMERAL)
    assert second == "answered:test"


async def test_a_click_that_names_no_person_answers_nothing(platform):
    await platform.ask(CHANNEL, THREAD, Question("q9", "Which?", ("a",)), workspace=GUILD)

    outcome = await platform.questions.on_interaction(_click(_components(platform.rest)[0]["custom_id"], user=""))

    assert outcome == "refused:no-user" and platform.received == []
    assert _sent(platform.rest, "POST", f"/webhooks/{APP}/{SECRET}")[0]["content"] == NO_PERSON_NOTICE


@pytest.mark.parametrize("control_kind", ["question", "stop"])
async def test_controls_from_an_earlier_instance_cannot_answer_or_stop_after_restart(control_kind):
    async with _running_platform() as earlier:
        await earlier.ask(CHANNEL, THREAD, Question("q-restart", "Which?", ("a",)), workspace=GUILD)
        control = (_components(earlier.rest)[0] if control_kind == "question" else
                   earlier.questions.stop_button("Stop", message_id=MESSAGE))

    async with _running_platform() as restarted:
        # Reuse the question id and local key: only the instance nonce distinguishes the old control.
        await restarted.ask(CHANNEL, THREAD, Question("q-restart", "Which?", ("a",)), workspace=GUILD)
        outcome = await restarted.questions.on_interaction(_click(control["custom_id"]))

        assert outcome == "ignored:button-of-another-start"
        assert earlier.received == restarted.received == []
        assert _sent(restarted.rest, "POST", "/callback") == [{"type": DEFERRED_UPDATE}]
        notice = STOP_STALE_NOTICE if control_kind == "stop" else WITHDRAWN_QUESTION_NOTE
        (told,) = _sent(restarted.rest, "POST", f"/webhooks/{APP}/{SECRET}")
        assert (told["content"], told["flags"]) == (notice, EPHEMERAL)
        expected_edit = {"components": []}
        if control_kind == "question":
            expected_edit["embeds"] = [{"description": WITHDRAWN_QUESTION_NOTE}]
        assert _sent(restarted.rest, "PATCH", "/messages/@original") == [expected_edit]


@pytest.mark.parametrize("kind,conversation", [(11, CHANNEL), (12, THREAD)])
async def test_the_stop_button_hands_a_stop_for_its_message_and_tells_the_clicker(platform, kind, conversation):
    platform.rest.channels[THREAD]["type"] = kind
    platform.outcome = "stopped:test"
    button = platform.questions.stop_button("Stop", message_id=MESSAGE)

    outcome = await platform.questions.on_interaction(_click(button["custom_id"]))

    (stop,) = platform.received
    assert isinstance(stop, InboundStop) and outcome == "stopped:test"
    assert (stop.message_id, stop.conversation, stop.workspace, stop.sender) == (MESSAGE, conversation, GUILD, ALICE)
    assert _sent(platform.rest, "POST", f"/webhooks/{APP}/{SECRET}")[0]["content"] == STOP_ACCEPTED_NOTICE


async def test_a_click_discord_did_not_take_in_time_reaches_nobody(platform):
    await platform.ask(CHANNEL, THREAD, Question("q10", "Which?", ("a",)), workspace=GUILD)
    platform.rest.answer("POST", f"/interactions/{INTERACTION}/{SECRET}/callback", (404, {"code": 10062}))

    outcome = await platform.questions.on_interaction(_click(_components(platform.rest)[0]["custom_id"]))

    assert outcome == "dropped:not-acknowledged (not_found)" and platform.received == []


async def test_the_gateway_hands_a_click_to_its_own_task_and_names_no_token(platform, caplog):
    caplog.set_level(logging.INFO, logger="belle_agent.platforms.discord")
    await platform.ask(CHANNEL, THREAD, Question("q11", "Which?", ("a",)), workspace=GUILD)
    click = _click(_components(platform.rest)[0]["custom_id"])

    taken = await platform.handle({"op": 0, "s": 30, "t": "INTERACTION_CREATE", "d": click})
    other = await platform.handle({"op": 0, "s": 31, "t": "INTERACTION_CREATE", "d": {"type": 3, "data": {"custom_id": "x"}}})
    for _ in range(20):
        await asyncio.sleep(0)

    assert (taken, other) == ("taken:interaction", "ignored:interaction-type-3")
    assert len(platform.received) == 1 and SECRET not in caplog.text


def test_the_declared_affordances_contradict_no_member():
    platform = DiscordPlatform(DiscordSettings(token=TOKEN))
    assert platform.affordances.questions == "present"
    assert affordance_contradictions(platform) == []


async def test_a_click_from_another_guild_answers_nothing_and_leaves_the_question_waiting(platform):
    await platform.ask(CHANNEL, THREAD, Question("q12", "Which?", ("a", "b")), workspace=GUILD)
    first = _components(platform.rest)[0]["custom_id"]
    platform.outcome = "ignored:question-not-waiting"

    elsewhere = {**_click(first), "guild_id": "1000000000000000299"}
    await platform.questions.on_interaction(elsewhere)
    platform.outcome = "answered:test"
    again = await platform.questions.on_interaction(_click(first))

    assert platform.received[0].workspace == "1000000000000000299" and again == "answered:test"


async def test_capacity_closes_the_oldest_question_and_its_control_cannot_answer(platform):
    platform.questions._pending_questions_max = 1
    await platform.ask(CHANNEL, THREAD, Question("q13", "First?", ("yes",)), workspace=GUILD)
    old_control = _components(platform.rest)[0]["custom_id"]
    old_message = platform.rest.posted[-1][2]
    await platform.ask(CHANNEL, THREAD, Question("q14", "Second?", ("yes",)), workspace=GUILD)

    assert platform.rest.calls("PATCH", f"/channels/{THREAD}/messages/{old_message}")[-1].body == {"components": []}
    assert await platform.questions.on_interaction(_click(old_control)) == "ignored:question-not-waiting"
    assert platform.received == []


async def test_expiry_closes_the_question_and_failed_close_leaves_a_safe_stale_control(platform):
    platform.questions._question_expiry_seconds = 1
    await platform.ask(CHANNEL, THREAD, Question("q15", "Still there?", ("yes",)), workspace=GUILD)
    control = _components(platform.rest)[0]["custom_id"]
    message = platform.rest.posted[-1][2]
    path = f"/channels/{THREAD}/messages/{message}"
    platform.rest.answer("PATCH", path, (403, {"code": 50013, "message": "Missing Permissions"}))

    await asyncio.sleep(1.05)
    outcome = await platform.questions.on_interaction(_click(control))

    assert outcome == "ignored:question-not-waiting" and platform.received == []
    assert len(platform.rest.calls("PATCH", path)) == 1


async def test_connector_close_removes_question_controls_and_leaves_late_clicks_stale(platform):
    await platform.ask(CHANNEL, THREAD, Question("q-close", "Still there?", ("yes",)), workspace=GUILD)
    control = _components(platform.rest)[0]["custom_id"]
    message = platform.rest.posted[-1][2]

    await platform.questions.close()
    outcome = await platform.questions.on_interaction(_click(control))

    assert platform.rest.calls("PATCH", f"/channels/{THREAD}/messages/{message}")[-1].body == {"components": []}
    assert outcome == "ignored:question-not-waiting" and platform.received == []


async def test_connector_close_continues_after_an_unexpected_control_edit_failure(platform, monkeypatch):
    await platform.ask(CHANNEL, THREAD, Question("q-close-error", "Still there?", ("yes",)), workspace=GUILD)

    async def broken(*args, **kwargs):
        raise RuntimeError("transport broke")

    monkeypatch.setattr(platform.questions._client, "mapping", broken)
    await platform.questions.close()

    assert platform.questions._shown == {} and platform.questions._expiry_tasks == {}


async def test_a_refused_host_withdrawal_forgets_local_state_so_the_visible_control_is_stale(platform):
    await platform.ask(CHANNEL, THREAD, Question("q16", "Still valid?", ("yes",)), workspace=GUILD)
    control = _components(platform.rest)[0]["custom_id"]
    message = platform.rest.posted[-1][2]
    path = f"/channels/{THREAD}/messages/{message}"
    platform.rest.answer("PATCH", path, (403, {"code": 50013, "message": "Missing Permissions"}))

    await platform.withdraw_question("q16")
    outcome = await platform.questions.on_interaction(_click(control))

    assert outcome == "ignored:question-not-waiting" and platform.received == []


async def test_new_question_can_be_answered_while_old_controls_are_being_removed(platform, monkeypatch):
    platform.questions._pending_questions_max = 1
    await platform.ask(CHANNEL, THREAD, Question("q-old", "First?", ("yes",)), workspace=GUILD)
    old_message = platform.rest.posted[-1][2]
    entered, release = asyncio.Event(), asyncio.Event()
    mapping = platform._client.mapping

    async def hold_old_edit(method, path, **kwargs):
        if method == "PATCH" and path == f"/channels/{THREAD}/messages/{old_message}":
            entered.set()
            await release.wait()
        return await mapping(method, path, **kwargs)

    monkeypatch.setattr(platform._client, "mapping", hold_old_edit)
    asking = asyncio.create_task(platform.ask(
        CHANNEL, THREAD, Question("q-new", "Second?", ("yes",)), workspace=GUILD,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        control = _components(platform.rest)[0]["custom_id"]
        outcome = await platform.questions.on_interaction(_click(control))
    finally:
        release.set()
        await asking

    assert outcome == "answered:test"
    assert [answer.question_id for answer in platform.received] == ["q-new"]
    assert not platform.questions._shown and not platform.questions._expiry_tasks


async def test_cancelled_question_removes_new_controls_while_old_controls_are_being_removed(platform, monkeypatch):
    platform.questions._pending_questions_max = 1
    await platform.ask(CHANNEL, THREAD, Question("q-old", "First?", ("yes",)), workspace=GUILD)
    old_message = platform.rest.posted[-1][2]
    entered = asyncio.Event()
    mapping = platform._client.mapping

    async def hold_old_edit(method, path, **kwargs):
        if method == "PATCH" and path == f"/channels/{THREAD}/messages/{old_message}":
            entered.set()
            await asyncio.Event().wait()
        return await mapping(method, path, **kwargs)

    monkeypatch.setattr(platform._client, "mapping", hold_old_edit)
    asking = asyncio.create_task(platform.ask(
        CHANNEL, THREAD, Question("q-new", "Second?", ("yes",)), workspace=GUILD,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        new_message = platform.rest.posted[-1][2]
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking
    finally:
        asking.cancel()
        await asyncio.gather(asking, return_exceptions=True)

    assert not platform.questions._shown and not platform.questions._expiry_tasks
    assert platform.rest.calls("PATCH", f"/channels/{THREAD}/messages/{new_message}")[-1].body == {"components": []}
