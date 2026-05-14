"""Shared argparse helpers for the cli/* entry points.

Every CLI accepts the same set of universal flags:

* ``--workspace PATH``      -- overrides ``WOWS_WORKSPACE``
* ``--game-dir PATH``       -- overrides ``WOWS_GAME_DIR``
* ``--toolkit-bin PATH``    -- overrides ``WOWS_TOOLKIT_BIN``
* ``--json-events``         -- switch the event printer to one JSON
                                object per line on stdout
* ``--quiet``               -- suppress the human-readable text printer
                                (errors still go to stderr)

Use :func:`add_common_args` to declare them on a parser; use
:func:`resolve_config` after parsing to merge them into a
:class:`PipelineConfig` (loading defaults from env first), and
:func:`build_printer` to pick the appropriate event sink for the
composer call.

Exit codes are codified in :data:`EXIT_OK` / :data:`EXIT_STEP_ERROR` /
:data:`EXIT_CONFIG_ERROR` / :data:`EXIT_UNEXPECTED` so the per-CLI
``main()`` bodies stay uniform.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from ..config import PipelineConfig
from ..types import OnEvent
from ._emit import make_json_printer, make_text_printer

# ----- Exit codes -----------------------------------------------------------

EXIT_OK: int = 0
EXIT_STEP_ERROR: int = 1
EXIT_CONFIG_ERROR: int = 2
EXIT_UNEXPECTED: int = 3


# ----- Argument declarations -------------------------------------------------


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add the universal ``--workspace`` / ``--game-dir`` / ``--toolkit-bin``
    / ``--json-events`` / ``--quiet`` flags to ``parser``.
    """
    g = parser.add_argument_group("config overrides")
    g.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Override WOWS_WORKSPACE (per-ship dirs + libraries/ live here).",
    )
    g.add_argument(
        "--game-dir",
        type=Path,
        default=None,
        help="Override WOWS_GAME_DIR (WoWS Steam install root).",
    )
    g.add_argument(
        "--toolkit-bin",
        type=Path,
        default=None,
        help="Override WOWS_TOOLKIT_BIN (path to wowsunpack executable).",
    )

    o = parser.add_argument_group("output")
    o.add_argument(
        "--json-events",
        action="store_true",
        help="Emit one JSON object per StepEvent on stdout instead of "
             "human-readable text on stderr. Use for subprocess "
             "supervision (webview Vite middleware, CI logs).",
    )
    o.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the text event printer. Errors still go to "
             "stderr. Has no effect when --json-events is set.",
    )


# ----- Config + printer plumbing --------------------------------------------


def resolve_config(args: argparse.Namespace) -> PipelineConfig:
    """Build a :class:`PipelineConfig` honoring the CLI overrides.

    Loads defaults from env via :meth:`PipelineConfig.load`, then
    overlays any explicit ``--workspace`` / ``--game-dir`` /
    ``--toolkit-bin`` values. Returns a fresh frozen config -- safe to
    hand to a composer.
    """
    cfg = PipelineConfig.load()
    overrides: dict = {}
    workspace = getattr(args, "workspace", None)
    if workspace is not None:
        overrides["workspace"] = Path(workspace).expanduser().resolve()
        # cache_dir is derived from workspace; recompute when overridden.
        overrides["cache_dir"] = overrides["workspace"] / ".cache"
    game_dir = getattr(args, "game_dir", None)
    if game_dir is not None:
        overrides["game_dir"] = Path(game_dir).expanduser().resolve()
    toolkit_bin = getattr(args, "toolkit_bin", None)
    if toolkit_bin is not None:
        overrides["toolkit_bin"] = Path(toolkit_bin).expanduser().resolve()
    if not overrides:
        return cfg
    return replace(cfg, **overrides)


def build_printer(args: argparse.Namespace) -> OnEvent | None:
    """Pick the event printer based on ``--json-events`` / ``--quiet``."""
    if getattr(args, "json_events", False):
        return make_json_printer()
    return make_text_printer(quiet=getattr(args, "quiet", False))


__all__ = [
    "add_common_args",
    "resolve_config",
    "build_printer",
    "EXIT_OK",
    "EXIT_STEP_ERROR",
    "EXIT_CONFIG_ERROR",
    "EXIT_UNEXPECTED",
]
