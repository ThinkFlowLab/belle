"""Copy a request while adding one value to its SDK run context."""

from collections.abc import Mapping
from typing import Any


def with_run_context_extra(inputs: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    updated = dict(inputs)
    raw_run = updated.get("run")
    run = dict(raw_run) if isinstance(raw_run, Mapping) else {}
    raw_context = run.get("context")
    context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
    raw_extra = context.get("extra")
    extra = dict(raw_extra) if isinstance(raw_extra, Mapping) else {}
    extra[key] = value
    context["extra"] = extra
    run["context"] = context
    run.setdefault("kind", "normal")
    updated["run"] = run
    return updated
