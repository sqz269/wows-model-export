"""Shared argparse helpers for the cli/* entry points.

Every CLI accepts the same set of universal flags:

* ``--workspace PATH``      -- overrides ``WOWS_WORKSPACE``
* ``--game-dir PATH``       -- overrides ``WOWS_GAME_DIR``
* ``--toolkit-bin PATH``    -- overrides ``WOWS_TOOLKIT_BIN``
* ``--archive-dir PATH``    -- overrides ``WOWS_ARCHIVE_DIR`` (the build archive)
* ``--build N``             -- read game build ``N`` (from the archive, else
                               the install) and write into the workspace's
                               ``builds/<N>/``; the install's current build
                               is the plain workspace
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
import sys
from dataclasses import replace
from pathlib import Path

from ..config import PipelineConfig, set_process_default
from ..types import OnEvent
from ._emit import make_json_printer, make_text_printer

# ----- Exit codes -----------------------------------------------------------

EXIT_OK: int = 0
EXIT_STEP_ERROR: int = 1
EXIT_CONFIG_ERROR: int = 2
EXIT_UNEXPECTED: int = 3


# ----- Argument declarations -------------------------------------------------


def add_common_args(parser: argparse.ArgumentParser, *, build_option: bool = True) -> None:
    """Add the universal ``--workspace`` / ``--game-dir`` / ``--toolkit-bin``
    / ``--archive-dir`` / ``--build`` / ``--json-events`` / ``--quiet``
    flags to ``parser``. ``build_option=False`` leaves ``--build`` out, for a
    parser that gives it its own meaning or a command that works on the
    current build only (pair it with ``resolve_config(args,
    pin_build=False)`` so ``WOWS_BUILD`` is ignored too).
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
    g.add_argument(
        "--archive-dir",
        type=Path,
        default=None,
        help="Override WOWS_ARCHIVE_DIR (the append-only build archive, laid "
             "out like the install: bin/<build>/idx + res_packages/).",
    )
    if build_option:
        g.add_argument(
            "--build",
            type=int,
            default=None,
            metavar="BUILD",
            help="Read this game build instead of the install's current one: "
                 "from the archive, else from the install while it still has "
                 "the build's idx files. Output goes to <workspace>/builds/"
                 "<BUILD>/ (a workspace of its own). The current build is the "
                 "plain workspace.",
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


def add_build_args(parser: argparse.ArgumentParser) -> None:
    """Only the game-build flags (``--archive-dir``, ``--build``), for a CLI
    that has its own argument set; :func:`resolve_config` reads them."""
    g = parser.add_argument_group("game build")
    g.add_argument(
        "--archive-dir",
        type=Path,
        default=None,
        help="Override WOWS_ARCHIVE_DIR (the append-only build archive).",
    )
    g.add_argument(
        "--build",
        type=int,
        default=None,
        metavar="BUILD",
        help="Read this game build (archive, else install) and write into "
             "<workspace>/builds/<BUILD>/; the current build is the plain workspace.",
    )


# ----- Config + printer plumbing --------------------------------------------


def resolve_config(args: argparse.Namespace, *, pin_build: bool = True) -> PipelineConfig:
    """Build a :class:`PipelineConfig` honoring the CLI overrides.

    Loads defaults from env via :meth:`PipelineConfig.load`, then
    overlays any explicit ``--workspace`` / ``--game-dir`` /
    ``--toolkit-bin`` values. Returns a fresh frozen config -- safe to
    hand to a composer.

    With ``--build`` (or ``WOWS_BUILD``) the config is pinned to that
    build (:meth:`PipelineConfig.for_build`), the build is checked to be
    readable, and the pinned config becomes the process default, so every
    ``PipelineConfig.load()`` in this process returns it.

    ``pin_build=False`` is for commands that only work on the install's
    current build (publishing, the loose-file decal mirror): ``--build``
    is not read and ``WOWS_BUILD`` is ignored with a note, the unpinned
    config becoming the process default so no fallback reads it either.
    """
    cfg = PipelineConfig.load()
    env_build = cfg.build
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
    archive_dir = getattr(args, "archive_dir", None)
    if archive_dir is not None:
        overrides["archive_dir"] = Path(archive_dir).expanduser().resolve()
    if pin_build:
        build = getattr(args, "build", None)
        if build is None:
            build = cfg.build  # WOWS_BUILD
    else:
        build = None
    if overrides:
        cfg = replace(cfg.for_build(None), **overrides)
    cfg = cfg.for_build(build)
    if not pin_build and env_build is not None:
        print(f"note: WOWS_BUILD={env_build} ignored: this command works on the "
              "install's current build only", file=sys.stderr)
        set_process_default(cfg)
    if cfg.build is not None:
        from ..toolkit import build as _build

        _build.require_source(cfg.build, cfg)
        set_process_default(cfg)
    return cfg


def build_printer(args: argparse.Namespace) -> OnEvent | None:
    """Pick the event printer based on ``--json-events`` / ``--quiet``."""
    if getattr(args, "json_events", False):
        return make_json_printer()
    return make_text_printer(quiet=getattr(args, "quiet", False))


__all__ = [
    "add_build_args",
    "add_common_args",
    "resolve_config",
    "build_printer",
    "EXIT_OK",
    "EXIT_STEP_ERROR",
    "EXIT_CONFIG_ERROR",
    "EXIT_UNEXPECTED",
]
