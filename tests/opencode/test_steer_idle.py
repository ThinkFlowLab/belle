"""On OpenCode: a prompt steered into a session that runs nothing starts an execution of its own.

The OpenCode adapter waits for each message it steered into a turn. A message that arrives after the
turn's last model call is read only if OpenCode then runs the session once more for it.
"""

import asyncio

import pytest
from drive import Client

from belle_agent.integrations.opencode.sessions import message_id, session_id

pytestmark = pytest.mark.adapter


async def test_a_prompt_steered_into_an_idle_session_runs_at_once(world):
    client = Client()
    session = session_id("steer-idle", "c1")
    await asyncio.to_thread(client.api, "POST", "/api/session", {"id": session})
    prompt = {"id": message_id("steer-idle", "c1", "r1"), "text": "SAY=read", "delivery": "steer"}

    await asyncio.to_thread(client.api, "POST", f"/api/session/{session}/prompt", prompt)

    async with asyncio.timeout(60):
        while True:
            messages = await asyncio.to_thread(client.api, "GET", f"/api/session/{session}/message?limit=50")
            if any(message.get("type") == "idle" for message in messages or ()):
                break
            await asyncio.sleep(0.2)
    answered = [message for message in messages if message.get("type") == "assistant"]
    assert answered and "read" in "".join(part.get("text", "") for part in answered[0].get("content") or ())
    assert await asyncio.to_thread(client.api, "GET", f"/api/session/{session}/inbox") == []
