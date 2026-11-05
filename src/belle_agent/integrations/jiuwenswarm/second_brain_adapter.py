"""Ingest links and session files through the AgentServer gateway."""

import logging
from pathlib import Path
from typing import Any, ClassVar

from jiuwenswarm.extensions.sdk.gateway_adapter import (
    GatewayAdapter,
    build_error_response,
)
from jiuwenswarm.extensions.sdk.host_types import AgentRequest, AgentResponse

from belle_agent.integrations.jiuwenswarm.second_brain_backend import SECOND_BRAIN_BACKEND
from belle_agent.second_brain.paths import second_brain_config

logger = logging.getLogger(__name__)

_KIND_FILE = "file"
_KIND_LINK = "link"
_KINDS = frozenset({_KIND_FILE, _KIND_LINK})


class SecondBrainAdapter(GatewayAdapter):
    """Ingest one document or link into the second brain."""

    methods: ClassVar[frozenset[str]] = frozenset({"brain.ingest"})
    source: ClassVar[str] = "belle-agent"

    async def handle(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}

        kind = str(params.get("kind") or "").strip().lower()
        if kind not in _KINDS:
            return build_error_response(
                request, f"kind must be one of {sorted(_KINDS)}", code="BAD_REQUEST"
            )

        config = self._load_config()
        if not second_brain_config(config).get("enabled"):
            return build_error_response(
                request,
                "second_brain is not enabled on this AgentServer",
                code="BAD_REQUEST",
            )

        try:
            from belle_agent.second_brain.ingest import (
                SecondBrainIngestService,
            )
        except ImportError as exc:  # pragma: no cover - packaging failure
            return build_error_response(
                request, f"second brain unavailable: {exc}", code="INTERNAL_ERROR"
            )

        service = SecondBrainIngestService(
            config, corpus_dir=self._corpus_dir(config), backend=SECOND_BRAIN_BACKEND
        )
        meta = params.get("slack") if isinstance(params.get("slack"), dict) else {}
        field = "url" if kind == _KIND_LINK else "path"
        source = str(params.get(field) or "").strip()
        if not source:
            return build_error_response(
                request, f"{field} is required for kind={kind}", code="BAD_REQUEST"
            )

        try:
            if kind == _KIND_LINK:
                result = await service.ingest_link(source, meta)
            else:
                resolved = self._resolve_shared_path(source)
                if resolved is None:
                    return build_error_response(
                        request,
                        "path must be a file inside the agent sessions directory",
                        code="BAD_REQUEST",
                    )
                result = await service.ingest_path(
                    resolved, str(params.get("mime_type") or ""), meta
                )
        except Exception as exc:
            logger.exception("[SecondBrainAdapter] ingest failed")
            return build_error_response(
                request, f"ingest failed: {exc}", code="INTERNAL_ERROR"
            )

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=result,
            metadata=request.metadata,
        )

    @staticmethod
    def _corpus_dir(config: dict[str, Any]) -> Path:
        from jiuwenswarm.extensions.sdk.workspace_paths import get_agent_workspace_dir

        from belle_agent.second_brain.paths import resolve_second_brain_dir

        return resolve_second_brain_dir(config, workspace_dir=get_agent_workspace_dir())

    @staticmethod
    def _resolve_shared_path(raw_path: str) -> Path | None:
        """Resolve a file inside the sessions directory, including symlinks."""
        from jiuwenswarm.extensions.sdk.workspace_paths import get_agent_sessions_dir

        try:
            root = Path(get_agent_sessions_dir()).resolve()
            candidate = Path(raw_path).resolve()
        except (OSError, RuntimeError):
            return None
        if candidate.is_file() and candidate.is_relative_to(root):
            return candidate
        return None

    @staticmethod
    def _load_config() -> dict[str, Any]:
        from belle_agent.integrations.jiuwenswarm.config import load_belle_config

        return load_belle_config()


__all__ = ["SecondBrainAdapter"]
