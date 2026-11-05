"""Keep the Slack guide aligned with the connector's declared settings."""

import re
from pathlib import Path

from belle_agent.integrations.jiuwenswarm import scope_capabilities  # noqa: F401
from belle_agent.scopes import AXIS_CHAT_TYPE, channel_capabilities

DOCS = Path(__file__).resolve().parents[2] / "docs"
GUIDE = (DOCS / "platforms/slack.md").read_text(encoding="utf-8")


def test_guide_explains_triggers():
    for trigger in ("mention", "reply", "all", "url", "has_file"):
        assert f"`{trigger}`" in GUIDE, trigger
    for detail in ("not implicit", "[trigger: url]", "`models.defaults`", "reasoning", "`work_mode`"):
        assert detail in GUIDE, detail


def test_guide_lists_declared_delivery_keys():
    for key in channel_capabilities("slack").keys_for("delivery"):
        assert f"`{key}`" in GUIDE, key


def test_guide_explains_app_home_scope():
    for detail in ("only in the bot's DM", "channel never matches App Home", "match: {channel: slack}"):
        assert detail in GUIDE, detail


def test_guide_documents_declared_chat_kinds():
    declared = channel_capabilities("slack").values_for(AXIS_CHAT_TYPE)
    assert declared == {"channel", "group", "im", "mpim"}
    assert all(f"`{kind}`" in GUIDE for kind in declared)


def test_guide_local_links_resolve():
    for target in re.findall(r"\]\(([^)]+)\)", GUIDE):
        if "://" in target or target.startswith("#"):
            continue
        assert (DOCS / "platforms" / target.split("#", 1)[0]).exists(), target
