"""Discord rich message validation before the REST boundary."""

import base64
import json

import pytest

from belle_agent.platforms.discord.rich_post import (
    COMPONENTS_V2,
    RichPostError,
    compose_rich_post,
    rich_tool_properties,
)

ID = "1000000000000000116"


@pytest.mark.parametrize("kwargs, field", [
    ({}, "text"),
    ({"text": ""}, "text"),
    ({"text": "x" + " " * 2000}, "text"),
    ({"embeds": [{"title": "a" * 257}]}, "embeds[0].title"),
    ({"embeds": [{"description": "a" * 4097}]}, "embeds[0].description"),
    ({"embeds": [{"fields": [{"name": "n", "value": "v"}] * 26}]}, "embeds[0].fields"),
    ({"embeds": [{"description": "x" * 4000}, {"description": "y" * 2001}]}, "embeds"),
    ({"sticker_ids": [ID] * 4}, "sticker_ids"),
    ({"poll": {"question": {"text": "Which?"}, "answers": []}}, "poll.answers"),
    ({"poll": {"question": {"text": "x"}, "answers": [{"poll_media": {"text": "a"}}], "duration": 769}}, "poll.duration"),
    ({"mentions": {"roles": [ID]} , "text": "ping"}, "mentions.roles"),
    ({"mentions": {"everyone": True}, "text": "@everyone"}, "mentions.everyone"),
    ({"components": [{"type": 1, "components": [{"type": 2, "custom_id": "x", "style": 1}]}]}, "components[0].components[0].custom_id"),
    ({"components": [{"type": 15}]}, "components[0].type"),
    ({"components": [{"type": 18}]}, "components[0].type"),
    ({"text": "hello", "components": [{"type": 10, "content": "there"}]}, "components"),
    ({"attachments": [{"filename": "../../secret", "content": "x"}]}, "attachments[0].filename"),
    ({"attachments": [{"filename": "x.txt", "file_id": ID}]}, "attachments[0].file_id"),
    ({"tts": True, "text": "hello"}, "tts"),
])
def test_invalid_rich_message_names_field_before_send(kwargs, field):
    with pytest.raises(RichPostError) as exc:
        compose_rich_post(**kwargs)
    assert exc.value.field == field


def test_embed_only_and_theme_only_messages_have_no_text():
    assert compose_rich_post(embeds=[{"title": "Hello"}]).payload["embeds"] == [{"title": "Hello"}]
    assert "content" not in compose_rich_post(shared_client_theme={
        "colors": ["5865F2"], "gradient_angle": 0, "base_mix": 58,
    }).payload


def test_allowed_mentions_and_flags_are_independent():
    post = compose_rich_post(text=f"<@{ID}> hi", mentions={"users": [ID]},
                             suppress_embeds=True, suppress_notifications=True)
    assert post.payload["allowed_mentions"] == {"parse": [], "users": [ID]}
    assert post.payload["flags"] == (1 << 2) | (1 << 12)
    assert "tts" not in post.payload


def test_noninteractive_v2_component_and_registered_button():
    post = compose_rich_post(components=[{"type": 10, "content": "Hello"}])
    assert post.payload["flags"] == COMPONENTS_V2
    assert post.payload["components"][0]["content"] == "Hello"
    button = {"type": 1, "components": [{"type": 2, "style": 1, "custom_id": "approved"}]}
    assert compose_rich_post(components=[button], handlers={"approved"}).payload["components"] == [button]
    assert compose_rich_post(components=[{"type": 17, "components": [{"type": 10, "content": "Hi"}]}]).payload["flags"] == COMPONENTS_V2


def test_inline_file_and_caption_share_multipart_message():
    post = compose_rich_post(text="caption", attachments=[{
        "filename": "report.txt", "content_base64": base64.b64encode(b"hello\x00").decode(),
        "title": "Final report", "description": "The report", "is_spoiler": True,
    }])
    request = post.request_kwargs()
    assert json.loads(request["data"]["payload_json"])["attachments"] == [
        {"id": 0, "filename": "report.txt", "title": "Final report", "description": "The report", "is_spoiler": True},
    ]
    assert request["files"] == [("files[0]", ("report.txt", b"hello\x00"))]


def test_poll_and_sticker_payloads_match_discord():
    poll = {"question": {"text": "Choose"}, "answers": [
        {"poll_media": {"text": "A"}}, {"poll_media": {"text": "B"}},
    ], "duration": 24}
    assert compose_rich_post(poll=poll, sticker_ids=[ID]).payload["poll"] == poll


def test_schema_exposes_only_sources_that_can_send():
    properties = rich_tool_properties()
    assert "file_id" not in properties["attachments"]["items"]["properties"]
    assert properties["embeds"]["maxItems"] == 10
    assert properties["poll"]["properties"]["duration"]["maximum"] == 768


@pytest.mark.parametrize("url", ["https://example.com/image.png", "attachment://image.png"])
def test_media_gallery_preserves_media_records_and_multipart_uploads(url):
    gallery = {"type": 12, "items": [{
        "media": {"url": url}, "description": "Preview", "spoiler": True,
    }]}
    attachments = [{"filename": "image.png", "content_base64": "aGVsbG8="}] if url.startswith("attachment:") else None
    post = compose_rich_post(components=[gallery], attachments=attachments)
    assert post.payload["components"] == [gallery]
    assert post.payload["flags"] == COMPONENTS_V2
    request = post.request_kwargs()
    if attachments:
        assert json.loads(request["data"]["payload_json"])["components"] == [gallery]
        assert request["files"] == [("files[0]", ("image.png", b"hello"))]
    else:
        assert request == {"json": post.payload}


@pytest.mark.parametrize("items, field", [
    (None, "components[0].items"),
    ([], "components[0].items"),
    ([{"media": {"url": "https://example.com/x"}}] * 11, "components[0].items"),
    ([{}], "components[0].items[0].media"),
    ([{"media": {}}], "components[0].items[0].media.url"),
    ([{"media": {"url": ""}}], "components[0].items[0].media.url"),
    ([{"media": {"url": 1}}], "components[0].items[0].media.url"),
    ([{"media": {"url": "https://example.com/x"}, "description": "x" * 1025}], "components[0].items[0].description"),
    ([{"media": {"url": "https://example.com/x"}, "spoiler": 1}], "components[0].items[0].spoiler"),
    ([{"media": {"url": "https://example.com/x"}, "type": 12}], "components[0].items[0].type"),
])
def test_media_gallery_invalid_item_names_field(items, field):
    with pytest.raises(RichPostError) as exc:
        compose_rich_post(components=[{"type": 12, "items": items}])
    assert exc.value.field == field
