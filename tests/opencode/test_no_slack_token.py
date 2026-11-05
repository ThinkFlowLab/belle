"""No Slack credential is in the harness: not in the environment, not in an artefact.

The check does not apply to a live Slack run, which passes the tokens of a Slack app to the Belle container only.
"""

import os
import re

import pytest

from belle_agent.platforms.slack.tool_common import _SLACK_TOKEN_RE

# The core pattern covers the `xox?-` families. An app-level token is `xapp-`.
_APP_TOKEN_RE = re.compile(r"\bxapp-[A-Za-z0-9-]+\b", re.IGNORECASE)


def _token_shaped(value: str) -> bool:
    return bool(_SLACK_TOKEN_RE.search(value) or _APP_TOKEN_RE.search(value))


def slack_findings(environ, directories) -> list[str]:
    """Where a Slack variable or a Slack-token-shaped string is. Names and paths only, never a value."""
    found = [
        f"environment variable {name}"
        for name, value in environ.items()
        if "slack" in name.lower() or _token_shaped(value)
    ]
    for directory in directories:
        for folder, _, files in os.walk(directory):
            for name in files:
                path = os.path.join(folder, name)
                with open(path, errors="replace") as handle:
                    if any(_token_shaped(line) for line in handle):
                        found.append(f"file {path}")
    return found


@pytest.mark.skipif("OCB_STATE" not in os.environ, reason="requires the isolated test state")
def test_no_slack_token_in_the_environment_or_an_artefact():
    state = os.environ["OCB_STATE"]
    assert os.path.isdir(state) and os.access(state, os.R_OK | os.X_OK)
    assert slack_findings(os.environ, [state]) == []


def test_the_check_finds_each_breach(tmp_path):
    bot = "xox" + "b-0000000000-synthetic"  # assembled here so no token-shaped string is in the tree
    app = "xap" + "p-1-A000-synthetic"
    (tmp_path / "clean.jsonl").write_text('{"text": "nothing here"}\n')
    assert slack_findings({"PATH": "/usr/bin"}, [tmp_path]) == []
    assert slack_findings({"SLACK_BOT_TOKEN": "synthetic"}, []) == ["environment variable SLACK_BOT_TOKEN"]
    assert slack_findings({"HARMLESS": bot}, []) == ["environment variable HARMLESS"]
    assert slack_findings({"HARMLESS": app}, []) == ["environment variable HARMLESS"]
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "log.jsonl").write_text(f'{{"authorization": "Bearer {bot}"}}\n')
    (tmp_path / "app.txt").write_text(app)
    assert sorted(slack_findings({}, [tmp_path])) == sorted(
        [f"file {tmp_path / 'nested' / 'log.jsonl'}", f"file {tmp_path / 'app.txt'}"]
    )
