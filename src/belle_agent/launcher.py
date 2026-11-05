"""Start Belle from one configuration file: its platforms, a backend, and the host between them.

    python -m belle_agent.launcher --config belle.yaml --state-dir /var/lib/belle

This module is the composition root. It is the one module outside
``belle_agent.integrations`` that names an integration, and nothing imports
it: the core stays free of every backend, and a process that wants one
starts here. Each platform section that is on adds its platform, as
``belle_agent.platforms.base.enabled_platforms`` reads them. The ``backend`` key
selects the backend the same way: a package under ``belle_agent.integrations``
that exposes ``create_backend(config, state_dir)``. A backend reads its secrets
from the environment, and keeps what it must remember across restarts in
``state_dir``.
"""

import argparse
import asyncio
import importlib
import importlib.util
import logging
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from belle_agent.backend import Backend, BackendError
from belle_agent.config import BelleConfigError, expand_env, load_config
from belle_agent.host import host_from_config
from belle_agent.paths import resolve_paths, selected_config_file
from belle_agent.second_brain.backend import SecondBrainBackend
from belle_agent.second_brain.paths import second_brain_config

# Named, not ``__name__``: this module is run as ``__main__``.
logger = logging.getLogger("belle_agent.launcher")

def create_backend(config: Mapping[str, Any], state_dir: Path) -> Backend:
    """Construct the backend the ``backend`` setting names. It is not started."""
    name = expand_env(str(config.get("backend") or ""))
    if not name.isidentifier():
        raise BelleConfigError("Belle config backend must name a backend, such as opencode")
    module_name = f"belle_agent.integrations.{name}"
    factory = None
    try:
        if importlib.util.find_spec(module_name) is not None:
            factory = getattr(importlib.import_module(module_name), "create_backend", None)
    except ImportError as exc:
        raise BelleConfigError(f"Belle config backend={name} cannot be loaded: {exc}") from exc
    if factory is None:
        raise BelleConfigError(f"Belle config backend={name} names no backend this launcher can start")
    return factory(config, state_dir)


def second_brain_backend(config: Mapping[str, Any]) -> SecondBrainBackend | None:
    """Load what the second brain reads, summarizes and indexes with, where the configuration enables it."""
    if not second_brain_config(dict(config)).get("enabled"):
        return None
    try:
        from belle_agent.second_brain.default_backend import SECOND_BRAIN_BACKEND
    except ImportError as exc:
        raise BelleConfigError(f"second brain backend cannot be loaded: {exc}") from exc
    return SECOND_BRAIN_BACKEND


async def serve(config: dict[str, Any], state_dir: Path) -> int:
    """Run a host until the process is told to stop, or every platform has stopped for good. Then stop it.

    Return the exit code: 0 when told to stop, 1 when no platform is left to serve.
    """
    host = host_from_config(config, create_backend(config, state_dir), state_dir=state_dir, second_brain=second_brain_backend(config))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, stop.set)
    starting = asyncio.ensure_future(host.start())
    stopping = asyncio.ensure_future(stop.wait())
    down: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
    code = 0
    try:
        # A start can wait on the backend for minutes. Told to stop meanwhile, the process leaves the wait.
        await asyncio.wait({starting, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if starting.done():
            starting.result()
            platforms = ",".join(host.platforms)
            logger.info("belle is serving: platform=%s backend=%s", platforms, expand_env(str(config["backend"])))
            down = asyncio.ensure_future(host.wait_down())
            await asyncio.wait({stopping, down}, return_when=asyncio.FIRST_COMPLETED)
        if down.done() and not stopping.done():
            reasons = "; ".join(f"{name}: {reason}" for name, reason in down.result().items())
            logger.critical("belle stops: every platform has stopped for good, so the process exits with 1. %s", reasons)
            code = 1
        logger.info("belle is stopping")
    finally:
        for waiting in (starting, stopping, down):
            waiting.cancel()
        await asyncio.gather(starting, stopping, down, return_exceptions=True)
        # Stop admitting, end the running turns, unmount the tools and close, whether or not the start completed.
        await host.stop()
    logger.info("belle stopped")
    return code


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m belle_agent.launcher", description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, help="the Belle configuration file (default: BELLE_AGENT_CONFIG_FILE)")
    parser.add_argument("--state-dir", type=Path, help="where Belle keeps its state (default: BELLE_AGENT_STATE_DIR)")
    options = parser.parse_args(arguments)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    # One line per wire call, at a level that would drown the host's own lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        path = options.config or selected_config_file()
        if path is None:
            raise BelleConfigError("no configuration file: name one with --config")
        return asyncio.run(serve(load_config(path), options.state_dir or resolve_paths().state_dir))
    except (BelleConfigError, BackendError, OSError) as exc:
        # A configuration, a backend or a listener that cannot serve. The message names which and why.
        logger.critical("belle cannot start: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
