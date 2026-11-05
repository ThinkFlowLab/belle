"""Launch the optional terminal client."""

import argparse
from pathlib import Path

from belle_agent.config import BelleConfigError
from belle_agent.paths import resolve_paths


def main() -> None:
    """Run the overview as a separate process."""
    parser = argparse.ArgumentParser(description="Read Belle status through a local Unix socket.")
    parser.add_argument("--socket", type=Path, help="status socket (default: Belle runtime directory/control.sock)")
    parser.add_argument("--theme", choices=("dark", "light"), default="dark")
    args = parser.parse_args()
    socket = args.socket
    if socket is None:
        try:
            runtime = resolve_paths().runtime_dir
        except BelleConfigError as exc:
            parser.error(str(exc))
        if runtime is None:
            parser.error("--socket PATH or BELLE_AGENT_RUNTIME_DIR must name the status socket location")
        socket = runtime / "control.sock"
    try:
        from belle_agent.tui.app import BelleApp
    except ModuleNotFoundError as exc:
        if exc.name != "textual":
            raise
        parser.error("Textual is missing. Install belle-agent[tui] to run belle-tui.")
    BelleApp(socket, theme=args.theme).run()


if __name__ == "__main__":
    main()
