"""Choose which scheduler owns a host's schedule tools."""

from belle_agent.backend import BackendCapabilities
from belle_agent.config import BelleConfigError


def resolve_engine(requested: str, backend: BackendCapabilities) -> str:
    """Return belle, host, or disabled from the configured mode and backend declaration."""
    if requested == "disabled":
        return "disabled"
    if requested == "auto":
        return "host" if backend.scheduler else "belle"
    if requested == "host":
        if backend.scheduler:
            return "host"
        raise BelleConfigError(f"backend {backend.backend} has no scheduler that Belle can use")
    if requested == "belle":
        if not backend.scheduler:
            return "belle"
        raise BelleConfigError(
            f"backend {backend.backend} keeps its own scheduler and tools. "
            "scheduling.engine: belle is not available with it"
        )
    raise BelleConfigError("Belle config scheduling.engine must be auto, belle, host or disabled")
