"""One backend turn through the fake Discord connector."""

from dataclasses import replace

from conformance_kit import worlds
from standin import call

from belle_agent.backend import Answer
from belle_agent.host import OUTCOME_POSTED_BY_TOOL
from belle_agent.platforms.base import TRIGGER_DM, InboundAnswer, InboundMessage
from belle_agent.platforms.discord.settings import TOKEN_ENV


def _message(conversation: str, message_id: str) -> InboundMessage:
    return InboundMessage(
        platform="discord", workspace="smoke:direct", conversation=conversation, message_id=message_id,
        event_id=str(int(message_id) + 1), sender="42345678901234567", text="run the Discord tools",
        chat_type="dm", trigger=TRIGGER_DM,
    )


async def post_through_discord(kit, directory, discord):
    """Check named-bot metadata and mounted Discord read and post tools through a backend."""
    directory.mkdir(parents=True, exist_ok=True)
    channel, message, sender = "22345678901234567", "32345678901234567", "42345678901234567"
    discord.rest.answer("GET", f"/channels/{channel}/messages/{message}", (200, {
        "id": message, "channel_id": channel, "author": {"id": sender},
        "content": "read through the backend", "timestamp": "2026-10-21T00:00:00Z",
    }))
    read_tool = kit.backend.capabilities().tool_name("read_discord_conversation")
    post_tool = kit.backend.capabilities().tool_name("post_message")

    async with worlds(kit, directory, discord) as build:
        world = await build(
            "scopes:\n"
            "  - match: {channel: discord}\n"
            "    delivery: {mode: [all]}\n"
            f"platforms: {{discord: {{reply_in_thread: false, bots: [{{name: smoke, token_env: {TOKEN_ENV}}}]}}}}\n"
        )
        inbound = replace(_message(channel, message), text=" ".join((
            call(read_tool, message_id=message),
            call(post_tool, text="posted by the backend"),
        )))
        await world.send(inbound)

        assert world.requests[0].metadata["discord_channel_id"] == channel
        assert world.requests[0].metadata["discord_guild_id"] == ""
        assert world.requests[0].metadata["discord_user_id"] == sender
        assert world.requests[0].metadata["discord_bot"] == "smoke"
        assert discord.rest.calls("GET", f"/channels/{channel}/messages/{message}")
        assert [(post.conversation, post.text) for post in world.posts()] == [(channel, "posted by the backend")]
        assert world.outcomes() == [OUTCOME_POSTED_BY_TOOL]


async def approval_through_discord(kit, directory, discord, *, approved: bool) -> None:
    """Check that backend tool calls wait for a Discord approval and honor its answer."""
    directory.mkdir(parents=True, exist_ok=True)
    channel, message = "22345678901234568", "32345678901234568"
    post_tool = kit.backend.capabilities().tool_name("post_message")
    async with worlds(kit, directory, discord) as build:
        world = await build(
            "scopes:\n"
            "  - match: {channel: discord}\n"
            "    delivery: {mode: [all]}\n"
            "    permissions: {tools: {post_message: ask}}\n"
            f"platforms: {{discord: {{reply_in_thread: false, bots: [{{name: smoke, token_env: {TOKEN_ENV}}}]}}}}\n"
        )
        inbound = replace(_message(channel, message), text=call(post_tool, text="approval gated"))
        await world.platform.inject(inbound)
        (shown,) = await world.wait_question()
        assert shown.question.tool.endswith("post_message")
        assert world.posts() == []

        answer = InboundAnswer(
            platform="discord", workspace="smoke:direct", conversation=channel,
            question_id=shown.question.id, sender="42345678901234567", answer=Answer(approved=approved),
        )
        await world.send(answer)

        posts = [post for post in world.posts() if post.text == "approval gated"]
        assert bool(posts) is approved
        if approved:
            assert world.outcomes() == [OUTCOME_POSTED_BY_TOOL]
        else:
            assert OUTCOME_POSTED_BY_TOOL not in world.outcomes()
