"""Workspace resolution for the FastAPI backend.

The Node side of the webview had its own resolver in
``webview/src/server/workspace.ts`` — env var > marker walk > home
fallback. The Python pipeline has a different, simpler resolver in
:mod:`wows_model_export.config`: ``$WOWS_WORKSPACE`` env var, falling back
to ``Path.cwd()``. Both agree on the env-var-takes-priority rule, but
the Python side never walks for a marker directory.

We deliberately re-use :class:`wows_model_export.config.PipelineConfig`
rather than duplicating the Node walker. Net result:

* If the user runs ``wows-webview-serve`` from a directory that *is* a
  workspace (or from anywhere with ``$WOWS_WORKSPACE`` set), it just
  works.
* If the user runs it from somewhere else without the env var, the
  config resolves to ``Path.cwd()`` — which won't contain the
  ``libraries/accessories/`` marker, so endpoints will 404 their data
  endpoints with the documented hint. Same behaviour as the Node side
  for the same scenario.

The CLI accepts ``--workspace`` to override the env var; that lands in
the config via :func:`wows_model_export.cli._args.resolve_config` and is
threaded through to :func:`create_app` here.
"""

from __future__ import annotations

from pathlib import Path

from ..config import PipelineConfig


def load_default_config() -> PipelineConfig:
    """Resolve a :class:`PipelineConfig` from the environment.

    Convenience wrapper for callers that don't want to know about the
    config module directly — the FastAPI app factory accepts a config
    by value, so the CLI does the resolution once and hands it in.
    """
    return PipelineConfig.load()


def is_child_of(child: Path, parent: Path) -> bool:
    """Return True when ``child`` is the same as or a descendant of ``parent``.

    Mirrors the Node side's ``isChildOf`` traversal guard in
    ``webview/src/server/workspace.ts``. Both paths are resolved
    against the filesystem first so symlinks land in the right place;
    this matches the Node version's behaviour (it uses ``path.resolve``
    + ``path.relative`` which together collapse ``..`` and follow
    symlinks).
    """
    try:
        child_r = Path(child).resolve()
        parent_r = Path(parent).resolve()
    except (OSError, RuntimeError):
        return False
    if child_r == parent_r:
        return True
    try:
        child_r.relative_to(parent_r)
        return True
    except ValueError:
        return False


__all__ = ["load_default_config", "is_child_of"]
