"""Hosted approval checks for Discord's audience-widening post tool."""

import httpx
import pytest
from conformance_kit import ModelTurn

from belle_agent.host import CONFIRMATION_TIMEOUT_SECONDS

pytestmark = [
    pytest.mark.parametrize("kit", ["scripted"], indirect=True),
    pytest.mark.parametrize("platform_kit", ["discord"], indirect=True),
]

RECIPIENT = "1000000000000000701"
DM_CHANNEL = "1000000000000000500"
POST = ("post_message", {"text": "private detail", "recipient_user_id": RECIPIENT})
ANSWER_EVERYTHING = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
"""


async def _asked(build_world, calls=(POST,)):
    world = await build_world(ANSWER_EVERYTHING)
    world.platform_kit.rest.answer(
        "POST",
        "/users/@me/channels",
        (200, {"id": DM_CHANNEL, "type": 1, "recipients": [{"id": RECIPIENT}]}),
    )
    world.kit.play(ModelTurn(final="I asked first.", calls=calls))
    await world.send(world.message("send this privately"))
    (shown,) = world.questions()
    return world, shown


async def test_approved_post_waits_for_consent_and_runs_once(build_world):
    world, shown = await _asked(build_world)

    assert shown.question.kind == "approval"
    assert shown.question.tool == "post_message"
    assert world.platform_kit.rest.calls("POST", f"/channels/{DM_CHANNEL}/messages") == []

    assert (await world.send(world.answer_to(shown, approved=True))).startswith("answered")
    assert len(world.platform_kit.rest.calls("POST", f"/channels/{DM_CHANNEL}/messages")) == 1
    assert await world.send(world.answer_to(shown, approved=True)) == "ignored:question-not-waiting"
    assert len(world.platform_kit.rest.calls("POST", f"/channels/{DM_CHANNEL}/messages")) == 1


async def test_refused_post_never_runs(build_world):
    world, shown = await _asked(build_world)

    assert (await world.send(world.answer_to(shown, approved=False))).startswith("answered")
    assert world.platform_kit.rest.calls("POST", f"/channels/{DM_CHANNEL}/messages") == []


async def test_expired_approval_does_not_post(build_world):
    world, shown = await _asked(build_world)
    now = world.host.clock
    world.host.clock = lambda: now() + CONFIRMATION_TIMEOUT_SECONDS + 1

    assert await world.send(world.answer_to(shown, approved=True)) == "ignored:question-not-waiting"
    assert world.platform_kit.rest.calls("POST", f"/channels/{DM_CHANNEL}/messages") == []


async def test_identical_widening_calls_share_one_approval_and_post(build_world):
    world, shown = await _asked(build_world, calls=(POST, POST))

    assert len(world.questions()) == 1
    assert (await world.send(world.answer_to(shown, approved=True))).startswith("answered")
    assert len(world.platform_kit.rest.calls("POST", f"/channels/{DM_CHANNEL}/messages")) == 1


async def test_approved_uncertain_post_warns_without_claiming_nothing_was_sent(build_world):
    world, shown = await _asked(build_world)
    rest = world.platform_kit.rest
    original = rest._answer
    accepted = []

    def answer(request):
        response = original(request)
        if request.method == "POST" and request.url.path == f"/api/v10/channels/{DM_CHANNEL}/messages":
            accepted.append(response.json()["id"])
            return httpx.Response(200, content=b"not JSON")
        return response

    platform = world.host.platforms["discord"]
    platform._client._http._transport = httpx.MockTransport(answer)

    assert (await world.send(world.answer_to(shown, approved=True))).startswith("answered")
    notices = [entry.body.get("content", "") for entry in rest.requests
               if entry.method == "POST" and "/messages" in entry.path and isinstance(entry.body, dict)]
    assert len(accepted) == 1
    assert "delivery_uncertain_do_not_retry" in notices[-1]
    assert "Delivery is uncertain" in notices[-1]
    assert "Do not retry" in notices[-1]
    assert "Nothing was sent" not in notices[-1]
    assert await world.send(world.answer_to(shown, approved=True)) == "ignored:question-not-waiting"
    assert len(accepted) == 1
    await world.send(world.message("and?", conversation=shown.conversation, thread=shown.thread))
    told = "\n".join(world.requests[-1].scope.system_additions)
    assert "delivery_uncertain_do_not_retry" in told
