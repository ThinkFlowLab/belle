"""Configured model catalog values."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypedDict


class TaskModelRow(TypedDict):
    role: str
    id: str
    api: str
    model_name: str | None
    selected: bool


def task_model_rows(config: Mapping[str, Any]) -> list[TaskModelRow]:
    """List Belle-owned model entries without credentials or endpoint URLs."""
    groups = config.get("models") or {}
    rows: list[TaskModelRow] = []
    for role in ("decision", "summarizer", "extractor", "classifier"):
        group = groups.get(role) or {}
        entries = group.get("entries") or []
        default = group.get("default") or (entries[0]["id"] if len(entries) == 1 else "")
        selected = {default}
        if role == "summarizer":
            summarizer = ((config.get("second_brain") or {}).get("ingest") or {}).get("summarizer", {})
            selected = {summarizer.get("model") or default, summarizer.get("outline_model") or ""}
        for entry in entries:
            rows.append({"role": role, "id": entry["id"], "api": entry["api"],
                         "model_name": entry.get("model_name") or None, "selected": entry["id"] in selected})
    return rows


@dataclass(frozen=True)
class ConfiguredModels:
    """Configured model names and resolvable aliases in default order."""

    names: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    alias_pairs: tuple[tuple[str, str], ...] = ()

    @property
    def known(self) -> bool:
        """Whether the model list could be read at all."""
        return bool(self.names)

    @property
    def default(self) -> str:
        """Return the first configured model."""
        return self.names[0] if self.names else ""

    def accepts(self, name: str) -> bool:
        return name in self.names or name in self.aliases

    def describe(self) -> str:
        """The valid choices, for a warning that has to be actionable."""
        if not self.names:
            return "none"
        listed = ", ".join(self.names)
        if self.aliases:
            listed += f" (or the aliases {', '.join(self.aliases)})"
        return listed

    def describe_fallback(self) -> str:
        """Name the model a dropped override leaves the channel on."""
        return (
            f"the default model {self.default}"
            if self.default
            else "whatever model the server defaults to"
        )
