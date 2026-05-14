"""Winding-audit + flip endpoints for the accessory library.

Lifts the legacy I:-side webview's three routes onto the FastAPI backend:

  ``GET  /api/winding-audit``       — static read of ``winding_audit.json``
  ``POST /api/auto-flip-winding``   — re-score the library + apply the
                                       high-confidence inversions
                                       (in-process, ~3 s for 1k assets)
  ``POST /api/flip-winding``        — flip a single GLB on disk and toggle
                                       its entry in ``flip_overrides.json``

Both flip routes run in-process rather than spawning a subprocess. The
legacy webview shelled out to ``build_accessory_library.py --audit-only
--auto-flip-winding``, but the new CLI couples the audit to the full
library build pipeline — only the composer's ``_audit_winding`` helper
does the focused rescore-and-apply pass. Calling it directly skips the
full build (no need to walk ships/ + re-export GLBs) and avoids the
subprocess startup cost.

Per-asset flip uses :func:`wows_model_export._glb.flip_winding` the same
way the bulk audit does. Same on-disk effect either way: rewrite the
GLB with reversed triangle winding + atomically toggle the override file.

Winding reversal is involutive, so "asset is in overrides" maps to
"asset's winding has been flipped vs. its original toolkit-emitted
output". The library composer's :func:`_reapply_flip_overrides` reads
``flip_overrides.json`` on rebuild so user flips survive a re-export.

Audit doc shape (mirrors ``compose.accessory_library._audit_winding``)::

    {
      "schema":       "wows_winding_audit/v1",
      "generated_at": "<iso8601>",
      "asset_count":  int,
      "summary":      {"flip": int, "applied": int, "ambiguous": int,
                       "manual": int, "keep": int, "unscored": int},
      "assets": [
        {"path":         "<glb-relative>",
         "verdict":      "flip"|"keep"|"ambiguous"|"manual"|"unscored",
         "correctness":  0..1,
         "signal_b":     0..1,
         "signal_a":     0..1,
         "n_prim":       int,
         "in_overrides": bool},
        ...
      ]
    }
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse, Response

from ... import _glb
from ...compose.accessory_library import (
    FLIP_OVERRIDES_FILENAME,
    WINDING_AUDIT_FILENAME,
    _audit_winding,
)
from ...config import PipelineConfig
from ...resolve import winding as resolve_winding


# Library-relative path: forward slashes, no '..' segments, must end in .glb.
# Same shape the audit JSON + flip-overrides JSON use.
_REL_GLB = re.compile(r"^[A-Za-z0-9_\-./]{1,512}\.glb$")


def _accessories_root(config: PipelineConfig) -> Path:
    return config.workspace / "libraries" / "accessories"


def _audit_path(config: PipelineConfig) -> Path:
    return _accessories_root(config) / WINDING_AUDIT_FILENAME


def _flip_overrides_path(config: PipelineConfig) -> Path:
    return _accessories_root(config) / FLIP_OVERRIDES_FILENAME


def _read_flip_overrides(path: Path) -> dict[str, Any]:
    """Read flip_overrides.json with the same fallback semantics as the
    composer (missing / unparseable file → empty doc)."""
    if not path.is_file():
        return {"version": 1, "updated": _now_iso(), "flipped": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "updated": _now_iso(), "flipped": []}
    flipped = doc.get("flipped")
    return {
        "version": 1,
        "updated": doc.get("updated") or _now_iso(),
        "flipped": flipped if isinstance(flipped, list) else [],
    }


def _write_flip_overrides(path: Path, doc: dict[str, Any]) -> None:
    """Atomic write of flip_overrides.json. Mirrors the composer's
    :func:`_save_flip_overrides` so format/sort order stays consistent."""
    entries = list(doc.get("flipped") or [])
    entries.sort(key=lambda e: e.get("path", ""))
    out_doc = {
        "version": 1,
        "updated": _now_iso(),
        "flipped": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(out_doc, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _toggle_flip_override(
    overrides_path: Path,
    rel_posix: str,
    flip_normals: bool,
) -> dict[str, Any]:
    """Toggle ``rel_posix`` in the overrides file. Returns the post-toggle
    state ``{"flipped": bool, "flip_normals": bool}`` so the client can
    update the badge without a follow-up GET.

    Winding-flip is involutive: present → remove (asset is now back to
    its toolkit-emitted state); absent → add (asset is now flipped).
    """
    doc = _read_flip_overrides(overrides_path)
    entries = list(doc.get("flipped") or [])
    idx = next(
        (i for i, e in enumerate(entries) if e.get("path") == rel_posix),
        -1,
    )
    if idx >= 0:
        entries.pop(idx)
        _write_flip_overrides(overrides_path, {"flipped": entries})
        return {"flipped": False, "flip_normals": False}
    entries.append({"path": rel_posix, "flip_normals": bool(flip_normals)})
    _write_flip_overrides(overrides_path, {"flipped": entries})
    return {"flipped": True, "flip_normals": bool(flip_normals)}


def _flip_glb_in_place(glb_path: Path, flip_normals_too: bool) -> dict[str, Any]:
    """Read → flip winding → (optionally) flip normals → write. Returns a
    small report compatible with the legacy ``glb_flip_winding.py`` output
    so the webview can render it inline."""
    data = glb_path.read_bytes()
    gltf, bin_data = _glb.parse_glb(data)
    new_bin, wrep = _glb.flip_winding(gltf, bin_data)
    if flip_normals_too:
        new_bin, nrep = resolve_winding.flip_normals(gltf, new_bin)
    else:
        nrep = None
    _glb.write_glb(gltf, new_bin, glb_path)
    return {"winding": wrep, "normals": nrep}


def make_router(config: PipelineConfig) -> APIRouter:
    """Build the winding-audit + flip router bound to ``config.workspace``."""
    router = APIRouter()
    workspace = config.workspace
    accessories_root = _accessories_root(config)
    audit_path = _audit_path(config)
    overrides_path = _flip_overrides_path(config)

    # ── /api/winding-audit ──────────────────────────────────────────────
    @router.get("/winding-audit")
    def get_winding_audit() -> Response:
        if not audit_path.exists():
            return JSONResponse(
                status_code=404,
                content={
                    "error": "winding_audit_missing",
                    "path": str(audit_path),
                    "hint": (
                        "Run `wows-build-accessory-library --audit-only` to "
                        "generate the audit JSON."
                    ),
                },
                headers={"Cache-Control": "no-cache"},
            )
        try:
            raw = audit_path.read_bytes()
        except OSError as err:
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": str(err)},
            )
        return Response(
            content=raw,
            media_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )

    # ── /api/auto-flip-winding ──────────────────────────────────────────
    # Bulk-apply the audit's FLIP verdicts. Run in-process via
    # ``_audit_winding`` rather than shelling out to the build CLI: the
    # CLI couples the audit to the full library build (walks ships/,
    # re-exports GLBs), while we want just the rescore + flip pass. The
    # call is fast (~3 s for ~1k assets) and the GIL is fine since the
    # work is mostly file I/O.
    #
    # Runs in the default thread pool so the event loop isn't blocked
    # while the audit walks ``library_root.rglob("*.glb")``.
    @router.post("/auto-flip-winding")
    async def post_auto_flip_winding() -> JSONResponse:
        local_warnings: list[str] = []

        def _run() -> tuple[list[str], int]:
            return _audit_winding(
                accessories_root,
                apply=True,
                warnings=local_warnings,
            )

        try:
            flipped, applied = await asyncio.to_thread(_run)
        except FileNotFoundError as err:
            # Library root absent — surface a 503 + hint (matches the
            # GET /api/winding-audit shape so the client can render the
            # same "build the library first" guidance).
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": str(err),
                    "hint": (
                        "Run `wows-build-accessory-library` to populate "
                        "<workspace>/libraries/accessories/ first."
                    ),
                },
            )
        except Exception as err:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": f"{type(err).__name__}: {err}",
                    "traceback": traceback.format_exc(),
                },
            )

        # Surface the apply count + any per-asset warnings the audit
        # accumulated. The legacy webview client only looked at
        # `ok` + `stdout/stderr`; we keep those fields populated so the
        # existing `postAutoFlipWinding` consumer keeps working.
        summary = (
            f"auto-flipped {applied} asset(s); "
            f"{len(flipped)} candidate(s) scored as FLIP"
        )
        return JSONResponse(
            content={
                "ok": True,
                "applied": applied,
                "flipped_paths": list(flipped),
                "warnings": local_warnings,
                "stdout": summary,
                "stderr": "\n".join(local_warnings),
            }
        )

    # ── /api/flip-winding ───────────────────────────────────────────────
    # Per-asset flip: rewrites the GLB on disk and toggles its entry in
    # `flip_overrides.json`. Pure in-process via _glb.flip_winding — no
    # subprocess, sub-millisecond turnaround.
    @router.post("/flip-winding")
    def post_flip_winding(body: dict[str, Any] = Body(default={})) -> JSONResponse:
        rel_path = str(body.get("relPath") or "")
        flip_normals = bool(body.get("flipNormals") or False)
        if not _REL_GLB.match(rel_path) or ".." in rel_path:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid relPath"},
            )
        # Normalise to forward slashes once. The accessories-root /
        # rel_path resolution still works on Windows because Path
        # handles either separator; we use the POSIX form for the
        # overrides file so the on-disk JSON matches the composer's
        # write format.
        rel_posix = rel_path.replace("\\", "/")
        abs_path = (accessories_root / rel_posix).resolve()
        try:
            abs_path.relative_to(accessories_root.resolve())
        except ValueError:
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": "path escapes accessories/"},
            )
        if not abs_path.exists():
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "GLB not found"},
            )
        try:
            report = _flip_glb_in_place(abs_path, flip_normals)
            override = _toggle_flip_override(overrides_path, rel_posix, flip_normals)
            return JSONResponse(
                content={
                    "ok": True,
                    "relPath": rel_path,
                    "override": override,
                    "report": report,
                }
            )
        except Exception as err:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": f"{type(err).__name__}: {err}",
                    "traceback": traceback.format_exc(),
                },
            )

    return router


__all__ = ["make_router"]
