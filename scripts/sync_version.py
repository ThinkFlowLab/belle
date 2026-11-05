"""Sync packaged version files with pyproject.toml."""

import argparse
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src/belle_agent/platforms/matrix/helper"
EXTENSIONS = ROOT / "src/belle_agent/integrations/jiuwenswarm/extensions"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if a version file is stale")
    mode.add_argument("--tag", action="store_true", help="print the matching image tag suffix")
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if args.tag:
        print(f"v{version}")
        return 0

    manifests = sorted(EXTENSIONS.glob("*/extension.yaml"))
    if not manifests:
        parser.error("no extension manifests found")
    files = {
        **{path: r"(^version: )\S+(\n|$)" for path in manifests},
        HELPER / "Cargo.toml": r'(^version = ")[^"]+(")',
        HELPER / "Cargo.lock": r'(\[\[package\]\]\nname = "belle-matrix-helper"\nversion = ")[^"]+(")',
        HELPER / "protocol.jsonl": r'("helper_version":\s*")[^"]+(")',
    }
    changes = {}
    for path, pattern in files.items():
        current = path.read_text()
        updated, count = re.subn(pattern, lambda match: f"{match[1]}{version}{match[2]}", current, count=1, flags=re.MULTILINE)
        if count != 1:
            parser.error(f"version field missing in {path.relative_to(ROOT)}")
        if updated != current:
            changes[path] = updated
    if changes:
        print("\n".join(str(path.relative_to(ROOT)) for path in changes))
    if args.check:
        return int(bool(changes))
    for path, updated in changes.items():
        path.write_text(updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
