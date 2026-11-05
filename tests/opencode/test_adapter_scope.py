"""Per-turn scope proven in captured provider requests, on OpenCode with and without the relay plugin.

The tool roster, the attached skill text and the system additions are read from
the stand-in's request log for the turn they belong to.
"""

import asyncio
import re
import secrets

import pytest
from standin import call, said, tools

from belle_agent.backend import TurnRequest, TurnScope
from belle_agent.capability_catalog import scope_allows

pytestmark = pytest.mark.adapter

POST = "belle_post_message"
# A phrase from group-chat-participation/SKILL.md, present only when the skill is attached.
SKILL_PHRASE = "worth interrupting for"

REQUIRE_SKILL = """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all], session: channel}
    agent: {skills: {required: [group-chat-participation]}}
  - match: {channel: loopback, chat: C2}
    delivery: {mode: [all]}
"""

# Tool permissions cannot be scoped per user (the schema forbids it), so a
# per-requester roster is shown through the skills a scope attaches by user.
TWO_REQUESTERS = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all], session: channel}
  - match: {channel: loopback, user: [U-BOB]}
    agent: {skills: {required: [group-chat-participation]}}
"""


def _session(model, text: str) -> str:
    # A required skill's content comes first in the user message, and the prompt ends it.
    return next(entry["session"] for entry in reversed(model.log) if "".join(said(entry["body"], "user")[-1:]).endswith(text))


async def test_a_required_skill_reaches_only_its_turn(build_world, model):
    world = await build_world(REQUIRE_SKILL)

    await world.send(world.message("SAY=with", conversation="C1"))
    await world.send(world.message("SAY=without", conversation="C2"))

    with_skill = model.requests(_session(model, "SAY=with"))[0]
    without = model.requests(_session(model, "SAY=without"))[0]
    assert SKILL_PHRASE in "".join(said(with_skill, "user") + said(with_skill, "system"))
    assert SKILL_PHRASE not in "".join(said(without, "user") + said(without, "system"))


async def test_two_requesters_in_one_conversation_get_two_rosters(build_world, model):
    world = await build_world(TWO_REQUESTERS)

    await world.send(world.message("SAY=plain", conversation="C1", sender="U1"))
    await world.send(world.message("SAY=skilled", conversation="C1", sender="U-BOB"))
    await world.send(world.message("SAY=plain2", conversation="C1", sender="U1"))

    # The three turns share one session (delivery.session: channel); resolve it from the plain turn,
    # whose prompt the skill does not alter.
    session = _session(model, "SAY=plain")
    requests = model.requests(session)
    assert len(requests) == 3
    # The skill is attached to its turn's own prompt (the last user message); an attached skill lingers
    # in the history, so the per-turn attachment is read from the turn's own message, not the whole request.
    attached = [SKILL_PHRASE in said(request, "user")[-1] for request in requests]
    # Only the second requester's turn attaches the skill; the first requester's roster returns unchanged.
    assert attached == [False, True, False]


UNAVAILABLE_SKILL = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
  - match: {channel: loopback, chat: C1}
    agent: {skills: {unavailable: [chat-digest]}}
"""
# A line of chat-digest/SKILL.md, present in a tool result only when the skill loaded.
DIGEST_PHRASE = "A digest states what changed."


async def test_an_unavailable_skill_is_not_listed_and_does_not_load_where_the_scope_names_it(build_world, model):
    world = await build_world(UNAVAILABLE_SKILL)
    texts = {chat: f"{call('skill', id='chat-digest')} in {chat}" for chat in ("C1", "C2")}

    for chat, text in texts.items():
        await world.send(world.message(text, conversation=chat))

    requests = {chat: model.requests(_session(model, text)) for chat, text in texts.items()}
    listed = {chat: "chat-digest" in "".join(said(asked[0], "system")) for chat, asked in requests.items()}
    results = {chat: "".join(said(asked[-1], "tool")) for chat, asked in requests.items()}
    assert not listed["C1"] and "permission.rejected" in results["C1"] and DIGEST_PHRASE not in results["C1"]
    # Control: where no scope makes it unavailable, the skill is listed and loads.
    assert listed["C2"] and DIGEST_PHRASE in results["C2"]


AVAILABLE = frozenset({"chat-digest", "reading-digest", "group-chat-participation"})
AVAILABLE_SKILLS = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
  - match: {channel: loopback, chat: C1}
    agent:
      skills:
        available: [chat-digest, reading-digest, group-chat-participation]
        unavailable: [reading-digest]
        required: [group-chat-participation]
"""


def _listed(body: dict) -> set[str]:
    """The ids of OpenCode's list of skills in the system message."""
    return set(re.findall(r"<id>([^<]+)</id>", "".join(said(body, "system"))))


async def test_only_the_skills_a_scope_allows_are_listed_and_load(build_world, model):
    world = await build_world(AVAILABLE_SKILLS)
    catalog = [skill["name"] for skill in (await world.host.catalog()).skills]
    texts = {chat: f"{call('skill', id='pr-tracker')} in {chat}" for chat in ("C1", "C2")}

    for chat, text in texts.items():
        await world.send(world.message(text, conversation=chat))

    requests = {chat: model.requests(_session(model, text)) for chat, text in texts.items()}
    results = {chat: "".join(said(asked[-1], "tool")) for chat, asked in requests.items()}
    allowed = {name for name in catalog if scope_allows(name, AVAILABLE, {"reading-digest"})}
    assert allowed == {"chat-digest", "group-chat-participation"}
    assert _listed(requests["C1"][0]) == allowed
    assert "permission.rejected" in results["C1"] and "<skill_content" not in results["C1"]
    # A required skill is attached under the same scope.
    assert SKILL_PHRASE in said(requests["C1"][0], "user")[-1]
    # Control: where no scope restricts, every skill is listed and pr-tracker loads. OpenCode's browser plugin
    # denies its skill to every agent until a desktop app attaches a browser to the session.
    assert _listed(requests["C2"][0]) == set(catalog) - {"browser"}
    assert "<skill_content" in results["C2"]


async def test_one_session_three_turns_the_second_restricted(world, model):
    backend = world.kit.backend
    restricted = {"shell": "deny", "webfetch": "deny", "post_message": "deny"}

    for number, levels in enumerate(({}, restricted, {})):
        request = TurnRequest(conversation="c1", request_id=f"r{number}", text=f"turn{number} SAY=ok", scope=TurnScope(tools=levels))
        async for _ in backend.run_turn(request):
            pass

    full, narrowed, again = (set(tools(request)) for request in model.requests(_session(model, "turn2 SAY=ok")))
    # The roster of each request is its own turn's scope: a built-in tool and one of Belle's leave and come back.
    assert {"shell", "webfetch", POST} <= full
    assert narrowed == full - {"shell", "webfetch", POST}
    assert again == full


RESTRICTED_ROSTER = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all], reply: optional}
    permissions: {tools: {shell: deny, edit: deny, write: deny, execute: deny, webfetch: deny, websearch: deny}}
"""


@pytest.mark.provider
async def test_a_real_model_stays_inside_a_restricted_roster(build_world):
    """A real model given a restricted roster answers through the posting tool, and the denied tools are gone."""
    world = await build_world(RESTRICTED_ROSTER)
    token = "ROSTER_" + secrets.token_hex(4)

    async with asyncio.timeout(240):
        await world.send(world.direct(
            f"Reply by calling the {POST} tool once with the text {token}. "
            "The shell, edit, write and other code tools are not available to you."
        ))

    # A provider run uses the real model, so the stand-in log is empty; the roster is proven behaviourally:
    # the model answered through the posting tool and did not need a denied tool (the recording proxy reads the request).
    assert [post.conversation for post in world.posts()] == ["D-U1"]
    assert token in world.posts()[0].text
