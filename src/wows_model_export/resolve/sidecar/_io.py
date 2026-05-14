"""Serialisation, canonicalisation, and merge-preserving updates.

Three layers, in dependency order:

- :func:`_canonicalise` — reorder a document into the canonical key
  layout (deterministic on-disk output).
- :func:`dumps` / :func:`write` / :func:`read` — round-trip the document
  through bytes with v3 schema validation.
- :func:`merge_preserving` — fold ``update`` into ``base`` keyed by
  ``instance_id`` / ``material_id`` / ``skin_id`` so hand-authored
  fields survive automated re-runs.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

from ._constants import (
    PLACEMENT_SECTIONS,
    SCHEMA_VERSION,
    SidecarSchemaError,
    _ASSET_OVERRIDE_ORDER,
    _GEOMETRY_ORDER,
    _ARMOR_ORDER,
    _HITBOX_ORDER,
    _HULL_ENTRY_ORDER,
    _HULL_STATS_ORDER,
    _MATERIAL_ORDER,
    _PIPELINE_ORDER,
    _PLACEMENT_ORDER,
    _RANGES_ORDER,
    _SHELL_ORDER,
    _SHIP_ORDER,
    _SKIN_ORDER,
    _TOP_LEVEL_ORDER,
    _TORPEDO_PROFILE_ORDER,
    _TRANSFORM_ORDER,
    _VARIANTS_ORDER,
)

# ---------------------------------------------------------------------------

def _order_dict(
    d: dict[str, Any],
    preferred: tuple[str, ...],
) -> dict[str, Any]:
    """Return a new dict with ``preferred`` keys first (in order), then any
    extra keys alphabetically. Values are left untouched."""
    out: dict[str, Any] = {}
    for k in preferred:
        if k in d:
            out[k] = d[k]
    for k in sorted(d):
        if k not in out:
            out[k] = d[k]
    return out


def _canonicalise(doc: dict[str, Any]) -> dict[str, Any]:
    """Produce a dict with deterministic key order for every section.

    Lists of placements / materials / skins keep input order (the caller
    owns their ordering — typically stable-sorted by instance_id in the
    toolkit's emitter). Dict keys inside those items are reordered per the
    per-section schema.
    """
    out: dict[str, Any] = {}

    for k in _TOP_LEVEL_ORDER:
        if k not in doc:
            continue
        v = doc[k]
        if k == "pipeline" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _PIPELINE_ORDER)
        elif k == "ship" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _SHIP_ORDER)
        elif k == "variants" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _VARIANTS_ORDER)
        elif k == "hulls" and isinstance(v, dict):
            # Dict-of-dicts. Each value is a hull entry (keys ordered per
            # _HULL_ENTRY_ORDER). Outer key order: stock first, then by
            # module_id alphabetical for stable diffs.
            ordered: dict[str, Any] = {}
            entries = list(v.items())
            entries.sort(key=lambda kv: (
                not (isinstance(kv[1], dict) and kv[1].get("is_stock")),
                kv[0],
            ))
            for hull_name, entry in entries:
                if not isinstance(entry, dict):
                    ordered[hull_name] = entry
                    continue
                ordered_entry = _order_dict(_deep_sort_inner(entry), _HULL_ENTRY_ORDER)
                # Order placement lists' inner items per _PLACEMENT_ORDER.
                for sect in PLACEMENT_SECTIONS:
                    items = ordered_entry.get(sect)
                    if isinstance(items, list):
                        ordered_entry[sect] = [
                            _order_dict(_deep_sort_inner(it), _PLACEMENT_ORDER)
                            if isinstance(it, dict) else it
                            for it in items
                        ]
                # Order stats sub-block.
                stats = ordered_entry.get("stats")
                if isinstance(stats, dict):
                    ordered_entry["stats"] = _order_dict(stats, _HULL_STATS_ORDER)
                ordered[hull_name] = ordered_entry
            out[k] = ordered
        elif k == "geometry" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _GEOMETRY_ORDER)
        elif k == "armor" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _ARMOR_ORDER)
        elif k == "hitbox" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _HITBOX_ORDER)
        elif k in PLACEMENT_SECTIONS and isinstance(v, list):
            out[k] = [
                _order_dict(_deep_sort_inner(item), _PLACEMENT_ORDER)
                if isinstance(item, dict) else item
                for item in v
            ]
        elif k == "ballistics" and isinstance(v, dict):
            out[k] = _canonicalise_ballistics(v)
        elif k == "materials" and isinstance(v, list):
            out[k] = [
                _order_dict(_deep_sort_inner(item), _MATERIAL_ORDER)
                if isinstance(item, dict) else item
                for item in v
            ]
        elif k == "skins" and isinstance(v, list):
            ordered_skins: list[Any] = []
            for item in v:
                if not isinstance(item, dict):
                    ordered_skins.append(item)
                    continue
                ordered_item = _order_dict(_deep_sort_inner(item), _SKIN_ORDER)
                # Order each ``asset_overrides[<asset_id>]`` block per
                # _ASSET_OVERRIDE_ORDER, with assets keyed alphabetically
                # for stable diffs.
                ao = ordered_item.get("asset_overrides")
                if isinstance(ao, dict):
                    new_ao: dict[str, Any] = {}
                    for aid in sorted(ao):
                        entry = ao[aid]
                        if isinstance(entry, dict):
                            new_ao[aid] = _order_dict(
                                _deep_sort_inner(entry), _ASSET_OVERRIDE_ORDER,
                            )
                        else:
                            new_ao[aid] = entry
                    ordered_item["asset_overrides"] = new_ao
                ordered_skins.append(ordered_item)
            out[k] = ordered_skins
        else:
            out[k] = v

    # Forward-compat: preserve any unknown top-level keys at the end, sorted.
    for k in sorted(doc):
        if k not in out:
            out[k] = doc[k]

    # Special-case: transforms inside placement entries get their own order.
    for section in PLACEMENT_SECTIONS:
        if section not in out:
            continue
        for item in out[section]:
            if isinstance(item, dict) and isinstance(item.get("transform"), dict):
                item["transform"] = _order_dict(
                    item["transform"], _TRANSFORM_ORDER,
                )

    return out


def _deep_sort_inner(obj: Any) -> Any:
    """Recursively alphabetise keys in nested dicts. Lists keep order."""
    if isinstance(obj, dict):
        return {k: _deep_sort_inner(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_deep_sort_inner(v) for v in obj]
    return obj


def _canonicalise_ballistics(b: dict[str, Any]) -> dict[str, Any]:
    """Canonical ordering for the ``ballistics`` section.

    * Top-level keys ordered per :data:`_BALLISTICS_ORDER`.
    * ``source`` ordered alphabetically (no fixed schema yet).
    * ``ranges`` ordered per :data:`_RANGES_ORDER`.
    * ``shells`` keyed by ammo_id, alphabetically sorted; each entry
      ordered per :data:`_SHELL_ORDER` for stable diffs.
    * ``torpedoes`` keyed by ammo_id, alphabetically sorted; each entry
      ordered per :data:`_TORPEDO_PROFILE_ORDER`.
    """
    out: dict[str, Any] = {}
    src = b.get("source")
    if isinstance(src, dict):
        out["source"] = _deep_sort_inner(src)
    rng = b.get("ranges")
    if isinstance(rng, dict):
        out["ranges"] = _order_dict(_deep_sort_inner(rng), _RANGES_ORDER)
    shells = b.get("shells")
    if isinstance(shells, dict):
        ordered_shells: dict[str, Any] = {}
        for ammo_id in sorted(shells):
            entry = shells[ammo_id]
            if isinstance(entry, dict):
                ordered_shells[ammo_id] = _order_dict(
                    _deep_sort_inner(entry), _SHELL_ORDER,
                )
            else:
                ordered_shells[ammo_id] = entry
        out["shells"] = ordered_shells
    torps = b.get("torpedoes")
    if isinstance(torps, dict):
        ordered_torps: dict[str, Any] = {}
        for ammo_id in sorted(torps):
            entry = torps[ammo_id]
            if isinstance(entry, dict):
                ordered_torps[ammo_id] = _order_dict(
                    _deep_sort_inner(entry), _TORPEDO_PROFILE_ORDER,
                )
            else:
                ordered_torps[ammo_id] = entry
        out["torpedoes"] = ordered_torps
    # Forward-compat: surface unknown keys at the end, sorted.
    for k in sorted(b):
        if k not in out:
            out[k] = _deep_sort_inner(b[k])
    return out



def dumps(doc: dict[str, Any]) -> str:
    """Serialise to the canonical on-disk form.

    2-space indent, LF newlines, spec-ordered keys, trailing LF. The output
    is byte-stable for identical input — re-running the pipeline with no
    changes yields the same bytes.
    """
    canon = _canonicalise(doc)
    buf = io.StringIO()
    json.dump(canon, buf, indent=2, ensure_ascii=False, sort_keys=False)
    text = buf.getvalue().replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def write(doc: dict[str, Any], path: str | Path) -> Path:
    """Write a sidecar to disk using canonical formatting.

    Writes atomically via a sibling ``.tmp`` rename. Binary mode keeps
    Windows from sneaking CRLF into the output.
    """
    path = Path(path)
    text = dumps(doc)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(text.encode("utf-8"))
    os.replace(tmp, path)
    return path


def read(path: str | Path) -> dict[str, Any]:
    """Load + validate a v3 sidecar.

    Raises :class:`SidecarSchemaError` for:
      - Missing / non-int ``schema_version``
      - v1 input (``schema_version == 1``). v1 is not auto-migrated; ships
        regenerate through the new pipeline.
      - v2 input (``schema_version == 2``). v2 ships must be
        regenerated to pick up the new ``materials[i].texture_sets``
        scheme-keyed structure + ``skins[].scheme_key`` field. Run
        ``python tools/ship/scaffold_ship.py <Ship> --skip-export
        --skip-armor --skip-ammo`` to upgrade.
      - Any ``schema_version`` other than :data:`SCHEMA_VERSION`.
    """
    path = Path(path)
    with open(path, "rb") as f:
        data = f.read().decode("utf-8")
    doc = json.loads(data)
    if not isinstance(doc, dict):
        raise SidecarSchemaError(f"{path}: sidecar root must be an object")
    version = doc.get("schema_version")
    if not isinstance(version, int):
        raise SidecarSchemaError(
            f"{path}: missing or non-int 'schema_version' — not a valid sidecar"
        )
    if version == 1:
        raise SidecarSchemaError(
            f"{path}: schema_version=1 is not supported. v1 ships must be "
            "regenerated through the toolkit pipeline (run `wows-scaffold-ship "
            "<Ship>`); there is no automatic migration."
        )
    if version == 2:
        raise SidecarSchemaError(
            f"{path}: schema_version=2 is not supported. v2 ships must be "
            "regenerated to pick up the v3 `materials[i].texture_sets` + "
            "`skins[].scheme_key` structure. Re-scaffold with "
            "`wows-scaffold-ship <Ship> --skip-export --skip-armor --skip-ammo`."
        )
    if version != SCHEMA_VERSION:
        raise SidecarSchemaError(
            f"{path}: schema_version={version} not supported by this "
            f"library (expected {SCHEMA_VERSION})"
        )
    return doc



# ---------------------------------------------------------------------------

#: Which list-of-dict sections merge by which identifier key.
_KEYED_LIST_SECTIONS: dict[str, str] = {
    "turrets": "instance_id",
    "secondaries": "instance_id",
    "antiair": "instance_id",
    "torpedoes": "instance_id",
    "accessories": "instance_id",
    "materials": "material_id",
    "skins": "skin_id",
}


def merge_preserving(
    base: dict[str, Any],
    update: dict[str, Any],
) -> dict[str, Any]:
    """Recursive merge that preserves fields in ``base`` not mentioned by
    ``update``.

    Semantics:
      - Dicts merge recursively.
      - For placement arrays (``turrets`` / ``secondaries`` / ``antiair`` /
        ``torpedoes`` / ``accessories``): items match by ``instance_id``.
        The update's fields override; unmentioned fields (including
        hand-authored ``attach_to`` / ``casts_shadow`` / custom
        ``ammo_types``) survive.
      - ``materials[]`` matches by ``material_id``; ``skins[]`` by
        ``skin_id``.
      - For primitive values, ``update`` wins (pass ``None`` explicitly to
        clear a field).
      - Items in ``base`` whose key doesn't appear in ``update`` survive
        unchanged.

    Returns a new dict; input dicts are not mutated.
    """
    out = _deepcopy_jsonish(base)
    _merge_into(out, update)
    return out


def _merge_into(base: dict[str, Any], update: dict[str, Any]) -> None:
    for k, v in update.items():
        if k in _KEYED_LIST_SECTIONS and isinstance(v, list):
            key = _KEYED_LIST_SECTIONS[k]
            base[k] = _merge_keyed_list(base.get(k, []) or [], v, key)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge_into(base[k], v)
        else:
            base[k] = _deepcopy_jsonish(v)


def _merge_keyed_list(
    old: list[dict[str, Any]],
    new: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    """Merge two lists of dicts keyed by ``key``.

    - Items in ``new`` with a known key merge into the corresponding ``old``
      entry; unknown keys append at the end (in ``new``'s order).
    - Items in ``old`` whose key isn't mentioned in ``new`` survive
      unchanged and appear after the ``new`` items.
    """
    old_by_key = {
        item[key]: item for item in old
        if isinstance(item, dict) and key in item
    }
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for item in new:
        if not isinstance(item, dict):
            out.append(_deepcopy_jsonish(item))
            continue
        k = item.get(key)
        if k is None:
            out.append(_deepcopy_jsonish(item))
            continue
        base = _deepcopy_jsonish(old_by_key.get(k, {}))
        _merge_into(base, item)
        out.append(base)
        seen.add(k)
    for item in old:
        if not isinstance(item, dict):
            continue
        k = item.get(key)
        if k is not None and k not in seen:
            out.append(_deepcopy_jsonish(item))
    return out


def _deepcopy_jsonish(obj: Any) -> Any:
    """Shallow-enough deep copy for JSON-shaped data. Cheaper than
    ``copy.deepcopy`` because we know the value domain."""
    if isinstance(obj, dict):
        return {k: _deepcopy_jsonish(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deepcopy_jsonish(v) for v in obj]
    return obj
