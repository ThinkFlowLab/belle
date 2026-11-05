"""Shared host helpers for contrib/evals probes.

Probes import this from ``contrib/evals/<suite>/`` on the host, or from
``/common`` inside the model container (``run_container`` mounts this directory).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path

ENV_KEYS = ("API_BASE", "API_KEY")
DECISION_ENV_KEYS = ("DECISION_API_BASE", "DECISION_API_KEY")
COMMON_DIR = Path(__file__).resolve().parent
# Prompt wording for message_decisions KIND_* values.
KIND_LABELS = {
    "one_to_one_dm": "direct message with one person",
    "group_dm": "group direct message",
    "public_channel": "public channel",
    "private_channel": "private channel",
}


class RequestPacer:
    def __init__(self, max_rpm: int):
        self.interval = 60 / max_rpm
        self.next_start = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_start)
            self.next_start = start + self.interval
        if start > now:
            time.sleep(start - now)


def load_env_file(path: Path, keys: tuple[str, ...] = ENV_KEYS) -> dict[str, str]:
    """Read only the model variables needed by a probe."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() in keys:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def probe_config(env_path: Path | None, keys: tuple[str, ...]) -> dict[str, str]:
    """Merge an optional env file with matching process environment variables."""
    config = load_env_file(env_path, keys) if env_path and env_path.is_file() else {}
    config.update({key: os.environ[key] for key in keys if os.environ.get(key)})
    return config


def env_file(config: dict[str, str], keys: tuple[str, ...], *, prefix: str = "eval-") -> Path:
    if any("\n" in config.get(key, "") or "\r" in config.get(key, "") for key in keys):
        raise ValueError("environment values must not contain newlines")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=prefix, delete=False) as handle:
        path = Path(handle.name)
        os.chmod(path, 0o600)
        for key in keys:
            if config.get(key):
                handle.write(f"{key}={config[key]}\n")
    return path


def json_file(value: object, *, prefix: str) -> Path:
    """Write ``value`` to a temporary JSON file that the model container can read."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=prefix, delete=False) as handle:
        path = Path(handle.name)
        os.chmod(path, 0o644)
        json.dump(value, handle, ensure_ascii=False)
    return path


def advisory_file(
    cases: list[dict[str, object]],
    advice: str = "synthetic_good",
    *,
    prefix: str,
    build: Callable[[list[dict[str, object]], str], dict[str, str]],
) -> Path:
    return json_file(build(cases, advice) if advice != "none" else {}, prefix=prefix)


def check_cards(path: Path, names: Iterable[str]) -> None:
    """Refuse a ``--cards`` file that lacks a usable card for each named tool."""
    try:
        cards = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"--cards {path}: expected a JSON list of tool cards ({exc})") from exc
    by_name = {card.get("name"): card for card in cards if isinstance(card, dict)} if isinstance(cards, list) else {}
    for name in names:
        card = by_name.get(name)
        usable = (isinstance(card, dict) and isinstance(card.get("description"), str)
                  and isinstance(card.get("input_params"), dict))
        if not usable:
            raise ValueError(f"--cards {path}: needs a card named {name!r} with a description string "
                             "and an input_params object")


def run_container(
    image: str,
    config: dict[str, str],
    keys: tuple[str, ...],
    mounts: list[tuple[Path, str]],
    args: list[str],
    extra_env: tuple[str, ...] = (),
) -> int:
    env_path = env_file(config, keys)
    command = [
        "podman", "run", "--rm", "--pull=never", "--network=slirp4netns",
        "--read-only", "--user=65534:65534", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--env-file", str(env_path),
    ]
    for value in extra_env:
        command.extend(("--env", value))
    for source, target in [(COMMON_DIR, "/common"), *mounts]:
        command.extend(("-v", f"{source.resolve()}:{target}:ro,Z"))
    command.extend(("--entrypoint", "python", image, "/probe.py", *args))
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError:
        print("ERROR: podman is not installed")
        return 2
    finally:
        env_path.unlink(missing_ok=True)


def load_chat_models(path: Path) -> list[str]:
    models = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not models or any(not isinstance(model, dict) or set(model) != {"model_name"}
                         or not isinstance(model["model_name"], str) or not model["model_name"].strip()
                         for model in models):
        raise ValueError("each chat model needs a nonempty model_name")
    names = [model["model_name"] for model in models]
    if len(names) != len(set(names)):
        raise ValueError("chat model names must be unique")
    return names


def load_decision_models(path: Path) -> list[dict[str, str]]:
    from belle_agent.typed_decision.compose import ADAPTERS

    models = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not models:
        raise ValueError("decision model list must not be empty")
    for model in models:
        if not isinstance(model, dict) or set(model) != {"model_name", "api"}:
            raise ValueError("each decision model needs model_name and api")
        if not isinstance(model["model_name"], str) or not model["model_name"].strip():
            raise ValueError("decision model_name must be a nonempty string")
        if not isinstance(model["api"], str) or model["api"] not in ADAPTERS:
            raise ValueError(f"unsupported decision api: {model['api']!r}")
    names = [model["model_name"] for model in models]
    if len(names) != len(set(names)):
        raise ValueError("decision model names must be unique")
    return models


def decision_client_from_env(model: dict[str, str]):
    from belle_agent.typed_decision import build_client

    return build_client({"models": {"decision": {"default": "probe", "entries": [{
        "id": "probe", "api": model["api"], "api_base": os.environ["DECISION_API_BASE"],
        "api_key": os.environ["DECISION_API_KEY"], "model_name": model["model_name"],
    }]}}})


def provider_failure(
    case: dict[str, object], error: str, *, expected_field: str = "suite",
) -> dict[str, object]:
    return {
        "case": case["id"], "suite": case["suite"], "expected": case[expected_field],
        "pass": False, "failure_kind": "provider_error", "impact": "unscored", "severity": None,
        "error": error,
    }


def suite_summary(results: list[dict[str, object]]) -> dict[str, object]:
    failures: Counter[str] = Counter()
    impacts: Counter[str] = Counter()
    severities: Counter[str] = Counter()
    for result in results:
        if not result["pass"]:
            failures[str(result["failure_kind"])] += 1
            impacts[str(result["impact"])] += 1
            if result["severity"] is not None:
                severities[str(result["severity"])] += 1
    return {
        "total": len(results), "passed": sum(bool(result["pass"]) for result in results),
        "failure_kinds": dict(failures), "impacts": dict(impacts), "severities": dict(severities),
    }
