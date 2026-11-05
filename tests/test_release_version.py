"""Keep distributed version files aligned with the package version."""

import subprocess
import sys
from pathlib import Path


def test_version_files_match_pyproject():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/sync_version.py"), "--check"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
