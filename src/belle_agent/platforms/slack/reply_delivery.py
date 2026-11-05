"""Slack reply rendering, delivery, and recovery from truncated messages."""

import logging
from collections.abc import Mapping
from typing import Any

from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack import questions as slack_questions
from belle_agent.platforms.slack.config import (
    BLOCKKIT_TABLES_AUTO,
    BLOCKKIT_TABLES_DEFAULT,
    BLOCKKIT_TABLES_MODES,
    BLOCKKIT_TABLES_OFF,
    resolve_render_tables,
    settled_word,
)
from belle_agent.platforms.slack.cron import CronPush
from belle_agent.platforms.slack.delivery import (
    SlackDeliveryError,
    block_failure_text,
    describe_refused_blocks,
    describe_validation_errors,
    drop_refused_blocks,
    post_payload,
    response_ts,
    short_echo_tail,
    validate_blocks,
)
from belle_agent.platforms.slack.identity import click_target
from belle_agent.platforms.slack.inputs import clamp as _clamp
from belle_agent.platforms.slack.questions import MAX_SECTION_TEXT_LENGTH as _MAX_SECTION_TEXT_LENGTH
from belle_agent.platforms.slack.text import (
    MAX_SLACK_TEXT_LENGTH as _MAX_SLACK_TEXT_LENGTH,
)
from belle_agent.platforms.slack.text import (
    MAX_SLACK_UPDATE_TEXT_LENGTH as _MAX_SLACK_UPDATE_TEXT_LENGTH,
)
from belle_agent.platforms.slack.text import (
    fenced_regions_stripped,
    preferred_split_index,
    split_text,
)

logger = logging.getLogger(__name__)
_MAX_RATE_LIMIT_RETRIES = 3
_SLACK_TRUNCATION_NOTICE = "…Slack cut the message above short. The rest of it follows."
_BLOCKKIT_FALLBACK_GENERIC_TEXT = "Sent a Block Kit message."
_BLOCKKIT_FALLBACK_TEXT_LIMIT = 150


class SlackReplyDelivery:
    #: How the lines of the class that uses this mixin are tagged. A connector sets its own.
    _log_tag: str = "[slack]"

    def _blocks_for(
        self, text: str, requested: bool | None
    ) -> list[dict[str, Any]] | None:
        """Render one normalized mrkdwn chunk, or keep it as plain text."""
        return self._blocks_and_kind_for(text, requested)[0]

    def _blocks_and_kind_for(
        self, text: str, requested: bool | None
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """Render one chunk and retain its source kind for failure handling."""
        mode = self._blockkit_tables_mode()
        if mode == BLOCKKIT_TABLES_OFF or requested is False:
            return None, slack_blocks.BLOCK_KIND_UNKNOWN
        if requested is not True and mode != BLOCKKIT_TABLES_AUTO:
            return None, slack_blocks.BLOCK_KIND_UNKNOWN
        rendered = slack_blocks.render_blocks_with_kind(
            text,
            requested=requested is True,
            render_tables=self._render_tables_mode(),
        )
        if rendered is None:
            return None, slack_blocks.BLOCK_KIND_UNKNOWN
        return rendered

    def _blockkit_tables_mode(self) -> str:
        return settled_word(
            "blockkit_tables",
            self.config.blockkit_tables or "",
            BLOCKKIT_TABLES_MODES,
            BLOCKKIT_TABLES_DEFAULT,
        )

    def _render_tables_mode(self) -> str:
        """Resolve table rendering with the gateway's config parser."""
        return resolve_render_tables({"render_tables": self.config.render_tables})

    async def _post_chunks(
        self, chunks: list[str], *, channel_id: str, thread_ts: str,
        block_request: bool | None, start: int = 0,
    ) -> None:
        """Post remaining chunks and fail on the first refusal."""
        for index, chunk in enumerate(chunks[start:], start=start):
            blocks, block_kind = self._blocks_and_kind_for(chunk, block_request)
            sent, _, error = await self._post_text(
                channel_id=channel_id,
                text=chunk,
                thread_ts=thread_ts,
                chunk_index=index + 1,
                chunk_total=len(chunks),
                blocks=blocks,
                block_kind=block_kind,
            )
            if not sent:
                raise SlackDeliveryError(
                    error,
                    channel_id=channel_id,
                    chunks_sent=index,
                    chunks_total=len(chunks),
                )

    async def _post_text(
        self,
        *,
        channel_id: str,
        text: str,
        thread_ts: str,
        update_ts: str = "",
        chunk_index: int = 0,
        chunk_total: int = 0,
        blocks: list[dict[str, Any]] | None = None,
        block_kind: str = slack_blocks.BLOCK_KIND_UNKNOWN,
        block_text_route: str = "",
        repair_truncation: bool = True,
    ) -> tuple[bool, str, str]:
        """Post one chunk and return its delivery result, timestamp, and error."""
        if blocks and not fenced_regions_stripped(text).strip():
            text = _clamp(
                slack_blocks.blockkit_fallback_summary(blocks)
                or _BLOCKKIT_FALLBACK_GENERIC_TEXT,
                _BLOCKKIT_FALLBACK_TEXT_LIMIT,
            )
        kwargs: dict[str, Any] = {"channel": channel_id, "text": text}
        if blocks is not None:
            kwargs["blocks"] = blocks
        if update_ts:
            kwargs["ts"] = update_ts
        elif thread_ts:
            kwargs["thread_ts"] = thread_ts

        text_limit = (
            _MAX_SLACK_UPDATE_TEXT_LENGTH if update_ts else _MAX_SLACK_TEXT_LENGTH
        )

        def fallback_text(
            refused: list[dict[str, Any]], errors: list[Mapping[str, Any]]
        ) -> str:
            return _clamp(
                block_failure_text(
                    text=text,
                    blocks=refused,
                    kind=block_kind,
                    errors=errors,
                    text_route=block_text_route,
                ),
                text_limit,
            )

        refused_blocks = list(blocks) if blocks else []
        refusal_code, errors = await validate_blocks(
            self._client, blocks, block_kind, logger, tag=self._log_tag
        )
        if errors:
            logger.warning(
                "Slack refused a Block Kit payload of kind %s before it was "
                "sent (%s); Slack's validator reported: %s; the payload it "
                "named: %s; delivering the message without it",
                block_kind,
                refusal_code or "refused",
                describe_validation_errors(errors),
                describe_refused_blocks(refused_blocks, errors),
            )
            text = fallback_text(blocks or [], errors)
            kwargs["text"] = text
            drop_refused_blocks(kwargs, update_ts=update_ts)
            blocks = None

        response, blocks, error = await post_payload(
            self._client, kwargs, update_ts=update_ts, blocks=blocks,
            fallback_text=fallback_text, logger=logger, tag=self._log_tag,
            retry_limit=_MAX_RATE_LIMIT_RETRIES,
        )
        if error:
            return False, "", error
        text = kwargs["text"]
        posted_ts = response_ts(response)
        dropped = short_echo_tail(
            response=response,
            channel_id=channel_id,
            response_ts=posted_ts,
            sent_text=text,
            update_ts=update_ts,
            had_blocks=bool(blocks),
            logger=logger,
            tag=self._log_tag,
        )
        if dropped and repair_truncation and not update_ts:
            await self._post_truncated_tail(
                channel_id=channel_id,
                thread_ts=thread_ts,
                dropped=dropped,
            )
        mode = "update" if update_ts else "post"
        if blocks is not None:
            mode = f"{mode}+blocks"
        if chunk_total:
            logger.info(
                "%s delivered text: channel=%s ts=%s chunk=%d/%d mode=%s",
                self._log_tag,
                channel_id,
                posted_ts or "-",
                chunk_index,
                chunk_total,
                mode,
            )
        else:
            logger.debug(
                "%s streaming edit applied: channel=%s ts=%s mode=%s",
                self._log_tag,
                channel_id,
                posted_ts or "-",
                mode,
            )
        return True, posted_ts, ""

    async def _post_truncated_tail(
        self, *, channel_id: str, thread_ts: str, dropped: str
    ) -> None:
        """Post the remainder of a reply that exceeded the edit limit."""
        tail = dropped.strip()
        if not tail:
            return
        chunks = split_text(f"{_SLACK_TRUNCATION_NOTICE}\n\n{tail}")
        for index, chunk in enumerate(chunks):
            sent, _, error = await self._post_text(
                channel_id=channel_id,
                text=chunk,
                thread_ts=thread_ts,
                chunk_index=index + 1,
                chunk_total=len(chunks),
                repair_truncation=False,
            )
            if not sent:
                logger.warning(
                    "%s the part Slack cut could not be posted "
                    "beside it: channel=%s chunk=%d/%d error=%s",
                    self._log_tag,
                    channel_id,
                    index + 1,
                    len(chunks),
                    error or "-",
                )
                return

    @classmethod
    def _section_blocks_for(cls, text: str) -> list[dict[str, Any]] | None:
        """Render text as sections, or return ``None`` if it will not fit."""
        remaining = (text or "").strip()
        blocks: list[dict[str, Any]] = []
        while remaining:
            split_at = (
                len(remaining) if len(remaining) <= _MAX_SECTION_TEXT_LENGTH
                else preferred_split_index(remaining, _MAX_SECTION_TEXT_LENGTH)
            )
            piece, remaining = remaining[:split_at].rstrip(), remaining[split_at:].lstrip()
            if not piece:
                piece, remaining = remaining[:_MAX_SECTION_TEXT_LENGTH], remaining[_MAX_SECTION_TEXT_LENGTH:]
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": piece}}
            )
            if len(blocks) >= slack_blocks.MAX_BLOCKS_PER_MESSAGE:
                return None
        return blocks or None

    def _root_blocks_and_kind_for(
        self, text: str, requested: bool | None, cron: CronPush | None
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """Render a root message and its optional cron status card."""
        if cron is None or cron.card is None:
            return self._blocks_and_kind_for(text, requested)
        if not cron.body_below_card:
            return [cron.card], slack_blocks.BLOCK_KIND_CHROME
        body, kind = self._blocks_and_kind_for(text, requested)
        if body is None:
            # Blocks replace text, so the card also needs body blocks.
            body = self._section_blocks_for(text)
            kind = slack_blocks.BLOCK_KIND_CHROME
        if body is None:
            return None, slack_blocks.BLOCK_KIND_UNKNOWN
        if len(body) + 1 > slack_blocks.MAX_BLOCKS_PER_MESSAGE:
            # Keep the result when the card would exceed Slack's block limit.
            logger.info(
                "%s cron run %s renders to the full block budget; "
                "delivering the result without a status card",
                self._log_tag,
                cron.run_id,
            )
            return body, kind
        return [cron.card, *body], slack_blocks.combine_block_kinds(
            (kind, slack_blocks.BLOCK_KIND_CHROME)
        )

    async def _post_root(
        self,
        *,
        channel_id: str,
        text: str,
        thread_ts: str,
        update_ts: str,
        blocks: list[dict[str, Any]] | None,
        chunk_index: int,
        chunk_total: int,
        cron: CronPush | None,
        block_kind: str = slack_blocks.BLOCK_KIND_UNKNOWN,
    ) -> tuple[bool, str, str]:
        """Post or update the first reply. Retry a failed cron edit as a post."""
        attempts = (update_ts, "") if update_ts and cron is not None else (update_ts,)
        for target_ts in attempts:
            sent, message_ts, error = await self._post_text(
                channel_id=channel_id,
                text=text,
                thread_ts=thread_ts,
                update_ts=target_ts,
                chunk_index=chunk_index,
                chunk_total=chunk_total,
                blocks=blocks,
                block_kind=block_kind,
            )
            if sent:
                break
            if target_ts and cron is not None:
                logger.warning(
                    "Slack refused to rewrite the record for cron run %s (%s); "
                    "posting the result as a new message",
                    cron.run_id,
                    error,
                )
        if not sent:
            return False, "", error
        # An edit answers with the ts it edited; keep it either way, since it
        # is what the rest of the reply threads under.
        message_ts = message_ts or update_ts
        if cron is not None and cron.is_placeholder and message_ts:
            self._remember_cron_record(cron, message_ts)
        return True, message_ts, ""

    async def _notify_clicker(
        self, body: Mapping[str, Any], user_id: str, text: str, *, notice: str
    ) -> None:
        """Tell the clicker about the action through an ephemeral message."""
        channel_id, _message_ts = click_target(body)
        await self._post_ephemeral(channel_id, user_id, text, notice=notice)

    async def _post_ephemeral(
        self, channel_id: str, user_id: str, text: str, *, notice: str
    ) -> None:
        """Post a message visible only to one user."""
        if self._client is None or not user_id or not channel_id:
            return
        try:
            await self._client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=_clamp(text, _MAX_SLACK_TEXT_LENGTH),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Slack could not tell %s what became of their message: %s",
                user_id or "-",
                exc,
            )
        else:
            logger.info(
                "Slack told %s what became of their message: %s",
                user_id,
                notice,
            )

    async def _close_question_message(
        self, body: Mapping[str, Any], *, note: str
    ) -> None:
        """Remove answered or withdrawn controls without removing the question."""
        if self._client is None:
            return
        message = body.get("message")
        message = message if isinstance(message, Mapping) else {}
        channel_id, message_ts = click_target(body)
        if not channel_id or not message_ts:
            logger.warning("Slack answer arrived without a message to update")
            return

        blocks = slack_questions.closed_blocks(message.get("blocks"), note)
        text = str(message.get("text") or "").strip()
        sent, _, error = await self._post_text(
            channel_id=channel_id,
            text=_clamp(f"{text}\n\n{note}".strip(), _MAX_SLACK_TEXT_LENGTH),
            thread_ts="",
            update_ts=message_ts,
            blocks=blocks,
            # The remaining blocks have no controls.
            block_kind=slack_blocks.BLOCK_KIND_CHROME,
        )
        if not sent:
            logger.warning(
                "Slack could not withdraw the buttons of an answered question: %s",
                error,
            )
