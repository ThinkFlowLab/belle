"""Publish or clear the requester's private Slack App Home tab."""

import json
import logging
from collections.abc import Mapping
from typing import Any

from . import blocks as slack_blocks
from .history_policy import (
    METADATA_ASKER_KEY,
    SlackWorkspaceUnresolved,
)
from .json_toolkit import (
    SlackMethodToolkitBase,
    slack_refusal_json,
)
from .text import normalize_slack_mrkdwn, split_text
from .tool_common import (
    _SlackCallFailure,
)

logger = logging.getLogger(__name__)

#: Tool names in registration order.
HOME_TAB_TOOL_NAMES: tuple[str, ...] = (
    "publish_slack_home_tab",
    "clear_slack_home_tab",
)

_VIEWS_PUBLISH = "views_publish"

_HOME_VIEW_TYPE = "home"

#: Operator actions for refusals that need more than an error code.
_PUBLISH_FAILURE_DETAIL: Mapping[str, str] = {
    "not_enabled": (
        "this Slack app has no Home tab to publish into. It is turned on by"
        " features.app_home.home_tab_enabled in the app manifest, which is the"
        " app's own configuration rather than an OAuth scope, so switching it"
        " on needs no reinstall. Until somebody does, nothing can be published"
        " and calling again will not help"
    ),
    "view_too_large": (
        "Slack refused the view for its size; a published view may be at most"
        " 250 KB. Publish less, or publish a summary and put the rest in a"
        " message"
    ),
}


#: The ``tab`` of an ``app_home_opened`` event that is the Home tab. The other is the direct message.
HOME_TAB = "home"

HOME_TAB_WELCOME = (
    "*Welcome.*\n"
    "This page is yours alone. Nobody else can see it."
    " Ask {bot} to put something here.\n"
    "Type `{command} help` to see what {bot} can do."
)


def home_tab_welcome_outcome(event: Mapping[str, Any]) -> str | None:
    """Say why an App Home visit gets no welcome, or ``None`` when it gets one.

    Only an empty Home tab is welcomed. A publish replaces the whole page, and
    Slack keeps no history of one.
    """
    if str(event.get("tab") or "").strip().lower() != HOME_TAB:
        return ""
    view = event.get("view")
    if isinstance(view, Mapping) and view.get("blocks"):
        return "ignored:home-tab-already-has-content"
    return None


async def publish_home_tab_welcome(toolkit: "SlackHomeTabToolkit", *, bot_name: str, command: str) -> bool:
    """Publish the welcome onto the Home tab of the person ``toolkit`` is bound to. Return whether Slack took it."""
    text = HOME_TAB_WELCOME.format(bot=bot_name or "the assistant", command=command)
    result = await toolkit.publish_slack_home_tab(text=text)
    try:
        return bool(json.loads(result).get("ok"))
    except (TypeError, ValueError, AttributeError):
        return False


def slack_home_tab_request_metadata(
    channel_id: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Require a Slack request with a named requester."""
    if str(channel_id or "").strip().lower() != "slack":
        return {}
    if not isinstance(metadata, Mapping):
        return {}
    if not str(metadata.get(METADATA_ASKER_KEY) or "").strip():
        return {}
    return dict(metadata)


def _home_tab_refusal_json(
    code: str,
    detail: str = "",
    *,
    user_id: str = "",
) -> str:
    """Describe a failed call without claiming the view changed."""
    return slack_refusal_json(logger, "home tab", code, detail, user_id=user_id)


class SlackHomeTabToolkit(SlackMethodToolkitBase):
    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            _publish_tool_definition(),
            _clear_tool_definition(),
        ]

    """The two Home tab tools, scoped to the Slack request in flight."""

    #: Publish deadline, including retries.
    DEFAULT_TIMEOUT_SECONDS = 30.0

    #: Replaced per request by the setting of the install it arrived from.
    _render_tables = slack_blocks.RENDER_TABLES_DEFAULT

    async def _load_settings(self, metadata: Mapping[str, Any]) -> Mapping[str, Any]:
        """Add the install's Block Kit setting to the bound request."""
        slack = await super()._load_settings(metadata)
        self._render_tables = slack_blocks.render_tables_setting(slack)
        return slack

    def _view_blocks(self, text: str) -> list[dict[str, Any]]:
        """Render text as Home tab blocks.

        Home views have no plain-text field, so declined renderings become
        section blocks containing the original text.
        """
        rendered: list[dict[str, Any]] = []
        for chunk in split_text(normalize_slack_mrkdwn(text)):
            blocks = slack_blocks.render_blocks(
                chunk,
                render_tables=self._render_tables,
            )
            rendered.extend(blocks or slack_blocks.section_blocks(chunk))
        return rendered

    def _slack_refusal(self, exc: _SlackCallFailure, *, user_id: str) -> str:
        """One Slack refusal as the shape every failure here returns."""
        detail = _PUBLISH_FAILURE_DETAIL.get(exc.code, "")
        if not detail:
            detail = f"Slack refused {exc.where}"
        return _home_tab_refusal_json(exc.code, detail, user_id=user_id)

    @staticmethod
    def _no_requester_refusal() -> str:
        """Refuse requests without a trusted requester."""
        return _home_tab_refusal_json(
            "trusted_slack_requester_required",
            "this request names nobody, so there is no Home tab to act on. A"
            " Home tab belongs to one person and this tool only ever acts on"
            " the tab of whoever asked; there is no other tab to fall back to",
        )

    async def _publish(self, blocks: list[dict[str, Any]], *, user_id: str, hash: str | None = None) -> str:
        """Replace the requester's view with these blocks."""
        if len(blocks) > slack_blocks.MAX_BLOCKS_PER_VIEW:
            return _home_tab_refusal_json(
                "home_tab_too_many_blocks",
                f"a published view holds at most"
                f" {slack_blocks.MAX_BLOCKS_PER_VIEW} blocks and this one came"
                f" to {len(blocks)}; publish less, or publish a summary and put"
                f" the rest in a message",
                user_id=user_id,
            )

        try:
            kwargs: dict[str, Any] = {"user_id": user_id, "view": {"type": _HOME_VIEW_TYPE, "blocks": blocks}}
            if hash is not None:
                kwargs["hash"] = hash
            response = await self._call(_VIEWS_PUBLISH, **kwargs)
        except _SlackCallFailure as exc:
            if exc.code == "hash_conflict":
                return _home_tab_refusal_json(
                    "hash_conflict", "the Home tab changed since that hash was read; refresh it before replacing",
                    user_id=user_id,
                )
            return self._slack_refusal(exc, user_id=user_id)

        logger.info("slack home tab: published %d block(s) for %s", len(blocks), user_id)
        result: dict[str, Any] = {
                "ok": True,
                "user_id": user_id,
                "blocks": len(blocks),
                "replaced_a_view": True,
        }
        view = response.get("view")
        if isinstance(view, Mapping) and isinstance(view.get("hash"), str):
            result["view_hash"] = view["hash"]
        return json.dumps(result, ensure_ascii=False)

    async def publish_slack_home_tab(
        self,
        text: str | None = None,
        blocks: Any = None,
        hash: str | None = None,
    ) -> str:
        """Publish a view onto the asker's Home tab, replacing what is there."""
        if hash is not None and (not isinstance(hash, str) or not hash):
            return _home_tab_refusal_json("hash_invalid", "hash must be a nonempty string from a previous Home view")
        metadata = self._runtime_metadata()
        user_id = self._requester(metadata)
        if not user_id:
            return self._no_requester_refusal()

        body = str(text or "").strip()
        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _home_tab_refusal_json(unresolved.code, unresolved.detail, user_id=user_id)

        declared = slack_blocks.apply_block_policy(blocks)
        if blocks is not None and declared is None:
            if not body:
                return _home_tab_refusal_json(
                    "blocks_refused",
                    "the blocks were refused because they are malformed or"
                    " contain interactive controls, and no text was given to"
                    " publish in their place",
                    user_id=user_id,
                )
            logger.warning("slack home tab: declared blocks were refused; publishing text instead")

        view_blocks = declared if declared is not None else self._view_blocks(body)
        if not view_blocks:
            # Empty views clear App Home; only clear_slack_home_tab may do that.
            return _home_tab_refusal_json(
                "nothing_to_publish",
                "Slack did not change the Home tab because there are no valid"
                " blocks to publish. Use clear_slack_home_tab to empty it.",
                user_id=user_id,
            )
        return await self._publish(view_blocks, user_id=user_id, hash=hash)

    async def clear_slack_home_tab(self, hash: str | None = None) -> str:
        """Empty the asker's Home tab, leaving Slack's empty state on it."""
        if hash is not None and (not isinstance(hash, str) or not hash):
            return _home_tab_refusal_json("hash_invalid", "hash must be a nonempty string from a previous Home view")
        metadata = self._runtime_metadata()
        user_id = self._requester(metadata)
        if not user_id:
            return self._no_requester_refusal()
        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _home_tab_refusal_json(unresolved.code, unresolved.detail, user_id=user_id)
        return await self._publish([], user_id=user_id, hash=hash)

def _publish_tool_definition() -> dict[str, Any]:
    return {
        "name": "publish_slack_home_tab",
        "description": (
            "Publish a private Home tab for the person who asked this turn. "
            "Use a direct message to show content to another person."
            "\nEach call replaces the whole page. Slack keeps no history. "
            "Publishing sends no notification. Tell the person in a message "
            "when they need to see the new page."
            "\nPass text for normal reply rendering, including mermaid, "
            "vega-lite, and blockkit fences. Pass blocks for a Block Kit "
            "payload. Both follow the message renderer's rules. Passing "
            "neither changes nothing; use clear_slack_home_tab to empty it."
            "\nA page holds at most 100 blocks. Images need a public URL or "
            "a Slack file this app can see. This tool never uploads a file. "
            "Home tabs must be enabled in the Slack app configuration."
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "The page, written the way a Slack reply is written. "
                        "Markdown, and fenced mermaid, vega-lite or blockkit "
                        "blocks render as they would in a message."
                    ),
                },
                "blocks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "A Block Kit payload, as a list of blocks. Use it "
                        "instead of text when the page is a layout rather than "
                        "prose. It is checked against the same rules a fenced "
                        "blockkit payload is checked against."
                    ),
                },
                "hash": {
                    "type": "string",
                    "description": "Optional hash of the Home view being replaced. Slack refuses the change if the view has changed.",
                },
            },
            "required": [],
        },
    }


def _clear_tool_definition() -> dict[str, Any]:
    return {
        "name": "clear_slack_home_tab",
        "description": (
            "Empty your Home tab in Slack, leaving the blank state a person "
            "sees before anything has ever been published there."
            "\nWhose tab. Always the tab of the person who asked for this "
            "turn, as publish_slack_home_tab is. There is no argument for anybody "
            "else's."
            "\nIt cannot be undone. Slack keeps no history of a Home tab, so "
            "whatever the page held is gone and can only come back by being "
            "published again. Clear the tab when its content is finished with "
            "or is misleading, not to make room for something you are about "
            "to publish -- publishing already replaces the whole page."
            "\nNo notification. Clearing tells nobody, exactly as publishing "
            "tells nobody."
            "\nIts one argument, hash, is optional: give the hash of the Home "
            "view you last saw, and Slack refuses the change if the view has "
            "changed since. Calling it twice is safe: the second call leaves "
            "an already empty tab empty."
        ),
        "input_params": {
            "type": "object",
            "properties": {"hash": {"type": "string", "description": "Optional current Home view hash for concurrency protection."}},
            "required": [],
        },
    }
