"""Belle's agent YAML, and the one the harness serves: the same, with a second model, an expert and a subagent,
and a default model that takes images and PDFs.

The integration checks run this file to write the harness's YAML into the
volume that replaces the image's agent directory. An operator who declares
models or experts does the same with a YAML of their own.

    python tests/docker_agent/agent_yaml.py OUT_FILE
"""

import copy
import sys
from pathlib import Path
from typing import Any

import yaml

SHIPPED = Path(__file__).resolve().parents[2] / "src/belle_agent/integrations/docker_agent/agent/belle.yaml"
#: The second model of the harness's YAML. The stand-in model receives this name.
SECOND_MODEL = "second"
#: The expert of the harness's YAML, and the instruction that tells its turns apart.
EXPERT = "translator"
EXPERT_INSTRUCTION = "You are the translator expert."
#: The subagent of the harness's YAML, which the entry agent can hand a task to.
SUBAGENT = "helper"


def shipped() -> dict[str, Any]:
    return yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))


def harness() -> dict[str, Any]:
    """The shipped YAML, with a second model of the same provider, an expert the entry agent routes to, and a
    subagent it can hand a task to. Both relay Belle's hooks as the entry agent does."""
    config = shipped()
    config["models"][SECOND_MODEL] = {**config["models"]["default"], "model": SECOND_MODEL}
    # The stand-in takes any content. The second model is left to read text only.
    config["models"]["default"]["capabilities"] = {"image": True, "pdf": True}
    root = config["agents"]["root"]
    for name, description, instruction in (
        (EXPERT, "Translator", EXPERT_INSTRUCTION),
        (SUBAGENT, "Helper", "You help."),
    ):
        config["agents"][name] = {**copy.deepcopy(root), "description": description, "instruction": instruction}
    root["sub_agents"] = [SUBAGENT]
    root["routing"] = {"allowed_agents": [EXPERT]}
    root["hooks"]["before_agent_run"] = copy.deepcopy(root["hooks"]["user_prompt_submit"])
    return config


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(yaml.safe_dump(harness(), sort_keys=False), encoding="utf-8")
