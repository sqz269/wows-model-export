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


def _strings(o: Any) -> Iterator[str]:
    stack = [o]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            yield cur
        elif isinstance(cur, dict):
            for k, v in cur.items():
                if isinstance(k, str):
                    yield k
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)


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
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        for s in _strings(doc):
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
    state: str  # current | stale | untracked | error
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


def refresh_fingerprints(build: int, *, config: PipelineConfig | None = None) -> int:
    """Re-take the stored fingerprints of every export made on ``build``
    from that build's saved manifest. Run after rebuilding a manifest with
    new hashing, so the exports' fingerprints use the same scheme. Returns
    the number of deps files updated."""
    cfg = config or PipelineConfig.load()
    if build not in _bm.stored_builds(cfg):
        return 0
    manifest = _bm.load_manifest(build, config=cfg, create=False)
    updated = 0
    for _kind, _name, d, _meta in _export_dirs(cfg):
        path = d / DEPS_FILENAME
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


def reexport_exports(
    statuses: list[ExportStatus],
    *,
    config: PipelineConfig | None = None,
    replay_skins: bool = True,
    build_library: bool = True,
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
    called before each export starts."""
    from ..errors import CancelledError
    from .clean_workspace import _load_model_dir_index
    from .ingest_ship import build_library_and_refresh, ingest_ship
    from .skin_pack import ingest_skin_pack

    cfg = config or PipelineConfig.load()
    report: dict[str, Any] = {
        "reexported": [], "failed": [], "skins_replayed": [], "library": None,
    }
    library_specs: list[dict[str, Any]] = []
    model_dir_index: dict[str, str] | None = None
    total = len(statuses)

    for i, st in enumerate(statuses):
        if on_progress is not None:
            on_progress(i, total, st)
        try:
            if st.kind == "ship":
                if model_dir_index is None and not (st.reexport or {}).get("ship_input"):
                    model_dir_index = _load_model_dir_index(cfg.workspace, cfg)
                kwargs, skins, wants_library, source = _ship_ingest_plan(st, cfg, model_dir_index)
                ship_input = kwargs.pop("ship_input")
                result = ingest_ship(
                    ship_input,
                    config=cfg,
                    interactive=False,
                    build_library=False,
                    provenance_build_library=wants_library,
                    on_event=on_event,
                    cancel=cancel,
                    **{k: v for k, v in kwargs.items() if v is not None},
                )
                report["reexported"].append({"kind": "ship", "name": st.name, "plan": source})
                if wants_library:
                    library_specs.append({
                        "label": result.label,
                        "toolkit_ship": kwargs.get("toolkit_ship_override") or ship_input,
                        "gameparams_ship_id": kwargs.get("gameparams_ship_id"),
                        "permoflage": kwargs.get("variant_permoflage") or "auto",
                    })
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
                                on_event=on_event,
                                cancel=cancel,
                            )
                            report["skins_replayed"].append(
                                {"ship": result.label, "skin_id": skin.skin_id}
                            )
                        except CancelledError:
                            raise
                        except Exception as err:  # noqa: BLE001
                            report["failed"].append({
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
                report["reexported"].append({"kind": "map", "name": st.name})
            else:
                raise ValueError(f"unknown export kind {st.kind!r}")
        except CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            report["failed"].append({
                "kind": st.kind, "name": st.name, "error": f"{type(err).__name__}: {err}",
            })

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
