"""Questions as buttons, and what a click on one becomes: only a click of this start, by a person, reaches the host."""

import httpx
import pytest
from transport import TOKEN, URL, EventFeeder, FakeRest, hello

from belle_agent.backend import APPROVAL, Question
from belle_agent.click_notices import ANSWER_REFUSED_NOTICE, STOP_STALE_NOTICE, WITHDRAWN_QUESTION_NOTE
from belle_agent.platforms.base import InboundAnswer, InboundStop, QuestionRefused
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.questions import NO_OPTION_NOTICE, NO_PERSON_NOTICE
from belle_agent.platforms.mattermost.settings import MattermostSettings

CHANNEL = "c" * 26
TEAM = "t" * 26
ALICE = "a" * 26
APPROVE_IT = Question(id="r1:held-1", text="Post this in **town-square**?", options=("approve", "reject"), kind=APPROVAL)


class Host:
    """Stands in for the host: keeps each record, and answers with the outcome a check sets."""

    def __init__(self) -> None:
        self.records: list = []
        self.outcome = "answered session=s1"

    async def __call__(self, record):
        self.records.append(record)
        return self.outcome


@pytest.fixture
async def connected():
    rest = FakeRest()
    client = MattermostClient(URL, TOKEN, transport=rest.transport(), open_events=EventFeeder(hello()).open)
    settings = MattermostSettings(URL, TOKEN, actions_url="http://belle.invalid:18400", actions_port=0)
    connector = MattermostPlatform(settings, client=client)
    host = Host()
    await connector.start(host)
    yield connector, rest, host
    await connector.stop()


def _buttons(rest: FakeRest, post_id: str) -> list[dict]:
    return rest.posts[post_id]["props"]["attachments"][0]["actions"]


def _click(rest: FakeRest, post_id: str, index: int, *, user: str = ALICE, **context) -> dict:
    """The request the server sends when a person clicks the button ``index`` of a post."""
    button = _buttons(rest, post_id)[index]
    return {"user_id": user, "user_name": "alice", "post_id": post_id, "channel_id": CHANNEL, "team_id": TEAM,
            "context": {**button["integration"]["context"], **context}}


async def test_an_approval_is_a_post_with_a_button_per_option_that_names_its_record_and_option(connected):
    connector, rest, _ = connected

    post_id = await connector.ask(CHANNEL, "root", APPROVE_IT, workspace=TEAM)

    post = rest.posts[post_id]
    assert (post["message"], post["root_id"]) == (APPROVE_IT.text, "root")
    buttons = _buttons(rest, post_id)
    assert [(button["name"], button["style"]) for button in buttons] == [("Approve", "primary"), ("Reject", "danger")]
    assert buttons[0]["integration"]["url"] == "http://belle.invalid:18400/action"
    context = buttons[1]["integration"]["context"]
    assert (context["kind"], context["record"], context["option"]) == ("answer", APPROVE_IT.id, 1)


async def test_a_question_with_no_server_post_id_is_not_registered(connected):
    connector, rest, _ = connected
    rest.routes[("POST", "/posts")] = (201, {})

    with pytest.raises(MattermostError, match="bad_response"):
        await connector.ask(CHANNEL, "root", APPROVE_IT, workspace=TEAM)

    assert connector.questions._shown == {}


async def test_a_click_answers_the_host_and_closes_the_question_with_who_answered(connected):
    connector, rest, host = connected
    post_id = await connector.ask(CHANNEL, "root", APPROVE_IT, workspace=TEAM)

    reply = await connector.questions.on_click(_click(rest, post_id, 0))

    (answer,) = host.records
    assert isinstance(answer, InboundAnswer)
    assert (answer.workspace, answer.conversation, answer.question_id, answer.sender) == (TEAM, CHANNEL, APPROVE_IT.id, ALICE)
    assert answer.answer.approved is True and answer.answer.values == ("approve",)
    assert reply == {"update": {"message": APPROVE_IT.text,
                                "props": {"attachments": [{"text": "Answered by @alice: Approve"}]}}}


async def test_a_refused_answer_is_told_to_the_clicker_alone_and_the_question_stays_open(connected):
    connector, rest, host = connected
    post_id = await connector.ask(CHANNEL, "", APPROVE_IT, workspace=TEAM)
    host.outcome = "refused:clicks.approve=user:U-LEAD"

    assert await connector.questions.on_click(_click(rest, post_id, 0)) == {"ephemeral_text": ANSWER_REFUSED_NOTICE}
    host.outcome = "answered session=s1"
    assert "update" in await connector.questions.on_click(_click(rest, post_id, 1))


async def test_a_question_the_host_no_longer_holds_loses_its_buttons(connected):
    connector, rest, host = connected
    post_id = await connector.ask(CHANNEL, "", APPROVE_IT, workspace=TEAM)
    host.outcome = "ignored:question-not-waiting"

    reply = await connector.questions.on_click(_click(rest, post_id, 0))

    assert reply["ephemeral_text"] == WITHDRAWN_QUESTION_NOTE
    assert reply["update"]["props"] == {"attachments": [{"text": WITHDRAWN_QUESTION_NOTE}]}


@pytest.mark.parametrize(
    ("change", "notice"),
    [
        ({"context": {"kind": "answer", "record": APPROVE_IT.id, "option": 0, "secret": "of-an-earlier-start"}},
         WITHDRAWN_QUESTION_NOTE),
        ({"context": {"kind": "stop", "message": "msg1", "secret": "of-an-earlier-start"}}, STOP_STALE_NOTICE),
        ({"context": {"kind": "answer", "record": APPROVE_IT.id, "option": 0}}, WITHDRAWN_QUESTION_NOTE),
        ({"record": "a-question-never-shown"}, WITHDRAWN_QUESTION_NOTE),
        ({"option": 2}, NO_OPTION_NOTICE),
        ({"option": True}, NO_OPTION_NOTICE),
        ({"user_id": ""}, NO_PERSON_NOTICE),
    ],
)
async def test_a_click_that_names_nothing_waiting_or_nobody_never_reaches_the_host(connected, change, notice):
    connector, rest, host = connected
    post_id = await connector.ask(CHANNEL, "", APPROVE_IT, workspace=TEAM)
    request = _click(rest, post_id, 0)
    for key in ("context", "user_id"):
        if key in change:
            request[key] = change[key]
    request["context"].update({key: value for key, value in change.items() if key not in ("context", "user_id")})

    reply = await connector.questions.on_click(request)

    assert reply["ephemeral_text"] == notice and host.records == []


async def test_a_stop_button_hands_the_host_a_stop_for_its_message(connected):
    connector, _, host = connected
    button = connector.questions.stop_button("Stop", workspace=TEAM, message_id="msg1")
    host.outcome = "stopped session=s1"

    reply = await connector.questions.on_click(
        {"user_id": ALICE, "post_id": "card", "channel_id": CHANNEL, "context": button["integration"]["context"]}
    )

    assert host.records == [InboundStop("mattermost", TEAM, CHANNEL, "msg1", ALICE)]
    assert reply == {"ephemeral_text": "Stopping this turn."}


async def test_the_server_reaches_the_click_through_the_listener(connected):
    connector, rest, host = connected
    post_id = await connector.ask(CHANNEL, "", APPROVE_IT, workspace=TEAM)

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{connector.actions.port}") as server:
        response = await server.post("/action", json=_click(rest, post_id, 1))

    assert response.status_code == 200 and response.json()["update"]["message"] == APPROVE_IT.text
    assert host.records[0].answer.approved is False


async def test_with_no_listener_a_question_cannot_be_asked():
    rest = FakeRest()
    client = MattermostClient(URL, TOKEN, transport=rest.transport(), open_events=EventFeeder(hello()).open)
    connector = MattermostPlatform(MattermostSettings(URL, TOKEN), client=client)

    with pytest.raises(NotImplementedError, match="actions.url"):
        await connector.ask(CHANNEL, "", APPROVE_IT, workspace=TEAM)
    assert rest.requests == []


async def test_a_question_with_no_option_is_refused(connected):
    connector, _, _ = connected

    with pytest.raises(QuestionRefused, match="no options"):
        await connector.ask(CHANNEL, "", Question(id="q", text="Which file?"), workspace=TEAM)
