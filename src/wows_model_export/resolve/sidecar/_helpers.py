"""Small pure helpers shared across the sidecar submodules.

Contains:

- ``normalise_hitbox_token`` — public canonicalisation entry for raw
  WG hitbox/zone names (the only export, surfaced on the package
  ``__init__``).
- private ``_normalise_*`` / ``_now_iso`` / ``_today_iso_date`` /
  ``_default_exporter`` — used by constructors and document builders.

No GLB reads, no JSON I/O. Pure stdlib.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from ._constants import HITBOX_TOKEN_MAP

def normalise_hitbox_token(token: str) -> str:
    """Return the canonical zone name for a raw splash-box or GameParams
    hitLocations token. Unknown tokens pass through lowercase.

    Accepts ``CM_SB_bow_1``, ``ruder``, ``SteeringGear``, ``ss_3``,
    ``gk_1_1`` (multi-level per-turret-barbette index) etc. Strips the
    ``CM_SB_`` prefix and iteratively peels trailing
    integer/underscore components until a known token appears in
    :data:`HITBOX_TOKEN_MAP` or no further stripping is possible.
    """
    t = token.strip()
    if t.startswith("CM_SB_"):
        t = t[len("CM_SB_"):]
    # Full-token lookup first, then case-insensitive fallback for raw
    # tokens that came through with mixed case (e.g. ``Stearinggear`` from
    # a hand-edited GameParams scrape).
    if t in HITBOX_TOKEN_MAP:
        return HITBOX_TOKEN_MAP[t]
    if t.lower() in HITBOX_TOKEN_MAP:
        return HITBOX_TOKEN_MAP[t.lower()]
    # Strip trailing ``_<n>`` groups iteratively, checking the map at each
    # level. Examples: ``gk_1_1`` → ``gk_1`` → ``gk`` (map hit → citadel);
    # ``ss_3_4`` → ``ss_3`` → ``ss`` (map hit → superstructure).
    stripped = t
    while stripped:
        if stripped in HITBOX_TOKEN_MAP:
            return HITBOX_TOKEN_MAP[stripped]
        low = stripped.lower()
        if low in HITBOX_TOKEN_MAP:
            return HITBOX_TOKEN_MAP[low]
        next_stripped = stripped.rstrip("0123456789").rstrip("_")
        if next_stripped == stripped:
            break
        stripped = next_stripped
    return (stripped or t).lower()


# ---------------------------------------------------------------------------
# Internal normalisers
# ---------------------------------------------------------------------------

def _normalise_transform(t: dict[str, Any] | None) -> dict[str, Any]:
    """Validate + shape a placement transform dict.

    Required: ``matrix`` (16 floats, column-major, metric). Optional but
    recommended: ``position`` (3 floats, convenience readout).
    """
    if not isinstance(t, dict):
        raise ValueError("transform must be an object with a 'matrix' key")
    matrix = t.get("matrix")
    if matrix is None:
        raise ValueError("transform.matrix is required (16 floats, column-major)")
    if len(list(matrix)) != 16:
        raise ValueError("transform.matrix must be 16 floats (column-major)")
    out: dict[str, Any] = {"matrix": [float(v) for v in matrix]}
    pos = t.get("position")
    if pos is not None:
        pos_list = [float(v) for v in pos]
        if len(pos_list) != 3:
            raise ValueError("transform.position must be 3 floats")
        out["position"] = pos_list
    return out


def _normalise_zone_dict(
    zones: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Normalise armor-zone keys to lowercase canonical form (matching
    ``hitbox.regions``). Toolkit emits CamelCase (``Bow``, ``SteeringGear``,
    ``TorpedoProtection``); we fold through :func:`normalise_hitbox_token`
    so both armor and hitbox sections share vocabulary."""
    out: dict[str, dict[str, Any]] = {}
    for name, info in zones.items():
        canonical = normalise_hitbox_token(name)
        entry: dict[str, Any] = {}
        if "default_thickness_mm" in info:
            entry["default_thickness_mm"] = float(info["default_thickness_mm"])
        if "max_thickness_mm" in info:
            entry["max_thickness_mm"] = float(info["max_thickness_mm"])
        if "plate_count" in info:
            entry["plate_count"] = int(info["plate_count"])
        for k, v in info.items():
            if k not in entry:
                entry[k] = v
        out[canonical] = entry
    return out


def _normalise_materials_table(
    table: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Normalise per-material ``zones`` to lowercase canonical form
    (matching ``armor.zones`` keys and ``hitbox.regions`` keys)."""
    out: dict[str, dict[str, Any]] = {}
    for mat_id, info in table.items():
        entry: dict[str, Any] = {}
        if "thickness_mm" in info:
            entry["thickness_mm"] = float(info["thickness_mm"])
        if "layers" in info:
            entry["layers"] = [float(v) for v in info["layers"]]
        if "zones" in info:
            entry["zones"] = [normalise_hitbox_token(z) for z in info["zones"]]
        if info.get("hidden"):
            entry["hidden"] = True
        for k, v in info.items():
            if k not in entry:
                entry[k] = v
        out[str(mat_id)] = entry
    return out


def _now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _today_iso_date() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _default_exporter() -> str:
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    host = (
        os.environ.get("COMPUTERNAME")
        or os.environ.get("HOSTNAME")
        or os.environ.get("HOST")
        or "unknown"
    )
    return f"{user}@{host}"


# ---------------------------------------------------------------------------
