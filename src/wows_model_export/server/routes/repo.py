"""``GET /repo/<rel-path>`` — workspace static file service.

The webview reads hull GLBs, sidecar JSON, DDS mip chains, and accessory
library GLBs from the user's workspace. Browsers can't ``fetch('file:')``,
so this route proxies workspace files through the API server.

Security: path traversal is blocked by an :func:`~.workspace.is_child_of`
check before reading. Symlinks are followed (so workspace junctions on
Windows still work), but the resolved path must still land inside the
workspace root.

Port of ``webview/src/server/endpoints/repo.ts``. The MIME map is
preserved verbatim — same extension → same Content-Type, including the
non-standard ``image/vnd.ms-dds`` for ``.dds``.

Range support: Starlette's :class:`FileResponse` (which we use) honours
the ``Range`` request header automatically — large GLBs stream chunked
without extra plumbing. The Node version did not; this is a small but
welcome improvement.

Cache policy: DDS / GLB / sidecar JSON files in the workspace are
immutable per-extraction. We send ``Cache-Control: public, max-age=300``
on binary payloads (DDS, GLB) so the browser can serve repeated reads
out of memory cache without a revalidation round-trip — meaningful when
a ship has 100+ DDS files and the user toggles textures on/off. JSON
artifacts (sidecar, accessory index) keep ``no-cache`` so re-extractions
in the same session surface promptly. A hard reload (Ctrl+Shift+R)
clears the binary cache if the user wants to see freshly re-extracted
textures within the 5-minute window.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.requests import Request

from ...config import PipelineConfig
from ..workspace import is_child_of


MIME: dict[str, str] = {
    ".json": "application/json",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".dds": "image/vnd.ms-dds",
    ".dd0": "application/octet-stream",
    ".vfd": "application/octet-stream",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bin": "application/octet-stream",
}

# Suffixes whose payload is content-addressed by the extraction pipeline
# (writing them is the only way they change) — safe to cache aggressively
# in the browser. JSON artifacts fall through to the conservative default
# because the accessory library index is rewritten when new ships land.
_CACHEABLE_SUFFIXES = frozenset(
    {
        ".dds",
        ".dd0",
        ".dd1",
        ".dd2",
        ".vfd",
        ".glb",
        ".gltf",
        ".bin",
        ".png",
        ".jpg",
        ".jpeg",
    }
)


def _mime_for(p: Path) -> str:
    return MIME.get(p.suffix.lower(), "application/octet-stream")


def _cache_control_for(p: Path) -> str:
    if p.suffix.lower() in _CACHEABLE_SUFFIXES:
        return "public, max-age=300"
    return "no-cache"


def make_router(config: PipelineConfig) -> APIRouter:
    router = APIRouter()
    workspace = config.workspace

    # FastAPI lets us bind a `path` converter; this keeps ``/`` characters
    # in the URL as part of the captured value, which is what we want
    # for the workspace-relative file lookup.
    # response_model=None: the handler can return one of three response
    # types, none of which is a Pydantic model — FastAPI's auto-inference
    # gets confused otherwise.
    @router.get("/{rel:path}", response_model=None)
    def serve_repo(rel: str, request: Request):
        # Trim the leading mount prefix's residual slash + decode any
        # percent escapes the way the Node ``decodeURIComponent`` call
        # did. (Starlette already decodes the path; we still strip a
        # stray leading slash so absolute paths can't be smuggled in.)
        rel_decoded = unquote(rel)
        rel_clean = rel_decoded.lstrip("/")
        if not rel_clean:
            return PlainTextResponse(status_code=404, content="not found")

        candidate = (workspace / rel_clean)
        try:
            abs_path = candidate.resolve()
        except (OSError, RuntimeError):
            return PlainTextResponse(status_code=404, content="not found")

        if abs_path != workspace.resolve() and not is_child_of(abs_path, workspace):
            return PlainTextResponse(status_code=403, content="forbidden")
        if not abs_path.is_file():
            return PlainTextResponse(status_code=404, content="not found")

        return FileResponse(
            path=str(abs_path),
            media_type=_mime_for(abs_path),
            headers={"Cache-Control": _cache_control_for(abs_path)},
        )

    return router


__all__ = ["make_router"]
