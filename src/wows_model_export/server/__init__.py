"""FastAPI backend for the webview.

Replaces the Vite/Node ``dev_api.ts`` middleware. Same wire contract — the
Svelte client treats both backends identically — but with the
``wows-webview-serve`` CLI it can be run as an independent process from
either ``npm run dev`` (Vite proxies ``/api/*`` + ``/repo/*`` to here) or
``pip install``-only deployments (no Node at runtime).

Layout:

    main.py            — :func:`create_app` factory + ``wows-webview-serve``
                         CLI entry helper
    workspace.py       — thin re-export of :func:`wows_model_export.config`
                         resolution (env-var precedence)
    jobs.py            — in-memory job runner (Stage 3:
                         ``ThreadPoolExecutor`` + composer ``Future``,
                         no longer ``Popen``-based)
    routes/repo.py     — ``GET /repo/<path>`` (workspace static file
                         service, traversal-guarded)
    routes/ships.py    — ``GET /api/ships`` (per-ship summaries)
    routes/library.py  — ``GET /api/library`` (accessory library index)
    routes/gameparams.py — ``GET /api/gameparams/status``
    routes/extract.py  — ``/api/extract/*`` (snapshot + run + skin + jobs)

Why FastAPI rather than the existing Node middleware:

* Workspace + cache + toolkit resolution already lives in
  :mod:`wows_model_export.config`; the Node side duplicates it.
* Job runner ports cleanly: in-memory dict + ``threading.Lock`` matches
  the Node ``Map`` + closure-captured state.
* No new runtime dependency on the user — the Python pipeline is
  already required to produce the artifacts this server serves.

See ``webview/INTEGRATION_PLAN.md`` for the full migration plan; this
module implements Path A Stage 1 (structural port only, no behaviour
changes — opaque-stdout jobs, HTTP polling).
"""

from __future__ import annotations

__all__ = ["create_app"]


def __getattr__(name: str):  # pragma: no cover - import-only convenience
    # Lazy import so the package is importable on a machine that doesn't
    # have fastapi/uvicorn installed (the webview extra is opt-in).
    if name == "create_app":
        from .main import create_app

        return create_app
    raise AttributeError(name)
