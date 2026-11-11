"""Which exports are out of date after a game update.

Every tracked export directory holds a ``build_deps.json``: the game build
it was made from, and a fingerprint (from that build's manifest, see
:mod:`.build_manifest`) for every game input it read. After an update, an
export is stale if any of those inputs has a different fingerprint in the
current build's manifest. Nothing else needs re-exporting.

Inputs come from two places:

- the :class:`~wows_model_export.build_deps.DepsCollector` filled while the
  export ran (toolkit ``--deps-out`` for every ``wowsunpack`` call, plus
  explicit records from code that reads pipeline caches);
- a scan of the export's own JSON outputs for strings that name a game
  input: GameParams entity ids, VFS paths, particle ``.xml`` paths and
  ``camouflages.xml`` entry names. This picks up data read from the
  GameParams / ``assets.bin`` / ``camouflages.xml`` caches without
  instrumenting every lookup.

Whole-file monoliths (``GameParams.data``, ``assets.bin``,
``camouflages.xml``) are never recorded as file inputs: they change every
build. Their entries are tracked one by one instead.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..build_deps import DepsCollector, norm_path
from ..config import PipelineConfig
from ..toolkit import build as _build
from . import build_manifest as _bm

DEPS_FILENAME = "build_deps.json"
DEPS_SCHEMA = "wows.build_deps/v1"

# Files that change every build; tracked per entry instead.
MONOLITH_FILES = frozenset({
    "/content/GameParams.data",
    "/content/assets.bin",
    "/camouflages.xml",
})

# Files the toolkit opens for its own setup, not for export content: the
# entity definitions (``scripts/entity_defs/*.def``, ``entities.xml``) feed
# its RPC / metadata layer and change in most updates. Tracking them would
# mark every export stale on every patch.
IGNORED_FILE_PREFIXES = ("/scripts/",)


def is_tracked_file(path: str) -> bool:
    return path not in MONOLITH_FILES and not path.startswith(IGNORED_FILE_PREFIXES)


# Output JSON larger than this isn't scanned for references.
_SCAN_MAX_BYTES = 96 * 1024 * 1024


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Reference scan of an export's outputs
# ---------------------------------------------------------------------------


@dataclass
class OutputRefs:
    files: set[str] = field(default_factory=set)
    records: set[str] = field(default_factory=set)
    gameparams: set[str] = field(default_factory=set)
    camouflages: set[str] = field(default_factory=set)


# A JSON string literal (keys and values). Sidecars are mostly numbers, so
# matching the literals in the raw text is much faster than parsing the
# document and walking every value.
_JSON_STRING = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')


def _json_strings(text: str) -> set[str]:
    out: set[str] = set()
    for s in set(_JSON_STRING.findall(text)):
        if "\\" in s:
            try:
                s = json.loads(f'"{s}"')
            except ValueError:
                continue
        out.add(s)
    return out


def _json_outputs(out_dir: Path) -> Iterator[Path]:
    for root, dirs, files in os.walk(out_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.endswith(".json") or name == DEPS_FILENAME:
                continue
            p = Path(root) / name
            try:
                if p.stat().st_size <= _SCAN_MAX_BYTES:
                    yield p
            except OSError:
                continue


_PATHISH = re.compile(r"^[\w./\\-]+\.[\w]+(/(high|low|shared))?$")


def scan_output_refs(out_dir: Path, manifest: _bm.BuildManifest) -> OutputRefs:
    refs = OutputRefs()
    camo_names = {k.split(":", 1)[1]: k for k in manifest.camouflages}
    seen: set[str] = set()
    for path in _json_outputs(out_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for s in _json_strings(text):
            if s in seen or not s or len(s) > 300:
                continue
            seen.add(s)
            if s in manifest.gameparams:
                refs.gameparams.add(s)
            key = camo_names.get(s)
            if key is not None:
                refs.camouflages.add(key)
            if ("/" in s or "\\" in s) and _PATHISH.match(s):
                p = norm_path(s)
                if p in manifest.files and p not in MONOLITH_FILES:
                    refs.files.add(p)
                elif p in manifest.records:
                    refs.records.add(p)
                elif p.startswith("/particles/") and p.endswith(".xml"):
                    refs.records.update(manifest.particle_records(p))
    return refs


# ---------------------------------------------------------------------------
# Writing an export's deps
# ---------------------------------------------------------------------------


def _fingerprints(manifest: _bm.BuildManifest, inputs: dict[str, Iterable[str]]) -> dict[str, dict]:
    return {
        "files": {p: manifest.file_fp(p) for p in sorted(inputs["files"])},
        "records": {p: manifest.record_fp(p) for p in sorted(inputs["records"])},
        "gameparams": {k: manifest.gameparams_fp(k) for k in sorted(inputs["gameparams"])},
        "camouflages": {k: manifest.camouflage_fp(k) for k in sorted(inputs["camouflages"])},
    }


def write_export_deps(
    out_dir: Path,
    *,
    kind: str,
    name: str,
    collector: DepsCollector,
    reexport: dict[str, Any] | None = None,
    config: PipelineConfig | None = None,
    merge: bool = False,
) -> Path:
    """Write ``out_dir/build_deps.json`` for a finished export.

    With ``merge``, inputs are added to the existing file instead of
    replacing it (a skin pack ingested into an already-exported ship).
    Inputs already listed keep the fingerprint they were recorded with,
    and the file keeps its original ``build`` and ``reexport``, so a ship
    exported on an older build still shows up as stale."""
    cfg = config or PipelineConfig.load()
    build = _build.current_build(cfg)
    manifest = _bm.load_manifest(build, config=cfg)
    out = Path(out_dir) / DEPS_FILENAME
    refs = scan_output_refs(out_dir, manifest)
    raw = collector.as_dict()
    inputs = {
        "files": {p for p in set(raw["files"]) | refs.files if is_tracked_file(p)},
        "records": set(raw["records"]) | refs.records,
        "gameparams": set(raw["gameparams"]) | refs.gameparams,
        "camouflages": refs.camouflages,
    }
    fingerprints = _fingerprints(manifest, inputs)
    toolkit_calls = raw["toolkit_calls"]
    toolkit_builds = set(raw["toolkit_builds"])
    created_at = _now()
    prior: dict[str, Any] = {}
    if merge and out.is_file():
        try:
            prior = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior = {}
    if prior:
        for section, entries in (prior.get("inputs") or {}).items():
            fingerprints.setdefault(section, {}).update(entries)
        build = prior.get("build", build)
        reexport = prior.get("reexport") or reexport
        created_at = prior.get("created_at") or created_at
        toolkit_calls += int(prior.get("toolkit_calls") or 0)
        toolkit_builds |= set(prior.get("toolkit_builds") or ())
    fingerprints = {k: dict(sorted(v.items())) for k, v in fingerprints.items()}
    doc = {
        "schema": DEPS_SCHEMA,
        "kind": kind,
        "name": name,
        "build": build,
        "created_at": created_at,
        "updated_at": _now(),
        "toolkit_calls": toolkit_calls,
        "toolkit_builds": sorted(toolkit_builds),
        "reexport": reexport or {},
        "counts": {k: len(v) for k, v in fingerprints.items()},
        "schemes": (prior.get("schemes") if prior else None) or dict(manifest.schemes),
        "inputs": fingerprints,
    }
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return out


def write_export_deps_safely(out_dir: Path | None, **kwargs: Any) -> Path | None:
    """:func:`write_export_deps` that logs instead of failing the export."""
    if out_dir is None or not Path(out_dir).is_dir():
        return None
    try:
        return write_export_deps(Path(out_dir), **kwargs)
    except Exception as err:  # noqa: BLE001 - tracking must never fail an export
        print(f"[build-deps] could not write {DEPS_FILENAME} in {out_dir}: "
              f"{type(err).__name__}: {err}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


@dataclass
class ExportStatus:
    kind: str
    name: str
    path: str
    state: str  # current | stale | untracked | error | removed
    build: int | None = None
    exported_at: str | None = None
    changed: dict[str, list[str]] = field(default_factory=dict)
    removed: dict[str, list[str]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    reexport: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    # Sections that couldn't be checked: the hashing changed since the
    # export was made and its build's manifest is gone.
    incomparable: list[str] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return sum(len(v) for v in self.changed.values()) + sum(len(v) for v in self.removed.values())

    def as_dict(self, *, limit: int | None = 50) -> dict[str, Any]:
        def cut(d: dict[str, list[str]]) -> dict[str, Any]:
            return {k: (v if limit is None else v[:limit]) for k, v in d.items() if v}

        return {
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "state": self.state,
            "build": self.build,
            "exported_at": self.exported_at,
            "changed_count": self.changed_count,
            "changed": cut(self.changed),
            "removed": cut(self.removed),
            "counts": self.counts,
            "reexport": self.reexport,
            "note": self.note,
            "incomparable": self.incomparable,
        }


def check_deps(
    doc: dict[str, Any],
    manifest: _bm.BuildManifest,
    base: _bm.BuildManifest | None = None,
) -> tuple[dict, dict, list[str]]:
    """Inputs whose fingerprint differs in ``manifest`` (changed), inputs
    no longer in it (removed), and sections that couldn't be compared.

    ``base`` is the saved manifest of the build the export was made from.
    When its hash scheme matches ``manifest``'s, both sides are read from
    the manifests, which survives a change of hashing. Otherwise the
    fingerprints stored in ``doc`` are used, if their scheme matches."""
    stored_schemes = doc.get("schemes") or _bm.LEGACY_SCHEMES
    changed: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    incomparable: list[str] = []
    for section, entries in (doc.get("inputs") or {}).items():
        if section not in _bm.SECTIONS:
            continue
        scheme = manifest.scheme(section)
        use_base = base is not None and base.scheme(section) == scheme
        if not use_base and stored_schemes.get(section, _bm.LEGACY_SCHEMES[section]) != scheme:
            incomparable.append(section)
            continue
        ch, rm = [], []
        for key, stored in entries.items():
            if section == "files" and not is_tracked_file(key):
                continue
            old = base.fingerprint(section, key) if use_base else stored
            new = manifest.fingerprint(section, key)
            if isinstance(old, (list, tuple)):
                old = list(old)
            if isinstance(new, (list, tuple)):
                new = list(new)
            if new == old:
                continue
            if new is None:
                rm.append(key)
            else:
                ch.append(key)
        changed[section] = sorted(ch)
        removed[section] = sorted(rm)
    return changed, removed, incomparable


# ---------------------------------------------------------------------------
# Accessory library assets
# ---------------------------------------------------------------------------

LIBRARY_ASSET_KIND = "library_asset"


class LibraryStaleness:
    """Build tracking for accessory-library assets (turrets, directors,
    boats, ...), which are shared across ships and exported once each.

    Each asset folder gets a ``build_deps.json`` listing the game files
    and records the asset is built from (:meth:`BuildManifest.asset_sources`)
    with their fingerprints in the build it was exported from. An asset
    must be exported again when any of them changed in the current build,
    when its source set gained an entry, or when it has no record at all
    (exported before tracking, so the game build is unknown). WG does
    re-author shared assets: AGM034's turret bone lost its Z-mirror between
    builds, and a library GLB from the older build then rendered 180° off
    against placements computed from the new one.
    """

    def __init__(self, manifest: _bm.BuildManifest, config: PipelineConfig):
        self.manifest = manifest
        self._cfg = config
        self._stored = set(_bm.stored_builds(config))
        self._bases: dict[int, _bm.BuildManifest | None] = {manifest.build: manifest}

    @classmethod
    def for_current_build(cls, config: PipelineConfig) -> LibraryStaleness:
        build = _build.current_build(config)
        return cls(_bm.load_manifest(build, config=config), config)

    def _base(self, build: Any) -> _bm.BuildManifest | None:
        if not isinstance(build, int):
            return None
        if build not in self._bases:
            base = None
            if build in self._stored:
                try:
                    base = _bm.load_manifest(build, config=self._cfg, create=False)
                except Exception:  # noqa: BLE001 - fall back to stored fingerprints
                    base = None
            self._bases[build] = base
        return self._bases[build]

    def reason(self, out_dir: Path, *, asset_id: str, asset_dir: str) -> str | None:
        """Why the asset in ``out_dir`` must be exported again, or None
        when it is current."""
        try:
            doc = json.loads((Path(out_dir) / DEPS_FILENAME).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "no build record"
        except (OSError, ValueError):
            return "unreadable build record"
        changed, removed, incomparable = check_deps(doc, self.manifest, self._base(doc.get("build")))
        n_changed = sum(len(v) for v in changed.values())
        if n_changed:
            return f"{n_changed} source(s) changed since build {doc.get('build')}"
        if any(removed.values()):
            return f"source(s) removed since build {doc.get('build')}"
        if incomparable:
            return "hashing changed since it was exported"
        files, records = self.manifest.asset_sources(asset_dir, asset_id)
        inputs = doc.get("inputs") or {}
        known = set(inputs.get("files") or ()) | set(inputs.get("records") or ())
        if any(p not in known for p in (*files, *records) if is_tracked_file(p)):
            return f"new source(s) in build {self.manifest.build}"
        return None

    def write(self, out_dir: Path, *, asset_id: str, asset_dir: str) -> Path:
        """Record the asset's sources as of the current build."""
        files, records = self.manifest.asset_sources(asset_dir, asset_id)
        fingerprints = _fingerprints(self.manifest, {
            "files": [p for p in files if is_tracked_file(p)],
            "records": records,
            "gameparams": (),
            "camouflages": (),
        })
        fingerprints = {k: v for k, v in fingerprints.items() if v}
        now = _now()
        doc = {
            "schema": DEPS_SCHEMA,
            "kind": LIBRARY_ASSET_KIND,
            "name": asset_id,
            "build": self.manifest.build,
            "created_at": now,
            "updated_at": now,
            "asset_dir": "/" + asset_dir.strip("/"),
            "counts": {k: len(v) for k, v in fingerprints.items()},
            "schemes": dict(self.manifest.schemes),
            "inputs": fingerprints,
        }
        out = Path(out_dir) / DEPS_FILENAME
        tmp = out.with_name(f"{DEPS_FILENAME}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, out)
        return out


def library_deps_files(config: PipelineConfig) -> Iterator[Path]:
    """``build_deps.json`` of every accessory-library asset."""
    root = config.workspace / "libraries" / "accessories"
    if root.is_dir():
        yield from sorted(root.rglob(DEPS_FILENAME))


def library_status(
    config: PipelineConfig | None = None,
    manifest: _bm.BuildManifest | None = None,
) -> dict[str, Any]:
    """Game-build state of the accessory library: how many assets are
    current, which are stale (``{"asset_id", "reason"}``), and how many
    have no build record (exported before tracking). Any library build
    re-exports the stale and untracked ones.

    Only assets in the library ``index.json`` count: the last library
    build managed those. GLBs outside it belong to ships no longer in the
    workspace; no build touches them, so they're reported as ``unused``."""
    cfg = config or PipelineConfig.load()
    out: dict[str, Any] = {"current": 0, "stale": [], "untracked": 0, "unused": 0}
    root = cfg.workspace / "libraries" / "accessories"
    if not root.is_dir():
        return out
    managed: set[str] | None = None
    try:
        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        if isinstance(index.get("assets"), dict):
            managed = set(index["assets"])
    except (OSError, ValueError):
        managed = None
    tracker = (
        LibraryStaleness(manifest, cfg) if manifest is not None
        else LibraryStaleness.for_current_build(cfg)
    )
    for glb in sorted(root.rglob("*.glb")):
        # Library layout: <asset_id>/<asset_id>.glb (+ <asset_id>_dead.glb).
        if glb.parent.name != glb.stem:
            continue
        if managed is not None and glb.stem not in managed:
            out["unused"] += 1
            continue
        try:
            doc = json.loads((glb.parent / DEPS_FILENAME).read_text(encoding="utf-8"))
        except FileNotFoundError:
            out["untracked"] += 1
            continue
        except (OSError, ValueError):
            doc = {}
        asset_dir = doc.get("asset_dir")
        why = (
            tracker.reason(glb.parent, asset_id=glb.stem, asset_dir=asset_dir)
            if isinstance(asset_dir, str) else "unreadable build record"
        )
        if why is None:
            out["current"] += 1
        else:
            out["stale"].append({"asset_id": glb.stem, "reason": why})
    return out


def refresh_fingerprints(build: int, *, config: PipelineConfig | None = None) -> int:
    """Re-take the stored fingerprints of every export (and library asset)
    made on ``build`` from that build's saved manifest. Run after
    rebuilding a manifest with new hashing, so the stored fingerprints use
    the same scheme. Returns the number of deps files updated."""
    cfg = config or PipelineConfig.load()
    if build not in _bm.stored_builds(cfg):
        return 0
    manifest = _bm.load_manifest(build, config=cfg, create=False)
    updated = 0
    deps_files = [d / DEPS_FILENAME for _kind, _name, d, _meta in _export_dirs(cfg)]
    deps_files += list(library_deps_files(cfg))
    for path in deps_files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("build") != build:
            continue
        doc["inputs"] = {
            section: {key: manifest.fingerprint(section, key) for key in entries}
            for section, entries in (doc.get("inputs") or {}).items()
            if section in _bm.SECTIONS
        }
        doc["schemes"] = dict(manifest.schemes)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        updated += 1
    return updated


def _export_dirs(config: PipelineConfig) -> Iterator[tuple[str, str, Path, Path | None]]:
    """(kind, name, dir, metadata file) for every export in the workspace."""
    ws = config.workspace
    ships = ws / "ships"
    if ships.is_dir():
        for d in sorted(ships.iterdir()):
            meta = d / f"{d.name}.meta.json"
            if meta.is_file():
                yield "ship", d.name, d, meta
    maps = ws / "maps"
    if maps.is_dir():
        for d in sorted(maps.iterdir()):
            meta = d / "export.json"
            if meta.is_file():
                yield "map", d.name, d, meta


def _exported_at(kind: str, meta: Path | None) -> str | None:
    if meta is None:
        return None
    try:
        doc = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if kind == "ship":
        return (doc.get("provenance") or {}).get("extracted_at") or (doc.get("pipeline") or {}).get("exported_at")
    return doc.get("generated_at")


def _ship_reexport_from_meta(meta: Path) -> dict[str, Any]:
    try:
        doc = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    args = (doc.get("provenance") or {}).get("extract_args") or {}
    return {"kind": "ship", **args} if args else {}


def _map_reexport_from_meta(meta: Path) -> dict[str, Any]:
    try:
        doc = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        "kind": "map",
        "name": doc.get("name") or meta.parent.name,
        "flags": doc.get("flags") or {},
        "collision_manifest": doc.get("collision_manifest_size") is not None,
    }


def _removed_from_build(
    kind: str, name: str, meta: Path | None, reexport: dict[str, Any], manifest: _bm.BuildManifest,
) -> str | None:
    """What the export was made from, if ``manifest``'s build no longer
    has it: a map whose space is gone (event maps are dropped from the
    game) or a ship whose GameParams entity is gone."""
    if kind == "map":
        space = reexport.get("name") or name
        if manifest.files and space not in manifest.space_names:
            return f"space {space!r}"
        return None
    if kind == "ship" and meta is not None and manifest.gameparams:
        try:
            ship = json.loads(meta.read_text(encoding="utf-8")).get("ship") or {}
        except (OSError, ValueError):
            return None
        entity = ship.get("wg_ship_full_id")
        if isinstance(entity, str) and entity and entity not in manifest.gameparams:
            return f"GameParams entity {entity!r}"
    return None


def export_status(
    kind: str, name: str, out_dir: Path, meta: Path | None, manifest: _bm.BuildManifest,
    base_manifest: Any = None,
) -> ExportStatus:
    """State of one export against ``manifest``. ``base_manifest(build)``
    returns the saved manifest of an older build, or ``None``."""
    deps_path = out_dir / DEPS_FILENAME
    exported_at = _exported_at(kind, meta)
    fallback_reexport = (
        _ship_reexport_from_meta(meta) if kind == "ship" and meta
        else _map_reexport_from_meta(meta) if kind == "map" and meta
        else {}
    )
    gone = _removed_from_build(kind, name, meta, fallback_reexport, manifest)
    if gone:
        build = None
        if deps_path.is_file():
            try:
                build = json.loads(deps_path.read_text(encoding="utf-8")).get("build")
            except (OSError, ValueError):
                pass
        return ExportStatus(
            kind=kind, name=name, path=str(out_dir), state="removed", build=build,
            exported_at=exported_at, reexport=fallback_reexport,
            note=f"{gone} is not in build {manifest.build}",
        )
    if not deps_path.is_file():
        return ExportStatus(
            kind=kind, name=name, path=str(out_dir), state="untracked",
            exported_at=exported_at, reexport=fallback_reexport,
            note="exported before build tracking; re-export once to start tracking",
        )
    try:
        doc = json.loads(deps_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return ExportStatus(
            kind=kind, name=name, path=str(out_dir), state="error",
            exported_at=exported_at, reexport=fallback_reexport, note=f"unreadable {DEPS_FILENAME}: {err}",
        )
    build = doc.get("build")
    reexport = doc.get("reexport") or fallback_reexport
    status = ExportStatus(
        kind=kind, name=name, path=str(out_dir), state="current", build=build,
        exported_at=exported_at or doc.get("created_at"), counts=doc.get("counts") or {},
        reexport=reexport,
    )
    if build == manifest.build:
        return status
    base = base_manifest(build) if base_manifest and isinstance(build, int) else None
    changed, removed, incomparable = check_deps(doc, manifest, base)
    status.changed, status.removed, status.incomparable = changed, removed, incomparable
    if status.changed_count:
        status.state = "stale"
    if incomparable:
        status.note = (
            f"hashing changed since build {build} and its manifest is gone; "
            f"not checked: {', '.join(incomparable)}"
        )
    return status


def workspace_status(
    *, config: PipelineConfig | None = None, build: int | None = None,
) -> tuple[_bm.BuildManifest, list[ExportStatus]]:
    """Status of every ship and map export against ``build`` (default: current)."""
    cfg = config or PipelineConfig.load()
    manifest = _bm.load_manifest(build, config=cfg)
    stored = set(_bm.stored_builds(cfg))

    def base_manifest(b: int) -> _bm.BuildManifest | None:
        if b not in stored or b == manifest.build:
            return None
        return _bm.load_manifest(b, config=cfg, create=False)

    out = [
        export_status(kind, name, d, meta, manifest, base_manifest)
        for kind, name, d, meta in _export_dirs(cfg)
    ]
    return manifest, out


# ---------------------------------------------------------------------------
# Re-export
# ---------------------------------------------------------------------------


def _ship_ingest_plan(
    status: ExportStatus, cfg: PipelineConfig, model_dir_index: dict[str, str] | None,
) -> tuple[dict[str, Any], tuple[Any, ...], bool, str]:
    """``ingest_ship`` kwargs, skin packs to replay, library intent, and
    where the plan came from, for one ship.

    Tracked ships replay the exact kwargs stored in ``build_deps.json``.
    Untracked ships fall back to the sidecar's ``provenance.extract_args``
    (the same plan ``compose.clean_and_reextract`` uses). Skin packs always
    come from the sidecar: a re-ingest rebuilds ``skins[]`` from scratch,
    so packs ingested on top of the ship have to be ingested again."""
    # compose/__init__ re-exports functions named like their modules, so
    # import the names, not the modules.
    from .clean_workspace import _load_model_dir_index, _plan_from_sidecar

    sidecar_path = Path(status.path) / f"{status.name}.meta.json"
    plan = _plan_from_sidecar(sidecar_path)
    skins = plan.skins if plan else ()
    rx = status.reexport or {}
    if rx.get("ship_input"):
        kwargs = {
            "ship_input": rx["ship_input"],
            "forced_label": rx.get("label") or status.name,
            "variant_permoflage": rx.get("permoflage", "auto"),
            "export_exterior_hulls": bool(rx.get("export_exterior_hulls")),
            "toolkit_ship_override": rx.get("toolkit_ship_override"),
            "gameparams_ship_id": rx.get("gameparams_ship_id"),
            "hull": rx.get("hull"),
            "class_override": rx.get("class_override"),
            "ship_key_suffix": rx.get("ship_key_suffix"),
        }
        return kwargs, skins, bool(rx.get("build_library")), "build_deps"
    if plan is None:
        raise ValueError(
            f"{status.name}: no {DEPS_FILENAME} and no usable sidecar to replay from"
        )
    model_dir = plan.model_dir
    if model_dir is None:
        if model_dir_index is None:
            model_dir_index = _load_model_dir_index(cfg.workspace, cfg)
        model_dir = model_dir_index.get(plan.vehicle)
    kwargs = {
        "ship_input": model_dir or plan.vehicle,
        "forced_label": plan.label,
        "variant_permoflage": plan.permoflage or "auto",
        "toolkit_ship_override": model_dir,
        "gameparams_ship_id": plan.vehicle,
        "hull": plan.hull,
    }
    return kwargs, skins, plan.build_library, f"sidecar ({plan.provenance_source})"


def reexport_plan(status: ExportStatus, config: PipelineConfig | None = None) -> dict[str, Any]:
    """What :func:`reexport_exports` would run for ``status`` (dry run)."""
    cfg = config or PipelineConfig.load()
    if status.kind == "ship":
        kwargs, skins, library, source = _ship_ingest_plan(status, cfg, None)
        return {
            "kind": "ship", "name": status.name, "source": source,
            "ingest_ship": kwargs, "build_library": library,
            "skin_packs": [s.skin_id for s in skins],
        }
    rx = status.reexport or {}
    return {
        "kind": "map", "name": status.name,
        "run_export": {
            "name": rx.get("name") or status.name,
            "flags": rx.get("flags") or {},
            "collision_manifest": bool(rx.get("collision_manifest")),
        },
    }


def library_refresh_spec(status: ExportStatus, config: PipelineConfig) -> dict[str, Any]:
    """The post-library sidecar refresh spec for a ship export (see
    :func:`~.ingest_ship.build_library_and_refresh`)."""
    kwargs = reexport_plan(status, config).get("ingest_ship") or {}
    return {
        "label": status.name,
        "toolkit_ship": kwargs.get("toolkit_ship_override") or kwargs.get("ship_input"),
        "gameparams_ship_id": kwargs.get("gameparams_ship_id"),
        "permoflage": kwargs.get("variant_permoflage") or "auto",
    }


DEFAULT_REEXPORT_JOBS = 3


def prewarm_caches(cfg: PipelineConfig, *, ships: bool) -> None:
    """Bring the shared caches up to the current build before exports run
    in parallel, so workers find them current instead of each rebuilding
    the same file (gameparams.json, assets.bin, camouflages.xml, the VFS
    manifest, the skel_ext hash table, the particle library). With
    ``ships``, also parse GameParams into memory once."""
    from ..read import gameparams as _gp_read
    from ..resolve import camo as _camo
    from ..resolve import skel_ext_hashes as _sxh
    from ..toolkit import assets_bin as _assets_bin
    from ..toolkit import vfs as _vfs
    from ..toolkit._subprocess import toolkit_capabilities
    from . import library_particles as _lib_particles

    toolkit_capabilities(cfg.require_toolkit_bin())
    _assets_bin.ensure_dump(config=cfg)
    _vfs.ensure_manifest(config=cfg)
    _camo.ensure_camouflages_xml(config=cfg)
    _sxh.load_or_build(cache=cfg.require_cache_dir() / "skel_ext_hashes.json", config=cfg)
    if ships:
        _gp_read.load_full(config=cfg)
        _lib_particles.ensure_built(config=cfg)


def _labelled(on_event: Any, name: str) -> Any:
    """``on_event`` with every event tagged with the export it came from
    (events from parallel exports interleave)."""
    from dataclasses import replace

    def emit(ev: Any) -> None:
        on_event(replace(
            ev,
            detail=f"[{name}] {ev.detail}" if ev.detail else f"[{name}]",
            data={**(ev.data or {}), "export": name},
        ))

    return emit


def reexport_exports(
    statuses: list[ExportStatus],
    *,
    config: PipelineConfig | None = None,
    replay_skins: bool = True,
    build_library: bool = True,
    jobs: int = 1,
    on_event: Any = None,
    cancel: Any = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Re-run the exports behind ``statuses`` on the current build.

    Ships go through ``compose.ingest_ship`` (then their skin packs), maps
    through ``maps_export.run_export``; both write a fresh
    ``build_deps.json``. Ships that asked for the accessory library get it
    built once at the end, as the extract queue does. Per-export failures
    are collected, not raised. ``on_progress(index, total, status)`` is
    called as each export starts.

    ``jobs`` > 1 runs that many exports at once. Each export writes only
    its own folder; the shared caches are brought current first and the
    shared libraries they extract into are locked. The one cross-folder
    writer is the shared texture store's adoption, and it leaves the
    folders of the exports still running alone: each ingest and skin pack
    claims its ship root through ``shared_textures.export_session``, and
    the copies another export's adoption left there move once that
    session ends. A ship's skin packs run in the same worker, after the
    ship."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ..errors import CancelledError
    from .clean_workspace import _load_model_dir_index
    from .ingest_ship import build_library_and_refresh, ingest_ship
    from .skin_pack import ingest_skin_pack

    cfg = config or PipelineConfig.load()
    report: dict[str, Any] = {
        "reexported": [], "failed": [], "skipped": [], "skins_replayed": [], "library": None,
    }
    for st in statuses:
        if st.state == "removed":
            report["skipped"].append({"kind": st.kind, "name": st.name, "reason": st.note})
    statuses = [st for st in statuses if st.state != "removed"]
    total = len(statuses)
    jobs = max(1, min(int(jobs or 1), total or 1))
    report["jobs"] = jobs

    model_dir_index: dict[str, str] | None = None
    if any(st.kind == "ship" and not (st.reexport or {}).get("ship_input") for st in statuses):
        model_dir_index = _load_model_dir_index(cfg.workspace, cfg)
    if jobs > 1:
        prewarm_caches(cfg, ships=any(st.kind == "ship" for st in statuses))

    lock = threading.Lock()
    stop = threading.Event()
    library_specs: list[dict[str, Any]] = []
    started = 0

    def record(key: str, entry: dict[str, Any]) -> None:
        with lock:
            report[key].append(entry)

    def run_one(st: ExportStatus) -> None:
        nonlocal started
        if stop.is_set():
            return
        with lock:
            index = started
            started += 1
            if on_progress is not None:
                on_progress(index, total, st)
        emit = _labelled(on_event, st.name) if jobs > 1 and on_event is not None else on_event
        try:
            if st.kind == "ship":
                kwargs, skins, wants_library, source = _ship_ingest_plan(st, cfg, model_dir_index)
                ship_input = kwargs.pop("ship_input")
                result = ingest_ship(
                    ship_input,
                    config=cfg,
                    interactive=False,
                    build_library=False,
                    provenance_build_library=wants_library,
                    on_event=emit,
                    cancel=cancel,
                    **{k: v for k, v in kwargs.items() if v is not None},
                )
                record("reexported", {"kind": "ship", "name": st.name, "plan": source})
                if wants_library:
                    record_spec = {
                        "label": result.label,
                        "toolkit_ship": kwargs.get("toolkit_ship_override") or ship_input,
                        "gameparams_ship_id": kwargs.get("gameparams_ship_id"),
                        "permoflage": kwargs.get("variant_permoflage") or "auto",
                    }
                    with lock:
                        library_specs.append(record_spec)
                if replay_skins:
                    for skin in skins:
                        try:
                            ingest_skin_pack(
                                skin.skin_source,
                                ship_id=result.label,
                                config=cfg,
                                skin_id=skin.skin_id,
                                display_name=skin.display_name,
                                source_kind=skin.source_kind,
                                on_event=emit,
                                cancel=cancel,
                            )
                            record("skins_replayed", {"ship": result.label, "skin_id": skin.skin_id})
                        except CancelledError:
                            raise
                        except Exception as err:  # noqa: BLE001
                            record("failed", {
                                "kind": "skin", "name": f"{result.label}::{skin.skin_id}",
                                "error": f"{type(err).__name__}: {err}",
                            })
            elif st.kind == "map":
                from ..maps_export import run_export

                plan = reexport_plan(st, cfg)["run_export"]
                run_export(
                    plan["name"],
                    config=cfg,
                    flags=plan["flags"],
                    collision_manifest=plan["collision_manifest"],
                )
                record("reexported", {"kind": "map", "name": st.name})
            else:
                raise ValueError(f"unknown export kind {st.kind!r}")
        except CancelledError:
            stop.set()
            raise
        except Exception as err:  # noqa: BLE001
            record("failed", {
                "kind": st.kind, "name": st.name, "error": f"{type(err).__name__}: {err}",
            })

    if jobs == 1:
        for st in statuses:
            run_one(st)
    else:
        cancelled: CancelledError | None = None
        with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="reexport") as pool:
            futures = [pool.submit(run_one, st) for st in statuses]
            for fut in futures:
                try:
                    fut.result()
                except CancelledError as err:
                    cancelled = cancelled or err
        if cancelled is not None:
            raise cancelled

    if build_library and not library_specs and not stop.is_set():
        # Nothing re-exported here wants a library build, but the game
        # update may still have changed shared library assets (turrets,
        # directors, ...). Rebuild those, then refresh every ship's sidecar
        # (mesh-swap bone corrections are read from the library GLBs).
        try:
            lib = library_status(cfg)
            if lib["stale"] or lib["untracked"]:
                _, all_statuses = workspace_status(config=cfg)
                library_specs = [
                    library_refresh_spec(s, cfg) for s in all_statuses
                    if s.kind == "ship" and s.state != "removed"
                ]
        except Exception as err:  # noqa: BLE001 - the check never fails a re-export
            report["library"] = {"ok": False, "error": f"library check failed: {err}"}

    if build_library and library_specs:
        try:
            build_library_and_refresh(
                workspace=cfg.workspace, config=cfg, refresh_specs=library_specs,
                on_event=on_event, cancel=cancel,
            )
            report["library"] = {"ships": len(library_specs), "ok": True}
        except CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            report["library"] = {
                "ships": len(library_specs), "ok": False,
                "error": f"{type(err).__name__}: {err}",
            }
    return report
