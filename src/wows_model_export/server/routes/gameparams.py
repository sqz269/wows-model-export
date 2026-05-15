"""GameParams browser + cache status endpoints.

* ``GET /api/gameparams/status`` — cache age + size + path (originally
  ported from the Node middleware). Used by the Extract page's
  "GameParams cache stale?" banner.

* ``GET /api/gameparams/types`` — `{counts: {Ship: N, Exterior: M, …}}`.
  Drives the type filter dropdown in the GameParams browser route.

* ``GET /api/gameparams/list`` — paginated/filtered summary list.
  Server-side filter by `type` + free-text `q`; server slices via
  `limit`/`offset`. Returns light-weight rows (id, type, species,
  nation, level) so the frontend can render hundreds without blowing
  the wire.

* ``GET /api/gameparams/entity/{entity_id}`` — full record JSON. Used
  by the right-pane JSON tree.

All three browser endpoints touch :func:`read.gameparams.load_full`,
which keeps the parsed 3-4 GB dict resident per-process after the
first call. Endpoints stream rather than copy where they can.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ...config import PipelineConfig
from ...read import gameparams as gp


def _summary_row(entity_id: str, entity: dict[str, Any]) -> dict[str, Any]:
    """Tabular row used by the browser list view.

    Pulled fields are the ones the UI surfaces — keep this in sync with
    `webview/src/lib/types/gameparams.ts:GameParamSummary`. Missing
    fields surface as null so the frontend can show `—`.
    """
    typeinfo = entity.get("typeinfo") or {}
    return {
        "id": entity_id,
        "type": typeinfo.get("type"),
        "species": typeinfo.get("species"),
        "nation": typeinfo.get("nation"),
        "level": entity.get("level"),
        # `name` on Vehicles is usually the same as the entity_id; on
        # Exteriors it's the cosmetic display name. Surface verbatim
        # when present so the UI can show e.g. "PCEE001_Bastard".
        "name": entity.get("name") if isinstance(entity.get("name"), str) else None,
    }


def make_router(config: PipelineConfig) -> APIRouter:
    router = APIRouter()
    cache_dir = config.cache_dir or (config.workspace / ".cache")
    gp_path: Path = cache_dir / "gameparams.json"

    # ── status (existing) ───────────────────────────────────────────────

    @router.get("/gameparams/status")
    def status() -> JSONResponse:
        if not gp_path.exists():
            return JSONResponse(
                content={
                    "exists": False,
                    "path": str(gp_path),
                    "hint": (
                        "Run `wows-find-ship-variants --refresh` to "
                        "populate it."
                    ),
                },
                headers={"Cache-Control": "no-cache"},
            )
        try:
            st = gp_path.stat()
        except OSError as err:
            return JSONResponse(
                status_code=500,
                content={"error": "internal_error", "detail": str(err)},
            )
        mtime = st.st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        ms = dt.microsecond // 1000
        mtime_iso = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"
        size_mb = round(st.st_size / (1024 * 1024), 1)
        return JSONResponse(
            content={
                "exists": True,
                "path": str(gp_path),
                "size_mb": size_mb,
                "mtime": int(mtime),
                "mtime_iso": mtime_iso,
            },
            headers={"Cache-Control": "no-cache"},
        )

    # ── browser ─────────────────────────────────────────────────────────

    @router.get("/gameparams/types")
    def types() -> JSONResponse:
        """Histogram of `typeinfo.type` values across the dump. First
        call triggers the ~10 s flat-load; subsequent calls hit the
        per-process cache (~instant)."""
        if not gp_path.exists():
            raise HTTPException(
                status_code=404,
                detail="gameparams.json not built — run `wows-find-ship-variants --refresh`",
            )
        flat = gp.load_full()
        counts: dict[str, int] = {}
        for entity in flat.values():
            t = (entity.get("typeinfo") or {}).get("type") or "Unknown"
            counts[t] = counts.get(t, 0) + 1
        return JSONResponse(
            content={"counts": counts, "total": len(flat)},
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/gameparams/list")
    def list_entities(
        type: str | None = None,  # noqa: A002 — matches the query name
        q: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> JSONResponse:
        """Filtered/paginated list of summary rows.

        - `type` matches `typeinfo.type` exactly (case-sensitive).
        - `q` is a case-insensitive substring match against the
          entity_id and `name`. No fuzzy matching — the IDs are
          already discoverable; this is a "find by prefix" tool.
        - `limit` clamped to [1, 1000]; default 200.
        """
        if not gp_path.exists():
            raise HTTPException(status_code=404, detail="gameparams.json not built")
        limit = max(1, min(1000, int(limit)))
        offset = max(0, int(offset))
        needle = q.lower() if q else None
        flat = gp.load_full()
        # Single pass: collect summary rows for everything that matches
        # the filters, then slice. With ~5-10k entities total the cost
        # is dominated by the predicate calls, not list growth.
        matched: list[dict[str, Any]] = []
        for entity_id, entity in flat.items():
            if type is not None:
                t = (entity.get("typeinfo") or {}).get("type")
                if t != type:
                    continue
            if needle is not None:
                name = entity.get("name") if isinstance(entity.get("name"), str) else ""
                if needle not in entity_id.lower() and needle not in (name or "").lower():
                    continue
            matched.append(_summary_row(entity_id, entity))
        matched.sort(key=lambda r: r["id"])
        total = len(matched)
        sliced = matched[offset : offset + limit]
        return JSONResponse(
            content={"total": total, "offset": offset, "limit": limit, "items": sliced},
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/gameparams/entity/{entity_id}")
    def entity(entity_id: str) -> JSONResponse:
        """Full record for a single entity. Prefix-form (`PASB018`)
        auto-resolves to the full key via `get_entity` — convenient
        for deep-linking from Vehicle param_index references."""
        if not gp_path.exists():
            raise HTTPException(status_code=404, detail="gameparams.json not built")
        rec = gp.get_entity(entity_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"entity not found: {entity_id}")
        return JSONResponse(
            content={"id": entity_id, "entity": rec},
            headers={"Cache-Control": "no-cache"},
        )

    return router


__all__ = ["make_router"]
