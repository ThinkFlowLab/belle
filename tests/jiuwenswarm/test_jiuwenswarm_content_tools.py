"""Native tools reuse content providers and the trusted Slack file gate."""

import json
from types import SimpleNamespace

import pytest

from belle_agent.integrations.jiuwenswarm import content_tools as native
from belle_agent.integrations.openjiuwen.tools.content import content_tools
from belle_agent.tools.content import ContentToolkit


async def test_native_function_keeps_provider_payload():
    calls = []

    async def request(**kwargs):
        calls.append(kwargs)
        return {"summary": "done"}

    tools = content_tools(ContentToolkit({"summarize": {}}, request=request))
    assert tools[0].card.name == "summarize"
    result = await tools[0].invoke({"text": "hello", "max_tokens": 20})
    assert json.loads(result) == {"summary": "done"}
    assert calls == [{"operation": "summarize", "payload": {"text": "hello", "max_tokens": 20}}]


async def test_native_plugin_registers_global_content_tools(monkeypatch):
    from belle_agent.integrations.jiuwenswarm import agent_runtime

    tools = (object(),)
    monkeypatch.setattr(native, "configured_content_tools", lambda context: tools)
    monkeypatch.setattr(agent_runtime, "rails", lambda agent: ())
    monkeypatch.setattr(agent_runtime, "activate", lambda *args: None)
    seen = []

    async def load(spec):
        seen.append(spec)
        return object()

    services = SimpleNamespace(
        agent=object(),
        runtime_context=object(),
        load_plugin_spec=load,
        register_request_context=None,
        register_runtime_tools=None,
    )
    assert await agent_runtime.mount_plugin(services) is not None
    assert seen[0].tools[0].params["instance"] is tools[0]


@pytest.mark.parametrize(
    "channel,metadata",
    [
        ("web", {"slack_channel_id": "C1", "slack_enforce_audience": True}),
        ("slack", {}),
        ("__cron__", {"slack_channel_id": "C1"}),
    ],
)
async def test_attachments_require_trusted_slack_context(monkeypatch, channel, metadata):
    monkeypatch.setattr(native, "_ContentFiles", lambda **kwargs: pytest.fail("must not reach file gate"))
    resolver = native._Attachments(SimpleNamespace(channel_id=channel, metadata=metadata))
    with pytest.raises(ValueError, match="trusted Slack"):
        await resolver.resolve_content_attachment("F1", {})


async def test_attachment_reads_current_context_through_native_file_gate(monkeypatch):
    from belle_agent.platforms.slack.history_policy import METADATA_ENFORCE_AUDIENCE_KEY

    context = SimpleNamespace(
        channel_id="slack", metadata={"slack_channel_id": "C1", METADATA_ENFORCE_AUDIENCE_KEY: True}
    )
    seen = []

    class Files:
        def __init__(self, metadata_provider, workspaces):
            seen.append(metadata_provider())
            self._scratch = SimpleNamespace(cleanup=lambda: seen.append("cleanup"))

        async def fetch_slack_file(self, ref):
            seen.append(ref)
            if ref == "denied":
                return {"error": "access_denied", "detail": "not visible"}, None
            return {"mimetype": "application/pdf"}, b"pdf bytes"

    monkeypatch.setattr(native, "_ContentFiles", Files)
    monkeypatch.setattr(native, "shared_slack_workspaces", lambda: object())
    resolver = native._Attachments(context)
    result = await resolver.resolve_content_attachment("F1", {"slack_channel_id": "untrusted"})
    assert result == {"document": "cGRmIGJ5dGVz", "document_mime_type": "application/pdf"}
    assert seen == [context.metadata, "F1", "cleanup"]
    context.metadata = {**context.metadata, "slack_channel_id": "C2"}
    with pytest.raises(ValueError, match="access_denied"):
        await resolver.resolve_content_attachment("denied", {})
    assert seen[-3:] == [context.metadata, "denied", "cleanup"]
