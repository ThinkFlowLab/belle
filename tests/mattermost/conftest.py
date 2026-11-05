"""The fixtures that several Mattermost test files share: a turn on the fake server, and a place on a test server."""

import secrets
from typing import Any

import pytest
from test_mattermost_server import ADMIN_TOKEN, URL, Place, Rest, _channel
from test_mattermost_tools import ALICE, BOB, CAROL, ELSEWHERE, LOUNGE, OTHER_TEAM, OUTSIDE, PAIR, QUIET, TEAM, Turn
from transport import BOT_USER, FakeRest

from belle_agent.platforms.base import InboundMessage
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.settings import MattermostSettings


@pytest.fixture
async def turn():
    rest = FakeRest()
    rest.channels.update({
        LOUNGE: {"id": LOUNGE, "type": "O", "team_id": TEAM, "name": "lounge", "display_name": "Lounge"},
        QUIET: {"id": QUIET, "type": "P", "team_id": TEAM, "name": "quiet-room", "display_name": "Quiet Room"},
        PAIR: {"id": PAIR, "type": "P", "team_id": TEAM, "name": "pair", "display_name": "Pair"},
        OUTSIDE: {"id": OUTSIDE, "type": "O", "team_id": TEAM, "name": "outside", "display_name": "Outside"},
        ELSEWHERE: {"id": ELSEWHERE, "type": "O", "team_id": OTHER_TEAM, "name": "elsewhere", "display_name": "Elsewhere"},
    })
    rest.members.update({QUIET: [ALICE, BOB, BOT_USER], PAIR: [ALICE, BOT_USER], LOUNGE: [ALICE, BOB, CAROL, BOT_USER]})
    rest.users.update({user: {"id": user, "username": name} for user, name in ((ALICE, "alice"), (BOB, "bob"), (CAROL, "carol"))})
    rest.teams.update({TEAM: [ALICE, BOB, CAROL, BOT_USER], OTHER_TEAM: [ALICE, BOT_USER]})
    running = Turn(rest)
    await running.connector.start(lambda record: None)
    yield running
    await running.connector.stop()


@pytest.fixture
async def place():
    admin = Rest(ADMIN_TOKEN)
    tag = secrets.token_hex(4)
    team = await admin.call("POST", "/teams", json={"name": f"belle-test-{tag}", "display_name": f"Belle test {tag}", "type": "O"})
    bot = await admin.call("POST", "/bots", json={"username": f"belle-{tag}", "display_name": "Belle test"})
    bot_token = (await admin.call("POST", f"/users/{bot['user_id']}/tokens", json={"description": "belle test"}))["token"]
    made = Place(admin, tag, team["id"], bot, bot_token)
    try:
        await admin.call("POST", f"/teams/{made.team}/members", json={"team_id": made.team, "user_id": bot["user_id"]})
        for name in ("alice", "bob"):
            user = await admin.call(
                "POST", "/users",
                json={"username": f"{name}-{tag}", "email": f"{name}-{tag}@belle.invalid", "password": secrets.token_hex(16)},
            )
            token = (await admin.call("POST", f"/users/{user['id']}/tokens", json={"description": "belle test"}))["token"]
            made.users[name] = (user, Rest(token))
            await admin.call("POST", f"/teams/{made.team}/members", json={"team_id": made.team, "user_id": user["id"]})
        members = [bot["user_id"], made.id("alice"), made.id("bob")]
        made.channels["lounge"] = await _channel(admin, made.team, f"lounge-{tag}", members)
        made.channels["chatter"] = await _channel(admin, made.team, f"chatter-{tag}", members)
        # A private channel of the bot and alice, and an open channel the bot is not in.
        made.channels["quiet"] = await _channel(admin, made.team, f"quiet-{tag}", members[:2], kind="P")
        made.channels["outside"] = await _channel(admin, made.team, f"outside-{tag}", members[1:])
        yield made
    finally:
        for user, rest in made.users.values():
            await rest.http.aclose()
            await admin.call("DELETE", f"/users/{user['id']}")
        await admin.call("POST", f"/bots/{bot['user_id']}/disable")
        await admin.call("DELETE", f"/teams/{made.team}")
        await admin.http.aclose()


@pytest.fixture
async def tools(place):
    """The connector's toolkit on the bot, and a function that sets the turn its calls belong to."""
    connector = MattermostPlatform(MattermostSettings(URL, place.bot_token))

    async def ignore(_record):
        return "ignored"

    await connector.start(ignore)
    metadata: dict[str, Any] = {}
    (toolkit,) = connector.toolkits(lambda: metadata)

    def turn(channel: str, sender: str, kind: str = "O") -> None:
        inbound = InboundMessage("mattermost", place.team, channel, "", sender, "", chat_type=kind)
        metadata.clear()
        metadata.update(connector.request_metadata(inbound))

    yield toolkit, turn
    await connector.stop()
