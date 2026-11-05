"""One question to the real model: does it see an image that ``download_slack_file`` fetched.

    python -m pytest -q -s tests/opencode/test_provider_tool_content.py

Run it beside OpenCode on the real provider, behind the recording proxy
(OCB_PROXY). OCB_MAX_REQUESTS=4 on the proxy caps the cost.

Slack is the fake transport and holds one image: a white 7 on red. The
recording proxy answers the turn's first request with the tool call, so the
model is asked once, with the tool's result in hand. No default pass runs
this: it needs the provider.
"""

import pytest
from conformance_kit import worlds
from opencode_kit import OpenCodeKit
from probe_tool_content import png
from slack_kit import SlackKit
from test_provider_validation import DENY_YAML, exchanges, note, script

pytestmark = [pytest.mark.adapter, pytest.mark.provider, pytest.mark.validation]

SCOPES = f"""
scopes:
  - match: {{channel: loopback}}
    delivery: {{mode: [all]}}
    permissions: {{tools: {{{DENY_YAML}}}}}
"""


async def test_the_model_describes_an_image_the_slack_file_tool_fetched(tmp_path, monkeypatch):
    slack = SlackKit(monkeypatch)
    async with worlds(OpenCodeKit(), tmp_path, slack) as build:
        world = await build(SCOPES)
        tool, arguments = slack.holds(world.platform, "seven.png", "image/png", png())
        script({"calls": [[world.kit.backend.capabilities().tool_name(tool), arguments]]})

        await world.send(world.message(
            "Read the file I shared with download_slack_file. Then answer in one line: which digit does the picture"
            " show, and what colour is its background? If you were given no picture, answer exactly NOTHING RECEIVED.",
            conversation="C1",
        ))

        answer = world.posts("C1")[-1].text
        sent = exchanges()[-1]["body"]["messages"]
        images = [part for message in sent if isinstance(message.get("content"), list) for part in message["content"] if part.get("type") == "image_url"]
        note("tool-content image via download_slack_file", outcome=world.outcomes(), images_in_the_request=len(images), model_said=answer[:300])
        assert len(images) == 1
        assert "7" in answer and "red" in answer.lower()
