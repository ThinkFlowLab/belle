"""Recall shared documents for each turn."""

import asyncio
import logging
from pathlib import Path
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.prompt_attachment_manager import (
    PromptAttachmentKind,
    PromptAttachmentManager,
)
from openjiuwen.harness.rails.base import DeepAgentRail

from belle_agent.integrations.openjiuwen.second_brain.tools import create_second_brain_tools
from belle_agent.second_brain.backend import SecondBrainBackend
from belle_agent.second_brain.index import get_second_brain_manager
from belle_agent.second_brain.recall import SYSTEM_INSTRUCTIONS, SecondBrainRecall, query_text
from belle_agent.second_brain.tools import SecondBrainToolContext

logger = logging.getLogger(__name__)

_SECTION_NAME = "second_brain"
_ATTACHMENT_SECTION = "second_brain_context"

class SecondBrainRail(SecondBrainRecall, DeepAgentRail):
    """Attach relevant shared documents and register corpus tools."""

    #: Below personal memory's priority of 80.
    priority = 78

    def __init__(
        self,
        *,
        corpus_dir: str | Path,
        top_k: int = 5,
        min_score: float = 0.35,
        max_total_bytes: int = 10240,
        config: dict[str, Any] | None = None,
        backend: SecondBrainBackend | None = None,
    ) -> None:
        super().__init__(
            corpus_dir=corpus_dir, top_k=top_k, min_score=min_score,
            max_total_bytes=max_total_bytes, config=config, backend=backend,
        )

        self.system_prompt_builder: Any = None
        self.attachment_manager: PromptAttachmentManager | None = None
        self._manager_initialized = False
        self._prefetch_task: asyncio.Task | None = None
        self._recalled: str | None = None
        self._owned_tool_cards: dict[str, Any] = {}
        self._tool_ctx: SecondBrainToolContext | None = None

    def init(self, agent) -> None:
        super().init(agent)
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        manager = getattr(agent, "prompt_attachment_manager", None)
        self.attachment_manager = (
            manager if isinstance(manager, PromptAttachmentManager) else None
        )
        self._register_tools(agent)

    def uninit(self, agent) -> None:
        if hasattr(agent, "ability_manager"):
            for name, card in list(self._owned_tool_cards.items()):
                # Another rail may own the name by now.
                if agent.ability_manager.get(name) is not card:
                    continue
                try:
                    agent.ability_manager.remove_ability(name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[SecondBrainRail] could not remove tool %s: %s", name, exc)
        self._owned_tool_cards.clear()
        self._tool_ctx = None
        self._manager_initialized = False
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(_SECTION_NAME)
            self.system_prompt_builder = None
        self.attachment_manager = None

    def _register_tools(self, agent) -> None:
        if not hasattr(agent, "ability_manager"):
            logger.warning("[SecondBrainRail] agent has no ability_manager")
            return
        self._tool_ctx = SecondBrainToolContext(
            corpus_dir=self._corpus_dir,
            manager=None,
            top_k=self._top_k,
            min_score=self._min_score,
            config=self._config,
            backend=self._backend,
        )
        for tool in create_second_brain_tools(self._tool_ctx):
            card = getattr(tool, "card", None)
            if card is None:
                continue
            try:
                result = agent.ability_manager.add_ability(card, tool)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[SecondBrainRail] could not register %s: %s", card.name, exc)
                continue
            if getattr(result, "added", False):
                self._owned_tool_cards[card.name] = card

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if self._manager is None:
            # Retry until ingestion creates an index.
            self._manager = await get_second_brain_manager(
                self._config, corpus_dir=self._corpus_dir, backend=self._backend
            )
            if self._manager is None and not self._manager_initialized:
                logger.info(
                    "[SecondBrainRail] no index yet at %s; will retry each"
                    " turn until something is ingested",
                    self._corpus_dir,
                )
            self._manager_initialized = True
            if self._manager is not None and self._tool_ctx is not None:
                self._tool_ctx.manager = self._manager

        self._recalled = None
        self._prefetch_task = None

        # A cron or heartbeat run has no question to recall against.
        if isinstance(ctx.inputs, InvokeInputs) and (
            ctx.inputs.is_cron() or ctx.inputs.is_heartbeat()
        ):
            return
        if self._manager is None:
            return

        query = self._last_user_query(ctx)
        if query:
            self._prefetch_task = asyncio.create_task(self.recall(query))

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if self.system_prompt_builder is None:
            return

        self.system_prompt_builder.remove_section(_SECTION_NAME)

        if self._prefetch_task is not None and self._recalled is None and self._prefetch_task.done():
            try:
                self._recalled = self._prefetch_task.result()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SecondBrainRail] recall failed: %s", exc)
                self._recalled = None
            self._prefetch_task = None
            # A pending search never blocks this model call.

        if self._recalled:
            await self._upsert_attachment(ctx, self._recalled)
        else:
            if self._prefetch_task is not None and not self._prefetch_task.done():
                logger.info(
                    "[SecondBrainRail] recall still running at model call;"
                    " this call goes without it"
                )
            await self._clear_attachment(ctx)

        self.system_prompt_builder.add_section(self._system_section())

    def _system_section(self) -> PromptSection:
        """Add corpus use and trust instructions in the selected language."""
        return PromptSection(
            name=_SECTION_NAME,
            content=SYSTEM_INSTRUCTIONS,
            priority=self.priority,
        )

    async def _upsert_attachment(self, ctx: AgentCallbackContext, content: str) -> None:
        if self.attachment_manager is None:
            return
        writer = self.attachment_manager.bind_context(ctx)
        try:
            await writer.add_section(
                section=_ATTACHMENT_SECTION,
                content=content,
                # Untrusted content stays outside the system prompt.
                kind=PromptAttachmentKind.MEMORY,
                source="belle_agent.second_brain_rail",
                priority=self.priority,
                content_kind="text/markdown",
            )
        except ValueError as exc:
            logger.warning("[SecondBrainRail] skipped attachment: %s", exc)

    async def _clear_attachment(self, ctx: AgentCallbackContext) -> None:
        if self.attachment_manager is None:
            return
        writer = self.attachment_manager.bind_context(ctx)
        try:
            await writer.clear_section(_ATTACHMENT_SECTION)
        except ValueError:
            return

    @staticmethod
    def _last_user_query(ctx: AgentCallbackContext) -> str | None:
        """Get the latest user text from either supported input shape."""
        inputs = getattr(ctx, "inputs", None)
        if inputs is None:
            return None

        direct = query_text(getattr(inputs, "query", None))
        if direct:
            return direct

        for message in reversed(getattr(inputs, "messages", None) or []):
            if getattr(message, "role", "") != "user":
                continue
            text = query_text(getattr(message, "content", None))
            if text:
                return text
        return None




__all__ = ["SecondBrainRail"]
