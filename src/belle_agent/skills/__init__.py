"""Packaged Belle skills, independent of any agent runtime."""

from collections.abc import Iterable, Mapping
from importlib.resources import files
from typing import Any

import yaml


def packaged_skill_text(name: str) -> str | None:
    """The ``SKILL.md`` text of the packaged skill ``name``, or ``None`` when Belle packages no skill of that name."""
    for entry in files(__name__).iterdir():
        if entry.name == name and (entry / "SKILL.md").is_file():
            return (entry / "SKILL.md").read_text(encoding="utf-8")
    return None


def packaged_skills() -> list[Mapping[str, Any]]:
    """List the packaged skills as a backend's catalog lists skills: name, description and ``installed``."""
    skills: list[Mapping[str, Any]] = []
    for entry in sorted(files(__name__).iterdir(), key=lambda entry: entry.name):
        if (entry / "SKILL.md").is_file():
            front = (entry / "SKILL.md").read_text(encoding="utf-8").split("---", 2)
            meta = yaml.safe_load(front[1]) if len(front) == 3 else None
            description = meta.get("description", "") if isinstance(meta, dict) else ""
            skills.append({"name": entry.name, "description": str(description).strip(), "installed": True})
    return skills


def required_skills_instruction(names: Iterable[str]) -> str:
    """The text of each packaged skill in ``names``, as one instruction to the model. Other names are left out.

    Empty when Belle packages none of them.
    """
    texts = [(name, text) for name in names if (text := packaged_skill_text(name)) is not None]
    if not texts:
        return ""
    return "\n\n".join(
        [
            (
                "# Required Skills\nThe `SKILL.md` text of each required skill is below."
                " Follow each instruction on this turn according to its stated conditions."
            ),
            *(f"## Required Skill: {name}\n\n{text}" for name, text in texts),
        ]
    )


def command_help_instruction(prefix: str) -> str:
    """Give the command-help skill the exact prefix of this platform installation."""
    if not prefix:
        return ""
    return f"The Belle help command in this conversation is `{prefix} help`."
