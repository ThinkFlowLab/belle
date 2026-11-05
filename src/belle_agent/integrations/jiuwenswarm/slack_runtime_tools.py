"""Mount Slack runtime tools and their request-scoped safety rail."""

import logging
from collections.abc import Callable
from functools import partial
from typing import Any

from belle_agent.integrations.jiuwenswarm.slack_bookmarks import SlackBookmarkToolkit, slack_bookmark_request_metadata
from belle_agent.integrations.jiuwenswarm.slack_canvases import SlackCanvasToolkit, slack_canvas_request_metadata
from belle_agent.integrations.jiuwenswarm.slack_directory import SlackDirectoryToolkit, slack_find_request_metadata
from belle_agent.integrations.jiuwenswarm.slack_history import SlackHistoryToolkit
from belle_agent.integrations.jiuwenswarm.slack_home_tab import SlackHomeTabToolkit, slack_home_tab_request_metadata
from belle_agent.integrations.jiuwenswarm.slack_lists import SlackListToolkit, slack_list_request_metadata
from belle_agent.integrations.jiuwenswarm.slack_pins import SlackPinToolkit, slack_pin_request_metadata
from belle_agent.integrations.jiuwenswarm.slack_post import SlackPostToolkit, slack_post_request_metadata
from belle_agent.integrations.jiuwenswarm.slack_reactions import SlackReactionToolkit, slack_reaction_request_metadata
from belle_agent.integrations.jiuwenswarm.slack_routing import (
    filter_slack_history_request_metadata as _filter_slack_history_request_metadata,
)
from belle_agent.integrations.jiuwenswarm.slack_routing import (
    request_carries_slack_history_context as _request_carries_slack_history_context,
)
from belle_agent.integrations.jiuwenswarm.slack_scheduled import SlackScheduledToolkit
from belle_agent.integrations.jiuwenswarm.slack_search import SlackSearchToolkit, slack_search_request_metadata
from belle_agent.integrations.jiuwenswarm.slack_write_confirmation_rail import SlackWriteConfirmationRail
from belle_agent.integrations.openjiuwen.agent_plugin.load import load_plugin

logger = logging.getLogger("jiuwenswarm.server.runtime.agent_adapter.interface_deep")


class SlackRuntimeTools:
    """Own Slack toolkit state and mount it for trusted Slack requests."""

    def __init__(self, agent: Any, request_context: Any) -> None:
        self._instance = agent
        self._runtime_cron_tool_context = request_context
        self._slack_history_toolkit: SlackHistoryToolkit | None = None
        self._slack_history_tools: list[Any] = []
        self._slack_toolkits: dict[str, tuple[Any, list[Any]]] = {}
        self._slack_post_toolkit: SlackPostToolkit | None = None
        self._slack_post_tools: list[Any] = []
        self._slack_write_rail: SlackWriteConfirmationRail | None = None
        self._slack_access_rails: dict[str, SlackWriteConfirmationRail] = {}
        self._slack_tool_plugin_records: dict[str, tuple[Any, Any]] = {}

    async def unmount(self) -> list[Any]:
        return await self._unmount_slack_tool_cards()

    async def remount(self, tools: list[Any]) -> None:
        await self._register_slack_tool_cards(tools)

    def rails(self) -> tuple[Any, ...]:
        return (*((self._slack_write_rail,) if self._slack_write_rail is not None else ()), *self._slack_access_rails.values())

    async def refresh(self) -> None:
        # Each toolkit has its own request predicate; history may return early.
        for name, toolkit_type, metadata_provider in (
            ("search", SlackSearchToolkit, partial(self._slack_context_metadata, slack_search_request_metadata)),
            ("reaction", SlackReactionToolkit, self._get_slack_reaction_request_metadata),
            ("pin", SlackPinToolkit, partial(self._slack_context_metadata, slack_pin_request_metadata)),
            ("bookmark", SlackBookmarkToolkit, partial(self._slack_context_metadata, slack_bookmark_request_metadata)),
            ("directory", SlackDirectoryToolkit, self._get_slack_directory_request_metadata),
        ):
            await self._mount_slack_toolkit(name, toolkit_type, metadata_provider)
        await self._refresh_slack_post_runtime_tools()
        for name, toolkit_type, metadata_provider in (
            ("home_tab", SlackHomeTabToolkit, partial(self._slack_context_metadata, slack_home_tab_request_metadata)),
            ("canvas", SlackCanvasToolkit, partial(self._slack_context_metadata, slack_canvas_request_metadata)),
            ("list", SlackListToolkit, partial(self._slack_context_metadata, slack_list_request_metadata)),
            ("scheduled", SlackScheduledToolkit, partial(self._slack_context_metadata, slack_post_request_metadata)),
        ):
            await self._mount_slack_toolkit(name, toolkit_type, metadata_provider)

        # History, files, and pins use the trusted conversation in this request.
        if not self._get_slack_history_request_metadata():
            return

        if self._slack_history_toolkit is None:
            self._slack_history_toolkit = SlackHistoryToolkit(
                metadata_provider=self._get_slack_history_request_metadata,
                session_id_provider=self._get_slack_history_session_id,
            )
            self._slack_history_tools = self._slack_history_toolkit.get_tools()
            await self._register_slack_tool_cards(self._slack_history_tools)
            return

        await self._register_slack_tool_cards(self._slack_history_tools)

    async def _register_slack_tool_cards(self, tools: list[Any]) -> None:
        """Mount request-checked tools through the agent plugin."""
        for tool in tools:
            name = tool.card.name
            if self._instance.ability_manager.get(name) is not None:
                continue  # Preserve an operator's same-name tool.
            previous = self._slack_tool_plugin_records.get(name)
            if previous is not None:
                await self._instance.unload_extension(previous[0])
                del self._slack_tool_plugin_records[name]
            record = await load_plugin(self._instance, tools=(tool,), skills=False)
            self._slack_tool_plugin_records[name] = (record, tool)

    async def _unmount_slack_tool_cards(self) -> list[Any]:
        """Release Belle-owned cards before another plugin claims their names."""
        unmounted = []
        for name, (record, tool) in list(self._slack_tool_plugin_records.items()):
            current = self._instance.ability_manager.get(name)
            if current is not None and current is not tool.card:
                continue
            await self._instance.unload_extension(record)
            del self._slack_tool_plugin_records[name]
            unmounted.append(tool)
        return unmounted

    async def _mount_slack_toolkit(
        self,
        name: str,
        toolkit_type: type,
        metadata_provider: Callable[[], dict[str, Any]],
    ) -> None:
        if not metadata_provider():
            return
        if name not in self._slack_toolkits:
            toolkit = toolkit_type(metadata_provider=metadata_provider)
            tools = toolkit.get_tools()
            if tools and name in {"canvas", "list", "scheduled"}:
                names = (
                    frozenset({"schedule_slack_message", "cancel_slack_scheduled_message"})
                    if name == "scheduled" else frozenset({f"share_slack_{name}"})
                )
                try:
                    rail = SlackWriteConfirmationRail(toolkit, tools=tools, tool_names=names, access_change=True)
                    await load_plugin(self._instance, rails=(rail,), skills=False)
                except Exception as exc:  # noqa: BLE001 - rail APIs vary by version.
                    logger.warning("Slack %s sharing unavailable: confirmation rail failed: %s", name, exc)
                    return
                self._slack_access_rails[name] = rail
            await self._register_slack_tool_cards(tools)
            self._slack_toolkits[name] = (toolkit, tools)
        else:
            await self._register_slack_tool_cards(self._slack_toolkits[name][1])

    def _slack_context_metadata(self, resolver: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        """Read the request context each time a toolkit calls its provider."""
        return resolver(
            self._runtime_cron_tool_context.channel_id,
            self._runtime_cron_tool_context.metadata,
        )

    def _get_slack_directory_request_metadata(self) -> dict[str, Any]:
        """Read name lookup metadata only for Slack request shapes."""
        return self._trusted_slack_metadata(slack_find_request_metadata)

    def _trusted_slack_metadata(self, resolver: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        if not _request_carries_slack_history_context(
            self._runtime_cron_tool_context.channel_id,
            self._runtime_cron_tool_context.metadata,
        ):
            return {}
        return resolver(self._runtime_cron_tool_context.metadata)

    async def _refresh_slack_post_runtime_tools(self) -> None:
        """Mount posting tools only after their widening-confirmation rail."""
        metadata = self._get_slack_post_request_metadata()
        if not metadata:
            return

        if self._slack_post_toolkit is None:
            toolkit = SlackPostToolkit(
                metadata_provider=self._get_slack_post_request_metadata,
            )
            tools = toolkit.get_tools()
            if not await self._register_slack_write_rail(toolkit, tools):
                logger.error("Slack posting tools unavailable: confirmation rail registration failed")
                return
            self._slack_post_toolkit = toolkit
            self._slack_post_tools = tools
            await self._register_slack_tool_cards(self._slack_post_tools)
            return

        await self._register_slack_tool_cards(self._slack_post_tools)

    async def _register_slack_write_rail(self, toolkit: SlackPostToolkit, tools: list[Any]) -> bool:
        """Return false if the posting rail cannot be registered."""
        if self._slack_write_rail is not None:
            return True
        try:
            rail = SlackWriteConfirmationRail(toolkit, tools=tools)
            await load_plugin(self._instance, rails=(rail,), skills=False)
        except Exception as exc:  # noqa: BLE001 - rail APIs vary by version.
            logger.warning("Slack write confirmation rail registration failed: %s", exc)
            return False
        self._slack_write_rail = rail
        return True

    def _get_slack_post_request_metadata(self) -> dict[str, Any]:
        return self._slack_context_metadata(slack_post_request_metadata)

    def _get_slack_history_request_metadata(self) -> dict[str, Any]:
        return self._slack_context_metadata(_filter_slack_history_request_metadata)

    def _get_slack_history_session_id(self) -> str:
        """Return the request's session for Slack file downloads."""
        return str(self._runtime_cron_tool_context.session_id or "").strip()

    def _get_slack_reaction_request_metadata(self) -> dict[str, Any]:
        """Read reaction metadata only for Slack request shapes."""
        return self._trusted_slack_metadata(slack_reaction_request_metadata)
