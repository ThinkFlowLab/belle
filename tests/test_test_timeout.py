"""OCB_TEST_TIMEOUT fails a test that hangs, and the fixtures of the test still tear down."""

import os
import subprocess
import sys
import time
from pathlib import Path


def _run(fixture: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"tests/fixtures/timeout/{fixture}"],
        cwd=Path(__file__).parent.parent, capture_output=True, text=True, timeout=60,
        env={**os.environ, "OCB_TEST_TIMEOUT": "0.5", **env},
    )


def test_a_timed_out_async_test_still_runs_its_fixture_teardown(tmp_path):
    teardown = tmp_path / "teardown"
    run = _run("hang.py", OCB_PROBE_TEARDOWN=str(teardown))

    assert run.returncode == 1, run.stdout + run.stderr
    assert "took longer than 0.5s (OCB_TEST_TIMEOUT)" in run.stdout
    assert teardown.read_text() == "closed"


def test_an_async_test_that_blocks_its_event_loop_still_fails():
    started = time.monotonic()
    run = _run("block.py")

    assert run.returncode == 1, run.stdout + run.stderr
    assert "took longer than 0.5s (OCB_TEST_TIMEOUT)" in run.stdout
    assert time.monotonic() - started < 20
