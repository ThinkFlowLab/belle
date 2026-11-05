"""Belle's example configuration on the loopback platform.

The backend sections are the example's, unchanged. The loopback platform
replaces the example's platforms, and one scope makes a channel reply optional.
It writes the configuration that the launcher image reads beside either
harness.

    python tests/loopback_config.py OUT_FILE
"""

import sys
from pathlib import Path

import yaml

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/config/belle-config.yaml"
LOOPBACK = {
    "platforms": {"loopback": {"enabled": True, "control": "${BELLE_LOOPBACK_CONTROL}"}},
    "scopes": [
        {"match": {"channel": "loopback", "chat_type": "channel"}, "delivery": {"mode": ["all"], "reply": "optional"}}
    ],
}


def loopback_config() -> dict:
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    return {key: value for key, value in example.items() if key not in ("platforms", "scopes")} | LOOPBACK


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(yaml.safe_dump(loopback_config(), sort_keys=False), encoding="utf-8")
