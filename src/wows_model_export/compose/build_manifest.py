"""Per-build fingerprint of the game's content, and build-to-build diffs.

A build manifest lists a fingerprint for every input an export can read,
for one game build:

- ``files``: every file in the game packages, as ``[crc32, size]``,
  straight from the ``.idx`` files (no package reads).
- ``records``: every record the toolkit synthesises from ``assets.bin``
  (``.visual``, ``.model``, ``.mfm``, ``.skel_ext``, particles, ...), as
  ``[hash, kind, type]``. These have no CRC in the idx, so the toolkit
  hashes their content (``metadata --content-hashes``). Particle records
  (``effect`` / ``effect_metadata``) are re-hashed here from the pipeline's
  own decoder, since the toolkit can only hash their fixed bytes.
- ``gameparams``: one hash per default-realm GameParams entity
  (``game-params --entity-hashes``).
- ``camouflages``: one hash per named entry of ``camouflages.xml``
  (``camouflage:<name>`` over every entry sharing the name,
  ``colorScheme:<name>``, ``shipgroup:<name>``). The file itself changes
  every build; its entries mostly don't.

Manifests are cached at ``<cache_dir>/builds/<build>/manifest.json.gz`` of
the root workspace (shared by its per-build workspaces).
The launcher deletes a build's idx files one update later, so the pipeline
snapshots the current build's manifest whenever it is first needed; diffs
against older builds read those snapshots.

Cost: ~5 s for files + records, ~5 s for particles, ~40 s for GameParams,
~2 s for camouflages.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import mmap
import os
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..toolkit import build as _build

MANIFEST_SCHEMA = "wows.build_manifest/v1"
DIFF_SCHEMA = "wows.build_diff/v1"

# Hash schemes of the sections the pipeline computes itself. Bump when the
# hashing changes: manifests (and export fingerprints) made with different
# schemes can't be compared, and are reported as such instead of as
# "everything changed".
PARTICLE_HASH_SCHEME = "wows.particle_hashes/v1"
CAMOUFLAGE_HASH_SCHEME = "wows.camouflage_hashes/v1"

SECTIONS = ("files", "records", "gameparams", "camouflages")

# Schemes of manifests written before schemes were recorded.
LEGACY_SCHEMES = {
    "files": "idx.crc32+size",
    "records": "wowsunpack.content_hashes/v1+" + PARTICLE_HASH_SCHEME,
    "gameparams": "wowsunpack.gameparams_hashes/v1",
    "camouflages": CAMOUFLAGE_HASH_SCHEME,
}

PARTICLE_TYPES = ("effect", "effect_metadata")


def builds_dir(config: PipelineConfig) -> Path:
    """Per-build manifests. They live in the root workspace's cache, which
    every build workspace of it shares (``PipelineConfig.root_cache_dir``)."""
    root = config.root_cache_dir
    root.mkdir(parents=True, exist_ok=True)
    return root / "builds"


def manifest_path(config: PipelineConfig, build: int) -> Path:
    return builds_dir(config) / str(build) / "manifest.json.gz"


def stored_builds(config: PipelineConfig) -> list[int]:
    """Builds with a saved manifest, oldest first."""
    root = builds_dir(config)
    if not root.is_dir():
        return []
    return sorted(
        int(p.name) for p in root.iterdir()
        if p.name.isdigit() and (p / "manifest.json.gz").is_file()
    )


@dataclass
class BuildManifest:
    build: int
    files: dict[str, list[int]] = field(default_factory=dict)
    records: dict[str, list[str]] = field(default_factory=dict)
    gameparams: dict[str, str] = field(default_factory=dict)
    camouflages: dict[str, str] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    # Section -> hash scheme (see ``hash_schemes``). Empty on manifests
    # written before schemes were recorded.
    schemes: dict[str, str] = field(default_factory=dict)

    # -- fingerprints -----------------------------------------------------

    def scheme(self, section: str) -> str:
        return (self.schemes or LEGACY_SCHEMES).get(section) or LEGACY_SCHEMES[section]

    def fingerprint(self, section: str, key: str) -> Any:
        """Fingerprint of ``key`` in ``section`` (``None`` if absent)."""
        if section == "files":
            return self.file_fp(key)
        if section == "records":
            return self.record_fp(key)
        if section == "gameparams":
            return self.gameparams_fp(key)
        if section == "camouflages":
            return self.camouflage_fp(key)
        raise KeyError(section)

    def file_fp(self, path: str) -> list[int] | None:
        return self.files.get(path)

    def record_fp(self, path: str) -> str | None:
        rec = self.records.get(path)
        return rec[0] if rec else None

    def gameparams_fp(self, entity: str) -> str | None:
        return self.gameparams.get(entity)

    def camouflage_fp(self, key: str) -> str | None:
        return self.camouflages.get(key)

    def particle_records(self, base: str) -> dict[str, str]:
        """Fingerprints of every record under a particle ``.xml`` path
        (``/particles/x.xml`` -> ``/particles/x.xml/high`` etc.)."""
        return dict(self._particle_index.get(base.rstrip("/"), {}))

    @cached_property
    def _particle_index(self) -> dict[str, dict[str, str]]:
        """Particle records grouped by their ``.xml`` path."""
        index: dict[str, dict[str, str]] = {}
        for p, r in self.records.items():
            if r[2] not in PARTICLE_TYPES:
                continue
            i = p.find(".xml/")
            if i >= 0:
                index.setdefault(p[:i + 4], {})[p] = r[0]
        return index

    @cached_property
    def space_names(self) -> frozenset[str]:
        """Names of the map spaces this build ships (``/spaces/<name>/``)."""
        return frozenset(
            p.split("/", 3)[2] for p in self.files
            if p.startswith("/spaces/") and p.count("/") >= 3
        )

    def asset_sources(self, asset_dir: str, asset_id: str) -> tuple[list[str], list[str]]:
        """The files and records a model asset is exported from: everything
        in its folder (``.../<asset_id>/``: geometry, visual, model,
        skel_ext, the ``_dead`` variant) plus its ``<asset_id>.*`` /
        ``<asset_id>_*`` entries in the sibling ``textures/`` folder
        (material files and texture maps)."""
        asset_dir = "/" + asset_dir.strip("/")
        textures_dir = asset_dir.rsplit("/", 1)[0] + "/textures"
        files: list[str] = []
        records: list[str] = []
        for path in self._paths_by_dir.get(asset_dir, ()):
            (files if path in self.files else records).append(path)
        for path in self._paths_by_dir.get(textures_dir, ()):
            base = path.rsplit("/", 1)[1]
            if base.startswith((asset_id + ".", asset_id + "_")):
                (files if path in self.files else records).append(path)
        return sorted(files), sorted(records)

    @cached_property
    def _paths_by_dir(self) -> dict[str, list[str]]:
        """Every file and record path, grouped by its parent folder."""
        index: dict[str, list[str]] = {}
        for paths in (self.files, self.records):
            for p in paths:
                index.setdefault(p[:p.rfind("/")], []).append(p)
        return index


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def hash_schemes(cfg: PipelineConfig) -> dict[str, str]:
    """The hash scheme of every manifest section, for the current toolkit.

    ``records`` covers both the toolkit's record hashes and the
    pipeline's particle hashes layered over them."""
    from ..toolkit._subprocess import toolkit_capabilities

    caps = toolkit_capabilities(cfg.require_toolkit_bin()).get("schemas") or {}
    return {
        "files": "idx.crc32+size",
        "records": "+".join((
            caps.get("metadata.content_hashes") or "wowsunpack.content_hashes/v1",
            PARTICLE_HASH_SCHEME,
        )),
        "gameparams": caps.get("game_params.entity_hashes") or "wowsunpack.gameparams_hashes/v1",
        "camouflages": CAMOUFLAGE_HASH_SCHEME,
    }


def _log(msg: str) -> None:
    print(f"[build-manifest] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _strip_record_index(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _strip_record_index(v) for k, v in o.items() if k != "record_index"}
    if isinstance(o, list):
        return [_strip_record_index(v) for v in o]
    return o


def particle_hashes(assets_bin: Path) -> dict[str, str]:
    """Hash every particle record from the pipeline's decoded form.

    Keys are manifest paths (``/particles/<name>.xml/<quality>``). The
    decoded dict drops ``record_index`` so a record keeps its hash when
    others are inserted before it."""
    from ..read import particles as P

    out: dict[str, str] = {}
    with open(assets_bin, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            hdr = P._read_header(mm)
            eff = next(
                (i for i, b in enumerate(hdr.blobs) if b.prototype_magic == P.EFFECT_BLOB_MAGIC),
                None,
            )
            meta = next(
                (i for i, b in enumerate(hdr.blobs)
                 if b.prototype_magic == P.EFFECT_METADATA_BLOB_MAGIC),
                None,
            )
            by_id = P._build_paths_by_self_id(hdr.paths)
            end = len(mm)
            for entry in hdr.paths:
                if entry.self_id == 0 or not entry.name:
                    continue
                loc = P._r2p_lookup(mm, hdr, entry.self_id)
                if loc is None or loc[0] not in (eff, meta):
                    continue
                full = P._reconstruct_full_path(by_id, entry)
                if not full:
                    continue
                if loc[0] == eff:
                    doc = P._decode_effect_record(mm, hdr.blobs[eff].data_offset, loc[1], end)
                else:
                    doc = P._decode_effect_metadata_record(
                        mm, hdr.blobs[meta].data_offset, loc[1], end
                    )
                blob = json.dumps(
                    _strip_record_index(doc), sort_keys=True, separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                out["/" + full] = hashlib.blake2b(blob, digest_size=8).hexdigest()
        finally:
            mm.close()
    return out


def camouflage_hashes(xml_path: Path) -> dict[str, str]:
    """Hash ``camouflages.xml`` per named entry.

    Several ``<camouflage>`` elements share a name (one per ship group or
    realm); they are hashed together, in file order."""
    import xml.etree.ElementTree as ET

    root = ET.parse(xml_path).getroot()
    groups: dict[str, list[bytes]] = defaultdict(list)
    for section in root:
        tag = section.tag
        for el in section:
            if tag == "camouflages.xml":
                key = "camouflage:" + (el.findtext("name") or "").strip()
            elif tag == "colorschemes.xml":
                key = "colorScheme:" + (el.findtext("name") or "").strip()
            elif tag == "shipgroups.xml":
                key = "shipgroup:" + el.tag
            else:
                key = f"{tag}:{el.tag}"
            groups[key].append(ET.tostring(el, encoding="utf-8"))
    return {
        k: hashlib.blake2b(b"\0".join(v), digest_size=8).hexdigest()
        for k, v in groups.items()
    }


def _camouflages_xml_for(build: int, current: int | None, cfg: PipelineConfig, tmp: Path) -> Path:
    if build == current:
        from ..resolve import camo as _camo

        return _camo.ensure_camouflages_xml(config=cfg)
    out = _build.extract_from_build(["*camouflages.xml"], tmp, build=build, config=cfg)
    hits = [p for p in out.rglob("camouflages.xml")]
    if not hits:
        raise FileNotFoundError(f"camouflages.xml not extracted for build {build}")
    return hits[0]


def _assets_bin_for(build: int, current: int | None, cfg: PipelineConfig, tmp: Path) -> Path:
    if build == current:
        from ..toolkit import assets_bin as _assets_bin

        return _assets_bin.ensure_dump(config=cfg)
    out = _build.extract_from_build(["*content/assets.bin"], tmp, build=build, config=cfg)
    hits = sorted(out.rglob("assets.bin"), key=lambda p: p.stat().st_size)
    if not hits:
        raise FileNotFoundError(f"assets.bin not extracted for build {build}")
    return hits[-1]


def build_manifest(build: int | None = None, *, config: PipelineConfig | None = None) -> Path:
    """Compute and save the manifest for ``build`` (default: current)."""
    cfg = config or PipelineConfig.load()
    current = _build.current_build(cfg)
    build = build or current
    if build is None:
        raise FileNotFoundError("no game build with idx files found")
    out = manifest_path(cfg, build)
    out.parent.mkdir(parents=True, exist_ok=True)
    sections: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix=f"wms-manifest-{build}-") as td:
        tmp = Path(td)
        t0 = time.perf_counter()
        meta_json = tmp / "metadata.json"
        _build.metadata_hashed(meta_json, build=None if build == current else build, config=cfg)
        files: dict[str, list[int]] = {}
        records: dict[str, list[str]] = {}
        with open(meta_json, encoding="utf-8") as f:
            entries = json.load(f)
        for e in entries:
            if e.get("is_directory"):
                continue
            path = e["path"]
            if e.get("source") == "assets_bin":
                if e.get("content_hash"):
                    records[path] = [e["content_hash"], e.get("hash_kind") or "", e.get("prototype_type") or ""]
            else:
                files[path] = [int(e.get("crc32") or 0), int(e.get("unpacked_size") or 0)]
        del entries
        sections["files"] = "ok"
        sections["records"] = "ok"
        _log(f"build {build}: {len(files)} files, {len(records)} records "
             f"({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        try:
            ab = _assets_bin_for(build, current, cfg, tmp / "ab")
            for path, h in particle_hashes(ab).items():
                rec = records.get(path)
                records[path] = [h, "decoded", rec[2] if rec else "effect"]
            sections["particles"] = "decoded"
            _log(f"build {build}: particles decoded ({time.perf_counter() - t0:.1f}s)")
        except Exception as err:  # noqa: BLE001 - older build without packages
            sections["particles"] = f"shallow ({type(err).__name__}: {err})"
            _log(f"build {build}: particle decode unavailable: {err}")

        t0 = time.perf_counter()
        gameparams: dict[str, str] = {}
        gp_schema = ""
        try:
            gp_json = tmp / "gameparams_hashes.json"
            _build.gameparams_entity_hashes(
                gp_json, build=None if build == current else build, config=cfg
            )
            doc = json.loads(gp_json.read_text(encoding="utf-8"))
            gp_schema = doc.get("schema") or ""
            # Default realm only: the pipeline never reads realm overrides.
            gameparams = {k: v for k, v in (doc.get("entities") or {}).items() if ":" not in k}
            sections["gameparams"] = "ok"
            _log(f"build {build}: {len(gameparams)} GameParams entities "
                 f"({time.perf_counter() - t0:.1f}s)")
        except Exception as err:  # noqa: BLE001
            sections["gameparams"] = f"unavailable ({type(err).__name__}: {err})"
            _log(f"build {build}: GameParams hashes unavailable: {err}")

        t0 = time.perf_counter()
        camouflages: dict[str, str] = {}
        try:
            camouflages = camouflage_hashes(_camouflages_xml_for(build, current, cfg, tmp / "camo"))
            sections["camouflages"] = "ok"
            _log(f"build {build}: {len(camouflages)} camouflages.xml entries "
                 f"({time.perf_counter() - t0:.1f}s)")
        except Exception as err:  # noqa: BLE001
            sections["camouflages"] = f"unavailable ({type(err).__name__}: {err})"
            _log(f"build {build}: camouflages.xml hashes unavailable: {err}")

    schemes = hash_schemes(cfg)
    if gp_schema:
        schemes["gameparams"] = gp_schema
    doc = {
        "schema": MANIFEST_SCHEMA,
        "build": build,
        "created_at": _now(),
        "sections": sections,
        "schemes": schemes,
        "files": files,
        "records": records,
        "gameparams": gameparams,
        "camouflages": camouflages,
    }
    tmp_out = out.with_suffix(".tmp")
    with gzip.open(tmp_out, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(doc, f, separators=(",", ":"))
    os.replace(tmp_out, out)
    _MANIFEST_CACHE.pop(build, None)
    return out


def ensure_manifest(
    build: int | None = None, *, config: PipelineConfig | None = None, refresh: bool = False
) -> Path:
    """Path to ``build``'s manifest, building it first if needed."""
    cfg = config or PipelineConfig.load()
    build = build or _build.current_build(cfg)
    if build is None:
        raise FileNotFoundError("no game build with idx files found")
    path = manifest_path(cfg, build)
    if path.is_file() and not refresh:
        return path
    return build_manifest(build, config=cfg)


_MANIFEST_CACHE: dict[int, tuple[float, BuildManifest]] = {}


def load_manifest(
    build: int | None = None, *, config: PipelineConfig | None = None, create: bool = True
) -> BuildManifest:
    """Load (and cache per process) ``build``'s manifest."""
    cfg = config or PipelineConfig.load()
    build = build or _build.current_build(cfg)
    if build is None:
        raise FileNotFoundError("no game build with idx files found")
    path = ensure_manifest(build, config=cfg) if create else manifest_path(cfg, build)
    mtime = path.stat().st_mtime
    hit = _MANIFEST_CACHE.get(build)
    if hit and hit[0] == mtime:
        return hit[1]
    with gzip.open(path, "rt", encoding="utf-8") as f:
        doc = json.load(f)
    m = BuildManifest(
        build=int(doc["build"]),
        files=doc.get("files") or {},
        records=doc.get("records") or {},
        gameparams=doc.get("gameparams") or {},
        camouflages=doc.get("camouflages") or {},
        sections=doc.get("sections") or {},
        created_at=doc.get("created_at") or "",
        schemes=doc.get("schemes") or {},
    )
    _MANIFEST_CACHE[build] = (mtime, m)
    return m


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

_SPACE_RE = re.compile(r"^/spaces/([^/]+)/")
_SHIP_RE = re.compile(r"^/content/gameplay/([^/]+)/ship/([^/]+)/([^/]+)/")


def area_of(path: str) -> str:
    """Coarse bucket for reporting (``maps``, ``ships``, ``particles``, ...)."""
    if path.startswith("/spaces/"):
        return "maps"
    if _SHIP_RE.match(path) or re.match(r"^/content/gameplay/[^/]+/ship/", path):
        return "ships"
    if path.startswith("/particles/"):
        return "particles"
    if path.startswith("/content/gameplay/"):
        return "ship parts & gameplay assets"
    if path.startswith("/content/location/") or path.startswith("/content/ports/"):
        return "map props & ports"
    if path.startswith("/content/"):
        return "other content"
    top = path.strip("/").split("/", 1)[0]
    return top or "/"


def _kind_key(path: str, common_names: set[str]) -> str:
    name = path.rsplit("/", 1)[-1]
    if name in common_names:
        return name
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else "(no extension)"


def _section_diff(old: dict[str, Any], new: dict[str, Any], fp) -> dict[str, list[str]]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(old) & set(new) if fp(old[p]) != fp(new[p]))
    return {"added": added, "removed": removed, "changed": changed}


def _format_signals(
    old: dict[str, Any], new: dict[str, Any], changed: list[str], key_of, *,
    min_count: int = 20, threshold: float = 0.9,
) -> list[dict[str, Any]]:
    """Groups where (almost) every file changed in one build.

    Content edits touch a few files of a kind; a format change rewrites all
    of them. A flagged group is worth a parser check before trusting new
    exports (build 13357625 rewrote every ``space.bin``)."""
    totals: Counter[str] = Counter()
    for p in set(old) & set(new):
        totals[key_of(p)] += 1
    changed_by: Counter[str] = Counter(key_of(p) for p in changed)
    out = []
    for k, total in totals.items():
        if total >= min_count and changed_by[k] / total >= threshold:
            out.append({"kind": k, "changed": changed_by[k], "total": total})
    return sorted(out, key=lambda d: -d["total"])


def diff_manifests(old: BuildManifest, new: BuildManifest) -> dict[str, Any]:
    """Added / removed / changed entries per section between two builds.

    A section whose hash scheme differs between the two manifests (the
    toolkit or pipeline hashing changed in between) is listed under
    ``incomparable``: its added / removed lists are still right, but its
    ``changed`` list is left empty. Rebuild the older manifest
    (``wows-builds snapshot --build N --refresh``) while that build's idx
    files are still installed to compare it."""
    incomparable = [s for s in SECTIONS if old.scheme(s) != new.scheme(s)]
    files = _section_diff(old.files, new.files, lambda v: tuple(v))
    records = _section_diff(old.records, new.records, lambda v: v[0])
    gameparams = _section_diff(old.gameparams, new.gameparams, lambda v: v)
    camouflages = _section_diff(old.camouflages, new.camouflages, lambda v: v)
    for name, section in (("files", files), ("records", records),
                          ("gameparams", gameparams), ("camouflages", camouflages)):
        if name in incomparable:
            section["changed"] = []

    names = Counter(p.rsplit("/", 1)[-1] for p in new.files)
    common_names = {n for n, c in names.items() if c >= 20 and "." in n}

    def file_key(p: str) -> str:
        return _kind_key(p, common_names)

    def record_key(p: str) -> str:
        rec = new.records.get(p) or old.records.get(p)
        return rec[2] if rec else "?"

    signals = _format_signals(old.files, new.files, files["changed"], file_key)
    decoded_old = {p: v for p, v in old.records.items() if v[1] == "decoded"}
    decoded_new = {p: v for p, v in new.records.items() if v[1] == "decoded"}
    signals += [
        {**s, "kind": f"record:{s['kind']}"}
        for s in _format_signals(
            decoded_old, decoded_new,
            [p for p in records["changed"] if p in decoded_old and p in decoded_new],
            record_key,
        )
    ]

    def by_area(paths: list[str]) -> dict[str, int]:
        return dict(Counter(area_of(p) for p in paths).most_common())

    spaces: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for kind in ("added", "removed", "changed"):
        for p in files[kind]:
            m = _SPACE_RE.match(p)
            if m:
                spaces[m.group(1)][kind].append(p[len(m.group(0)):])

    ship_dirs: Counter[str] = Counter()
    for p in files["changed"] + files["added"] + records["changed"] + records["added"]:
        m = _SHIP_RE.match(p)
        if m and m.group(3) != "textures":
            ship_dirs[m.group(3)] += 1

    shallow = sum(1 for v in new.records.values() if v[1] == "shallow")
    return {
        "schema": DIFF_SCHEMA,
        "from_build": old.build,
        "to_build": new.build,
        "generated_at": _now(),
        "summary": {
            "files": {k: len(v) for k, v in files.items()},
            "records": {k: len(v) for k, v in records.items()},
            "gameparams": {k: len(v) for k, v in gameparams.items()},
            "camouflages": {k: len(v) for k, v in camouflages.items()},
            "records_changed_by_type": dict(Counter(record_key(p) for p in records["changed"])),
            "shallow_records": shallow,
        },
        "by_area": {
            "files_changed": by_area(files["changed"]),
            "files_added": by_area(files["added"]),
            "records_changed": by_area(records["changed"]),
            "records_added": by_area(records["added"]),
        },
        "format_signals": signals,
        "incomparable": incomparable,
        "spaces": {k: {kk: sorted(vv) for kk, vv in v.items()} for k, v in sorted(spaces.items())},
        "ship_model_dirs": dict(ship_dirs.most_common()),
        "files": files,
        "records": records,
        "gameparams": gameparams,
        "camouflages": camouflages,
        "sections": {"from": old.sections, "to": new.sections},
    }
