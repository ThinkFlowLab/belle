"""The second brain as a conversation host runs it.

A host serves the three tools, recalls documents for a turn and ingests what
a message carries, all in its own process. The settings are the ones under
``second_brain`` that every host reads. What the corpus needs from outside,
a model, a parser and an index, comes through one ``SecondBrainBackend``.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from belle_agent.backend import read_data_uri
from belle_agent.platforms.base import MARK_INGEST_DECLINED, MARK_INGEST_EMPTY, MARK_INGEST_FAILED, MARK_INGESTED
from belle_agent.second_brain.backend import SecondBrainBackend
from belle_agent.second_brain.failures import describe
from belle_agent.second_brain.index import get_second_brain_manager
from belle_agent.second_brain.ingest import STATUS_DUPLICATE, STATUS_FILTERED, STATUS_INGESTED, SecondBrainIngestService
from belle_agent.second_brain.links import extract_urls
from belle_agent.second_brain.paths import (
    PUBLIC_CHANNELS,
    config_section,
    parse_ingest_channels,
    resolve_second_brain_dir,
    second_brain_config,
)
from belle_agent.second_brain.recall import SYSTEM_INSTRUCTIONS, SecondBrainRecall
from belle_agent.second_brain.tools import SecondBrainToolContext, second_brain_actions, second_brain_tool_definitions

logger = logging.getLogger(__name__)

STATUS_FAILED = "failed"

NOBODY_LOOKED = (
    "Nobody looked: the second brain is enabled here, but this deployment has no second brain"
    " backend to search, read or list with. This says nothing about what the corpus holds."
    " Do not retry."
)


async def _nobody_looked(**arguments: Any) -> str:
    return NOBODY_LOOKED


class SecondBrainToolkit:
    """The three corpus tools, in the shape a tool host serves."""

    def __init__(self, context: SecondBrainToolContext) -> None:
        # With no backend, an answer read from the corpus directory would pass for the state of a
        # corpus nothing here can fill or search.
        actions = second_brain_actions(context) if context.backend is not None else (_nobody_looked,) * 3
        self.brain_search, self.brain_read, self.brain_list = actions

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return second_brain_tool_definitions()


class SecondBrain:
    """One corpus, with the backend that reads, summarizes and indexes for it."""

    def __init__(self, config: Mapping[str, Any], backend: SecondBrainBackend | None) -> None:
        self._config = dict(config)
        self._backend = backend
        self._root = resolve_second_brain_dir(self._config)
        section = second_brain_config(self._config)
        retrieval = config_section(section, "retrieval")
        self._max_total_bytes = int(retrieval.get("max_total_bytes") or 10240)
        self._context = SecondBrainToolContext(
            corpus_dir=self._root,
            top_k=int(retrieval.get("top_k") or 5),
            min_score=float(retrieval.get("min_score") or 0.35),
            config=self._config,
            backend=backend,
        )
        self.toolkit = SecondBrainToolkit(self._context)
        self._service = SecondBrainIngestService(self._config, corpus_dir=self._root, backend=backend)
        self._all, self._public, self._channels = parse_ingest_channels(config_section(section, "ingest").get("channels"))

    @property
    def available(self) -> bool:
        """Whether a backend is loaded. Without one nothing is recalled and nothing is ingested."""
        return self._backend is not None

    @property
    def instructions(self) -> str:
        """What a turn is told about the corpus, its tools and the fence around recalled text."""
        # ponytail: English. The host has no language setting; add one when a deployment needs the other text.
        return SYSTEM_INSTRUCTIONS["en"]

    def warnings(self, *, public_known: bool = False) -> list[str]:
        """Name each setting that does nothing here, for the host's start.

        ``public_known`` says whether the host's platform can tell which conversations are public.
        """
        found = []
        if not self.available:
            found.append(
                "second_brain.enabled is set but no second brain backend is loaded: its tools answer that"
                " nobody looked, nothing is recalled and nothing is ingested"
            )
        if self._public and not public_known:
            found.append(
                f"second_brain.ingest.channels names {PUBLIC_CHANNELS!r}, and no platform on this host says"
                " which conversations are public: that entry selects nothing"
            )
        return found

    async def _index(self) -> Any | None:
        """Return the corpus index, asking the backend again until it has one."""
        if self._context.manager is None and self.available:
            try:
                self._context.manager = await get_second_brain_manager(
                    self._config, corpus_dir=self._root, backend=self._backend
                )
            except Exception as exc:  # noqa: BLE001 - a host starts, and a turn runs, without the corpus index
                logger.warning("[SecondBrain] no corpus index: %s", exc)
        return self._context.manager

    async def start(self) -> None:
        await self._index()

    async def recall(self, query: str) -> str | None:
        """Return the fenced summaries that match one message, or nothing."""
        if not self.available or not query.strip():
            return None
        # ponytail: awaited before the turn starts. An index slow enough to notice needs the
        # prefetch of the OpenJiuwen rail, which lets a model call go without the recall.
        return await SecondBrainRecall(
            corpus_dir=self._root,
            top_k=self._context.top_k,
            min_score=self._context.min_score,
            max_total_bytes=self._max_total_bytes,
            config=self._config,
            backend=self._backend,
            manager=await self._index(),
        ).recall(query)

    def ingests_from(self, conversation: str, *, public: bool = False, encrypted: bool = False) -> bool:
        """Whether ``second_brain.ingest.channels`` selects this conversation. Its platform says whether it is public.

        Only its own id selects an end-to-end ``encrypted`` conversation: ``*`` and ``public`` do not, and
        beside ``*`` no id is kept.
        """
        if not self.available:
            return False
        if encrypted:
            return conversation in self._channels
        return self._all or conversation in self._channels or (self._public and public)

    @staticmethod
    def carries_documents(text: str, files: Sequence[Mapping[str, Any]]) -> bool:
        return bool(files) or bool(extract_urls(text))

    async def ingest(self, text: str, files: Sequence[Mapping[str, Any]], meta: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Ingest each file and each link of one message. Return one result per document, in order."""
        results = []
        for entry in files:
            name = str(entry.get("name") or "document")
            results.append(await self._attempt(name, self._ingest_file(entry, name, meta)))
        for url in extract_urls(text):
            results.append(await self._attempt(url, self._service.ingest_link(url, dict(meta))))
        return results

    async def _ingest_file(self, entry: Mapping[str, Any], name: str, meta: Mapping[str, Any]) -> dict[str, Any]:
        # Nothing is fetched or opened, a local path least of all.
        mime, content = read_data_uri(str(entry.get("uri") or ""))
        return await self._service.ingest_bytes(content, name, mime, dict(meta))

    @staticmethod
    async def _attempt(label: str, ingestion: Any) -> dict[str, Any]:
        try:
            return await ingestion
        except Exception as exc:  # noqa: BLE001 - one document that fails does not stop the others
            logger.warning("[SecondBrain] ingest failed for %s: %s", label, describe(exc))
            return {"status": STATUS_FAILED, "reason": str(exc), "doc_id": "", "title": ""}


def ingest_mark(statuses: Iterable[str]) -> str:
    """Return the mark of one message from the ingest status of each of its documents.

    A document added or already held outweighs a failure, and a failure outweighs a decline. Any
    other status is a failure.
    """
    found = set(statuses)
    if found & {STATUS_INGESTED, STATUS_DUPLICATE}:
        return MARK_INGESTED
    if found - {STATUS_FILTERED}:
        return MARK_INGEST_FAILED
    return MARK_INGEST_DECLINED if found else MARK_INGEST_EMPTY


def second_brain_from_config(config: Mapping[str, Any], backend: SecondBrainBackend | None) -> SecondBrain | None:
    """Build the second brain unless disabled, with whatever backend was loaded."""
    if not second_brain_config(dict(config)).get("enabled"):
        return None
    return SecondBrain(config, backend)


__all__ = [
    "NOBODY_LOOKED",
    "SecondBrain",
    "SecondBrainToolkit",
    "ingest_mark",
    "second_brain_from_config",
]
