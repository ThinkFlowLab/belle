"""Copy license texts for the Matrix helper's locked Rust packages."""

import json
import shutil
import subprocess
from pathlib import Path

metadata = json.loads(
    subprocess.check_output(["cargo", "metadata", "--locked", "--format-version", "1", "--filter-platform", "host-tuple"])
)
resolved = {node["id"] for node in metadata["resolve"]["nodes"]}
packages = (package for package in metadata["packages"] if package["id"] in resolved)
destination = Path("/cargo-licenses")
destination.mkdir(exist_ok=True)
with (destination / "THIRD_PARTY.tsv").open("w") as inventory:
    for package in sorted(packages, key=lambda item: (item["name"], item["version"])):
        if not (package["source"] or "").startswith("registry+"):
            continue
        inventory.write(
            f'{package["name"]}\t{package["version"]}\t{package.get("license") or "unknown"}'
            f'\t{package.get("repository") or ""}\n'
        )
        source = Path(package["manifest_path"]).parent
        target = destination / f'{package["name"]}-{package["version"]}'
        for path in source.iterdir():
            if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT")):
                target.mkdir(exist_ok=True)
                shutil.copy2(path, target / path.name)
        if not target.exists():
            target.mkdir()
            shutil.copy2(source / "Cargo.toml", target / "Cargo.toml")
            for readme in source.glob("README*"):
                if readme.is_file():
                    shutil.copy2(readme, target / readme.name)
