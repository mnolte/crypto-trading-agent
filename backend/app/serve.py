"""Run the app with placement from EnvConfig.

Uvicorn's own ``--env-file`` would export every ``.env`` line into the process
environment, including secret fallbacks. Starting through this module keeps the
project's existing secret-loading rules: ``TA_HOST`` and ``TA_PORT`` are read by
pydantic-settings, while API keys stay in the keyring or are read one at a time.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

import uvicorn

from app.config import get_env


@dataclass(frozen=True)
class ServeOptions:
    host: str
    port: int
    reload: bool
    timeout_graceful_shutdown: int = 45


def serve_options(*, reload: bool) -> ServeOptions:
    env = get_env()
    return ServeOptions(host=env.ta_host, port=env.ta_port, reload=reload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the Trading Agent app")
    parser.add_argument("--reload", action="store_true", help="reload on Python file changes")
    args = parser.parse_args(argv)
    options = serve_options(reload=args.reload)
    uvicorn.run(
        "app.main:app",
        host=options.host,
        port=options.port,
        reload=options.reload,
        timeout_graceful_shutdown=options.timeout_graceful_shutdown,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
