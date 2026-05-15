"""``wows-webview-serve`` — FastAPI backend launcher for the Svelte webview.

Reads the workspace from the standard config chain (``$WOWS_WORKSPACE``
> CWD), builds the FastAPI app, hands it to uvicorn. Argv shape::

    wows-webview-serve [--host 127.0.0.1] [--port 5180]
                       [--workspace PATH] [--reload]

The CLI deliberately does **not** use the shared ``add_common_args`` /
``resolve_config`` flow: this binary doesn't need ``--toolkit-bin`` /
``--game-dir``, doesn't take ``--json-events``, and the rest of the
shared infra would just pull in unused argparse groups. Workspace
resolution is the only config field that matters at startup.

Two supported deployment shapes:

  * **Stand-alone** (default): the Svelte UI is bundled into the wheel
    under ``wows_model_export/_static/webview/`` and served by the same
    FastAPI process at ``/``. After ``pip install`` users get the full
    UI from a single ``wows-webview-serve`` invocation — no Node, no
    second process, no port juggling.
  * **Dev (two-process)**: ``cd webview && npm run dev`` runs Vite for
    HMR alongside this server; Vite proxies ``/api/*`` + ``/repo/*``
    here. The bundled UI is irrelevant in this mode (Vite serves the
    sources directly).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

from ..config import PipelineConfig
from ..errors import ConfigError


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-webview-serve",
        description=(
            "Local web UI for wows-model-export. Serves the bundled "
            "Svelte SPA at / together with the /api/* + /repo/* "
            "endpoints on a single TCP port. In development the Vite "
            "dev server (cd webview && npm run dev) hosts the UI itself "
            "and proxies API/repo calls back here."
        ),
    )
    ap.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Default 127.0.0.1 (localhost-only).",
    )
    ap.add_argument(
        "--port",
        type=int,
        default=5180,
        help="TCP port. Default 5180.",
    )
    ap.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help=(
            "Override WOWS_WORKSPACE (per-ship dirs + libraries/ live "
            "here). Falls back to the env var, then CWD."
        ),
    )
    ap.add_argument(
        "--reload",
        action="store_true",
        help=(
            "Enable uvicorn's auto-reload on source change. Off by "
            "default — only useful when iterating on the server code."
        ),
    )
    ap.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level. Default 'info'.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Resolve config the same way every other CLI does (env-var first,
    # CWD fallback). The --workspace override mirrors the shared
    # `_args.resolve_config` behaviour without dragging in the rest of
    # the common-args plumbing.
    try:
        cfg = PipelineConfig.load()
        if args.workspace is not None:
            workspace = Path(args.workspace).expanduser().resolve()
            cfg = replace(cfg, workspace=workspace, cache_dir=workspace / ".cache")
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # Import uvicorn lazily so just running with ``--help`` doesn't
    # error out on a machine without the webview extra installed.
    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed. Install the webview extra: "
            "`pip install wows-model-export[webview]`.",
            file=sys.stderr,
        )
        return 2

    logging.basicConfig(
        format="[webview-serve] %(message)s",
        level=logging.INFO,
    )
    logging.info(
        "starting wows-webview-serve on http://%s:%d (workspace=%s)",
        args.host,
        args.port,
        cfg.workspace,
    )

    if args.reload:
        # uvicorn's --reload mode requires an import string; it can't
        # pickle a live FastAPI instance. Stash the resolved workspace
        # so the reloaded worker can rebuild the same config from env.
        import os

        os.environ["WOWS_WORKSPACE"] = str(cfg.workspace)
        # Watch the wows_model_export package directory so edits to
        # routes / composers reload the running server. Default
        # ``reload_dirs`` is the CWD, which under ``npm run dev`` is
        # ``webview/`` — that misses every Python file. Compute the
        # package root from this module's own path so the watcher
        # tracks the installed/editable source on disk.
        import wows_model_export

        pkg_root = Path(wows_model_export.__file__).resolve().parent
        uvicorn.run(
            "wows_model_export.server.main:_app_for_reload",
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[str(pkg_root)],
            factory=True,
            log_level=args.log_level,
        )
    else:
        from ..server.main import create_app

        app = create_app(cfg)
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
    return 0


__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
