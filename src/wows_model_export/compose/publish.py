"""Copy pipeline artifacts to a downstream consumer target.

Layer 4 (composer) — does disk I/O only; idempotent mtime+size compare so
re-runs are cheap.

``target_dir`` is a required keyword argument so it works for any
consumer that wants the same on-disk shape: per-ship folders mirroring
``ships/<Ship>/``, plus the four fleet-shared libraries
(``accessories/``, ``projectiles/``, ``camo_masks/``, ``camo_mat/``).

What it copies (per domain):

* ``ships``       — ``<Ship>/<Ship>.meta.json`` + ``_armor.json`` +
                    ``_ballistics.json``; ``models/*.glb`` +
                    ``models/*.json`` (placements / accessories) +
                    ``models/textures_dds/*.dd?`` +
                    ``models/skins/<skin_id>/...`` (DDS only).
* ``library``     — ``libraries/accessories/`` GLBs + ``.json`` (index +
                    rig pivots + attached accessories) + DDS files,
                    skipping the large ``.skel_ext_candidates.json``
                    inputs (~10 MB per asset).
* ``projectiles`` — ``libraries/projectiles/`` GLBs + JSON + DDS.
* ``aircraft``    — ``libraries/aircraft/`` GLBs + JSON (index, visual
                    JSON, material mappings) + DDS, copied to
                    ``aircraft/``. OPT-IN only (not in the default domain
                    fan-out): request ``domains=("aircraft", ...)``.
* ``decals``      — the dynamic-decal library ``libraries/decals/``
                    (WG ``dyndecals/`` mirror: ``*_{d,p,e}.{dds,dd?}``
                    textures + the ``manifest.json`` prototype table)
                    copied to ``decals/``, PLUS the two camo-shared atlas
                    subtrees ``libraries/camo_masks/`` +
                    ``libraries/camo_mat/`` (DDS only). All three ride
                    under one ``decals`` domain — opted in via the
                    I:-side ``--accessories`` flag, or ``--all``. The
                    decal library must be built first with
                    ``wows-build-decal-library``; when it's absent the
                    copy is a graceful no-op.
* ``environment``  — the IBL / tonemap library: the PMREM cube + BRDF-LUT
                    cache (``content/environment/`` DDS) + the per-weather
                    ``library/environment/manifest.json`` (GT tonemap params,
                    SH, cube format). The ``content/`` + ``library/`` prefixes
                    are PRESERVED under the target so the manifest's
                    workspace-relative URLs resolve against the published root.
                    Built with ``wows-build-environment-library``; absent ->
                    no-op.
* ``shared_textures`` — the content-addressed store of texture sets
                    shared byte-for-byte across export folders
                    (``libraries/textures_shared/<key-8>/<file>``: the
                    ship detail atlas and every other texture set two or
                    more folders hold) copied to
                    ``textures_shared/<key-8>/<file>``. Sidecar and
                    library-index texture paths that start with
                    ``libraries/`` name these files (workspace-relative;
                    the ``libraries/`` prefix maps to the target root like
                    every other library). Published automatically whenever
                    ``ships``, ``library`` or ``projectiles`` is, since
                    their texture paths point into it. Flat files at the
                    store root (hardlink canonicals of
                    ``wows-dedup-textures --hardlink``) are not published.
* ``maps``        — cached map/space exports ``maps/<Map>/``: the map GLB +
                    the sidecar manifests (``export.json`` + ``*_manifest``
                    JSONs) + the non-GLB sidecars the map contract carries —
                    shoreline SDF ``.png`` pair, raw terrain heightfield
                    ``.r16``, per-weather sky ``.hdr`` environment maps.
                    OPT-IN only (not in the default domain fan-out): map
                    exports run hundreds of MB each, so callers request
                    ``domains=("maps",)`` deliberately. Absent -> no-op.

Canonical :class:`StepEvent` step names emitted to ``on_event``:

    "discover_domain_files"  "copy_ships"  "copy_library"
    "copy_projectiles"       "copy_aircraft"  "copy_decals"  "copy_environment"
    "copy_maps"              "copy_shared_textures"

Each step emits ``started`` -> ``completed`` (or ``skipped``).

Domain selection: pass ``domains=("ships", "library", ...)`` to restrict
the operation; the default fans out across all domains except ``maps``.
``only_ships`` filters the ``ships`` domain to a subset; absent it, every
ship that carries a ``<Name>.meta.json`` sidecar gets published.
"""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

from .. import shared_textures
from ..config import BUILD_WORKSPACES_DIR, PipelineConfig
from ..errors import ConfigError
from ..read import sidecar as read_sidecar
from ..types import OnEvent, PublishCounts, PublishResult
from ._step_runner import StepRunner
from .dds_mips import complete_partial_mip_chain, rewrap_legacy_rgba8

# Extensions we consider "publishable" under textures_dds/ and the
# library DDS roots. Matches the I:-side `DDS_EXTENSIONS` tuple.
_DDS_EXTENSIONS: tuple[str, ...] = (".dd0", ".dd1", ".dd2", ".dds")

# Domain step-name registry. Kept as a module-level constant so callers
# importing the names directly stay stable across releases.
_DOMAIN_STEPS: dict[str, str] = {
    "ships":       "copy_ships",
    "library":     "copy_library",
    "projectiles": "copy_projectiles",
    "aircraft":    "copy_aircraft",
    "decals":      "copy_decals",
    "environment": "copy_environment",
    "maps":        "copy_maps",
    "shared_textures": "copy_shared_textures",
}

# Domains whose texture paths reference the shared texture store; asking
# for any of them publishes the store too.
_STORE_USING_DOMAINS: frozenset[str] = frozenset({"ships", "library", "projectiles", "aircraft"})

# Non-GLB/JSON/DDS sidecar extensions the maps domain carries: shoreline
# SDF + minimap-style PNGs, per-weather sky HDR environment maps, raw
# terrain heightfield (u16le), vegetation mesh/instance blobs (.bin).
_MAP_SIDECAR_EXTENSIONS: tuple[str, ...] = (".png", ".hdr", ".r16", ".bin")


# ---------------------------------------------------------------------------
# Copy primitives
# ---------------------------------------------------------------------------


def _copy_if_changed(src: Path, dst: Path, force: bool) -> bool:
    """Copy ``src`` to ``dst`` unless ``dst`` already matches.

    Returns ``True`` when a copy actually happened. Uses mtime + size for
    the compare — cheap and correct for the pipeline's write-once-per-
    export pattern. ``force=True`` skips the check and always copies.
    """
    if dst.exists() and not force:
        s_stat = src.stat()
        d_stat = dst.stat()
        if (s_stat.st_size == d_stat.st_size
                and abs(s_stat.st_mtime - d_stat.st_mtime) < 1.0):
            return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if dst.suffix.lower() == ".dds" and (
        complete_partial_mip_chain(dst) | rewrap_legacy_rgba8(dst)
    ):
        # Some loaders reject a chain that stops before 1x1 or a legacy masked
        # RGBA pixel format (see dds_mips). The rewritten copy differs in size
        # from its source, so the size compare above re-copies these few
        # files on every publish; that's harmless.
        os.utime(dst, (src.stat().st_atime, src.stat().st_mtime))
    return True


def _discover_ships(workspace: Path) -> list[str]:
    """All ships under ``workspace/ships/`` that carry a ``<Name>.meta.json``."""
    ships_dir = workspace / "ships"
    if not ships_dir.is_dir():
        return []
    out: list[str] = []
    for child in sorted(ships_dir.iterdir()):
        if not child.is_dir():
            continue
        if (child / f"{child.name}.meta.json").is_file():
            out.append(child.name)
    return out


def _publish_ship(
    ship: str,
    *,
    source_root: Path,
    dest_root: Path,
    force: bool,
) -> PublishCounts:
    """Copy one ship's output into ``<dest_root>/<Ship>/``.

    Mirrors the I:-side ``publish_ship`` behavior verbatim:
    top-level sidecar / armor / ballistics JSON files, then
    ``models/`` (GLBs + JSON + textures_dds DDS + skins DDS).
    """
    src_ship = source_root / "ships" / ship
    if not src_ship.is_dir():
        raise FileNotFoundError(f"ship folder not found: {src_ship}")

    sidecar_path = src_ship / f"{ship}.meta.json"
    if not sidecar_path.is_file():
        raise FileNotFoundError(
            f"sidecar missing: {sidecar_path}. "
            f"Run compose.scaffold_ship({ship!r}) first."
        )

    dst_ship = dest_root / ship
    copied = 0
    skipped = 0

    # Top-level files.
    for name in (
        f"{ship}.meta.json",
        f"{ship}_armor.json",
        f"{ship}_ballistics.json",
    ):
        src = src_ship / name
        if not src.is_file():
            continue
        dst = dst_ship / name
        if _copy_if_changed(src, dst, force):
            copied += 1
        else:
            skipped += 1

    # models/ — hull GLB + placements JSON + textures_dds/ + skins/.
    src_gm3d = src_ship / read_sidecar.MODELS_SUBDIR
    if src_gm3d.is_dir():
        dst_gm3d = dst_ship / read_sidecar.MODELS_SUBDIR
        # Top-level GLB + placements JSON files.
        for src in src_gm3d.iterdir():
            if not src.is_file():
                continue
            if src.suffix.lower() not in (".glb", ".json"):
                continue
            dst = dst_gm3d / src.name
            if _copy_if_changed(src, dst, force):
                copied += 1
            else:
                skipped += 1
        # textures_dds/ — DDS files only.
        src_tex = src_gm3d / "textures_dds"
        if src_tex.is_dir():
            dst_tex = dst_gm3d / "textures_dds"
            for src in src_tex.iterdir():
                if not src.is_file():
                    continue
                if src.suffix.lower() not in _DDS_EXTENSIONS:
                    continue
                dst = dst_tex / src.name
                if _copy_if_changed(src, dst, force):
                    copied += 1
                else:
                    skipped += 1
        # models/exteriors/ — variant hull GLBs for hull-swap exteriors
        # (ship-exterior unification HullDelta; the top-level iterdir above
        # is deliberately non-recursive, so without this block the variant
        # hulls would never publish — handoff §9f.2). Empty/absent until
        # the producer cutover step emits them.
        src_ext = src_gm3d / "exteriors"
        if src_ext.is_dir():
            dst_ext = dst_gm3d / "exteriors"
            for src in src_ext.rglob("*"):
                if not src.is_file():
                    continue
                if src.suffix.lower() not in (".glb", ".json"):
                    continue
                rel = src.relative_to(src_ext)
                dst = dst_ext / rel
                if _copy_if_changed(src, dst, force):
                    copied += 1
                else:
                    skipped += 1
        # skins/<skin_id>/{hull,accessories/<asset_id>}/*.dd*
        # Sidecar `Skin.texture_sets[<scheme>][<slot>].dds_mips` paths
        # are relative to the hull GLB dir; preserving the subtree keeps
        # them resolving identically on the consumer side.
        src_skins = src_gm3d / "skins"
        if src_skins.is_dir():
            dst_skins = dst_gm3d / "skins"
            for src in src_skins.rglob("*"):
                if not src.is_file():
                    continue
                if src.suffix.lower() not in _DDS_EXTENSIONS:
                    continue
                rel = src.relative_to(src_skins)
                dst = dst_skins / rel
                if _copy_if_changed(src, dst, force):
                    copied += 1
                else:
                    skipped += 1

    return PublishCounts(copied=copied, skipped=skipped)


def _publish_tree(
    src_root: Path,
    dst_root: Path,
    *,
    force: bool,
    allow_json: bool = True,
    allow_glb: bool = True,
    allow_dds: bool = True,
    extra_extensions: tuple[str, ...] = (),
    skip_suffix: tuple[str, ...] = (),
) -> PublishCounts:
    """Walk ``src_root`` recursively and mirror eligible files into ``dst_root``.

    Used by the library copies (accessories / projectiles / decals) and
    the maps domain. The flag tuple lets each call narrow what's
    published — the camo-mask + camo_mat trees only carry DDS, while
    accessories + projectiles need GLBs and JSON too. ``extra_extensions``
    widens the whitelist for domains whose contract carries formats
    beyond GLB/JSON/DDS (the maps domain's ``.png``/``.hdr``/``.r16``
    sidecars).
    """
    if not src_root.is_dir():
        return PublishCounts()
    copied = 0
    skipped = 0
    for src in src_root.rglob("*"):
        if not src.is_file():
            continue
        if any(src.name.endswith(s) for s in skip_suffix):
            skipped += 1
            continue
        ext = src.suffix.lower()
        eligible = (
            (allow_glb and ext == ".glb")
            or (allow_json and ext == ".json")
            or (allow_dds and ext in _DDS_EXTENSIONS)
            or ext in extra_extensions
        )
        if not eligible:
            continue
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        if _copy_if_changed(src, dst, force):
            copied += 1
        else:
            skipped += 1
    return PublishCounts(copied=copied, skipped=skipped)


def _publish_flat_dds(
    src_root: Path,
    dst_root: Path,
    *,
    force: bool,
) -> PublishCounts:
    """Mirror only the immediate DDS files in ``src_root`` to ``dst_root``.

    Used for ``camo_masks/`` and ``camo_mat/`` where everything sits at
    the root (no per-asset subdirectories).
    """
    if not src_root.is_dir():
        return PublishCounts()
    copied = 0
    skipped = 0
    for src in src_root.iterdir():
        if not src.is_file():
            continue
        if src.suffix.lower() not in _DDS_EXTENSIONS:
            continue
        dst = dst_root / src.name
        if _copy_if_changed(src, dst, force):
            copied += 1
        else:
            skipped += 1
    return PublishCounts(copied=copied, skipped=skipped)


def _publish_shared_store(
    src_root: Path,
    dst_root: Path,
    *,
    force: bool,
) -> PublishCounts:
    """Mirror the shared texture store's ``<hash>/<file>`` entries.

    Only the content-addressed subdirectories are copied; flat files at
    the store root (legacy hardlink canonicals) are skipped.
    """
    if not src_root.is_dir():
        return PublishCounts()
    copied = 0
    skipped = 0
    for sub in sorted(src_root.iterdir()):
        if not sub.is_dir():
            continue
        name = sub.name.lower()
        if len(name) != shared_textures.HASH_PREFIX_LEN or any(
            c not in "0123456789abcdef" for c in name
        ):
            continue
        for src in sorted(sub.iterdir()):
            if not src.is_file() or src.suffix.lower() not in _DDS_EXTENSIONS:
                continue
            if _copy_if_changed(src, dst_root / sub.name / src.name, force):
                copied += 1
            else:
                skipped += 1
    return PublishCounts(copied=copied, skipped=skipped)


def _combine_counts(*counts: PublishCounts) -> PublishCounts:
    """Sum field-wise across multiple ``PublishCounts`` instances."""
    return PublishCounts(
        copied=sum(c.copied for c in counts),
        skipped=sum(c.skipped for c in counts),
        deleted=sum(c.deleted for c in counts),
    )


# ---------------------------------------------------------------------------
# Top-level composer
# ---------------------------------------------------------------------------


def build_workspace_build(cfg: PipelineConfig, workspace: Path) -> int | None:
    """The older game build whose workspace ``workspace`` is, or ``None``.

    True for a config pinned to a build (``--build``, the webview's
    build view) publishing its own workspace, and for any
    ``<workspace>/builds/<build>/`` folder named directly."""
    ws = Path(workspace).resolve()
    if cfg.build is not None and ws == Path(cfg.workspace).resolve():
        return cfg.build
    if ws.parent.name == BUILD_WORKSPACES_DIR and ws.name.isdigit():
        return int(ws.name)
    return None


def publish(
    *,
    target_dir: Path,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    only_ships: tuple[str, ...] | None = None,
    domains: tuple[str, ...] = (
        "ships", "library", "projectiles", "decals", "environment",
    ),
    only_maps: tuple[str, ...] | None = None,
    force: bool = False,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
    allow_build_workspace: bool = False,
) -> PublishResult:
    """Copy pipeline artifacts to a consumer target.

    Idempotent — re-runs use a mtime+size compare and skip up-to-date
    files. Pass ``force=True`` to copy every eligible file regardless.

    Parameters:
        target_dir   Consumer-side destination root. Any path mirrors
                     fine — the layout follows the I:-side workspace
                     conventions.
        workspace    Pipeline workspace; defaults to ``config.workspace``.
                     Per-ship dirs are read from ``<workspace>/ships/``,
                     libraries from ``<workspace>/libraries/``.
        config       Resolved :class:`PipelineConfig`; loaded on demand
                     when ``None``.
        only_ships   Restrict the ``ships`` domain to this subset. When
                     ``None``, publishes every ship under
                     ``workspace/ships`` that carries a sidecar.
        domains      Which domains to publish. The default fans out
                     across every domain EXCEPT ``maps`` (map exports
                     run hundreds of MB each — request ``("maps",)``
                     deliberately). ``shared_textures`` rides along with
                     ``ships`` / ``library`` / ``projectiles``. Unknown
                     names raise ``ValueError``.
        force        Skip the mtime+size compare and copy every file.
        on_event     Optional :class:`StepEvent` callback. Steps:
                     ``discover_domain_files``, ``copy_ships``,
                     ``copy_library``, ``copy_projectiles``,
                     ``copy_decals`` — each emits ``started`` ->
                     ``completed`` (or ``skipped`` when the domain is
                     not in the ``domains`` filter).
        cancel       Optional :class:`threading.Event` for cooperative
                     cancel; when set, the next step boundary raises
                     :class:`wows_model_export.errors.CancelledError`.
        allow_build_workspace
                     Publish an older game build's workspace
                     (a pinned config, or ``<workspace>/builds/<build>/``).
                     Refused by default with :class:`ConfigError`: a
                     consumer target holds the current build, and the
                     older build's files would overwrite those of the same
                     names (its accessory index, ship sidecars, GLBs).
                     Pass it only with a target of its own.

    Returns a :class:`PublishResult` with per-domain
    :class:`PublishCounts` (``copied`` / ``skipped`` / ``deleted``).
    Failures inside a step raise :class:`StepError` with the step name
    set to one of the canonical names above.
    """
    cfg = config or PipelineConfig.load()
    if workspace is None:
        workspace = cfg.workspace
    workspace = Path(workspace)
    target_dir = Path(target_dir)

    pinned = build_workspace_build(cfg, workspace)
    if pinned is not None and not allow_build_workspace:
        raise ConfigError(
            f"publish: {workspace} is the workspace of game build {pinned}, not "
            "the install's current build. Publishing it would overwrite the "
            f"target's current-build files of the same names in {target_dir}. "
            "Publish from the current build (no --build / WOWS_BUILD; in the "
            "webview, switch the game-build picker to the current build), or "
            "opt in with allow_build_workspace and a target of its own."
        )

    unknown = [d for d in domains if d not in _DOMAIN_STEPS]
    if unknown:
        raise ValueError(
            f"publish: unknown domains: {unknown!r}. "
            f"Valid: {sorted(_DOMAIN_STEPS)}"
        )

    domain_set = set(domains)
    warnings: list[str] = []
    runner = StepRunner(on_event, cancel=cancel)

    target_dir.mkdir(parents=True, exist_ok=True)

    # ── Step: discover_domain_files ───────────────────────────────────
    ships_to_publish: list[str] = []
    with runner.step("discover_domain_files", detail=str(target_dir)):
        if "ships" in domain_set:
            discovered = _discover_ships(workspace)
            if only_ships is not None:
                only_set = set(only_ships)
                ships_to_publish = [s for s in discovered if s in only_set]
                missing = sorted(only_set - set(discovered))
                if missing:
                    warnings.append(
                        f"only_ships requested {missing!r} but no sidecar found"
                    )
            else:
                ships_to_publish = list(discovered)

    # ── Step: copy_ships ──────────────────────────────────────────────
    ships_counts = PublishCounts()
    if "ships" in domain_set:
        with runner.step(
            "copy_ships",
            detail=f"{len(ships_to_publish)} ship(s)",
        ) as ctx:
            per_ship: list[PublishCounts] = []
            for ship in ships_to_publish:
                try:
                    counts = _publish_ship(
                        ship,
                        source_root=workspace,
                        dest_root=target_dir,
                        force=force,
                    )
                except FileNotFoundError as e:
                    warnings.append(f"{ship}: {e}")
                    continue
                per_ship.append(counts)
            ships_counts = _combine_counts(*per_ship) if per_ship else PublishCounts()
            ctx.annotate(
                f"copied={ships_counts.copied} skipped={ships_counts.skipped}",
                data={
                    "ship_count": len(ships_to_publish),
                    "copied":     ships_counts.copied,
                    "skipped":    ships_counts.skipped,
                },
            )
    else:
        runner.emit("copy_ships", "skipped", detail="domain not requested")

    # ── Step: copy_library ────────────────────────────────────────────
    library_counts = PublishCounts()
    if "library" in domain_set:
        with runner.step("copy_library") as ctx:
            library_counts = _publish_tree(
                workspace / "libraries" / "accessories",
                target_dir / "accessories",
                force=force,
                allow_json=True,
                allow_glb=True,
                allow_dds=True,
                # Drop the toolkit-emitted skel_ext candidate JSONs
                # (~10 MB per heavy mount; transient input to
                # asset_attachments_resolve, never consumed downstream)
                # and the shared-texture redirect files (pipeline-internal;
                # the index already names the store paths).
                skip_suffix=(
                    ".skel_ext_candidates.json",
                    shared_textures.REDIRECT_FILENAME,
                ),
            )
            ctx.annotate(
                f"copied={library_counts.copied} skipped={library_counts.skipped}",
                data={
                    "copied":  library_counts.copied,
                    "skipped": library_counts.skipped,
                },
            )
    else:
        runner.emit("copy_library", "skipped", detail="domain not requested")

    # ── Step: copy_projectiles ────────────────────────────────────────
    projectiles_counts = PublishCounts()
    if "projectiles" in domain_set:
        with runner.step("copy_projectiles") as ctx:
            projectiles_counts = _publish_tree(
                workspace / "libraries" / "projectiles",
                target_dir / "projectiles",
                force=force,
                allow_json=True,
                allow_glb=True,
                allow_dds=True,
                skip_suffix=(shared_textures.REDIRECT_FILENAME,),
            )
            ctx.annotate(
                f"copied={projectiles_counts.copied} "
                f"skipped={projectiles_counts.skipped}",
                data={
                    "copied":  projectiles_counts.copied,
                    "skipped": projectiles_counts.skipped,
                },
            )
    else:
        runner.emit("copy_projectiles", "skipped", detail="domain not requested")

    # ── Step: copy_aircraft ───────────────────────────────────────────
    aircraft_counts = PublishCounts()
    if "aircraft" in domain_set:
        with runner.step("copy_aircraft") as ctx:
            aircraft_counts = _publish_tree(
                workspace / "libraries" / "aircraft",
                target_dir / "aircraft",
                force=force,
                allow_json=True,
                allow_glb=True,
                allow_dds=True,
                skip_suffix=(shared_textures.REDIRECT_FILENAME,),
            )
            ctx.annotate(
                f"copied={aircraft_counts.copied} skipped={aircraft_counts.skipped}",
                data={
                    "copied":  aircraft_counts.copied,
                    "skipped": aircraft_counts.skipped,
                },
            )
    else:
        runner.emit("copy_aircraft", "skipped", detail="domain not requested")

    # ── Step: copy_shared_textures ────────────────────────────────────
    # Ship / library / projectile texture paths that start with
    # `libraries/` name the shared store, so it publishes with them.
    shared_counts = PublishCounts()
    if "shared_textures" in domain_set or domain_set & _STORE_USING_DOMAINS:
        with runner.step("copy_shared_textures") as ctx:
            shared_counts = _publish_shared_store(
                shared_textures.store_root(workspace),
                target_dir / shared_textures.STORE_REL.split("/", 1)[1],
                force=force,
            )
            ctx.annotate(
                f"copied={shared_counts.copied} skipped={shared_counts.skipped}",
                data={
                    "copied":  shared_counts.copied,
                    "skipped": shared_counts.skipped,
                },
            )
    else:
        runner.emit("copy_shared_textures", "skipped", detail="domain not requested")

    # ── Step: copy_decals ─────────────────────────────────────────────
    decals_counts = PublishCounts()
    if "decals" in domain_set:
        with runner.step("copy_decals") as ctx:
            masks = _publish_flat_dds(
                workspace / "libraries" / "camo_masks",
                target_dir / "camo_masks",
                force=force,
            )
            mats = _publish_flat_dds(
                workspace / "libraries" / "camo_mat",
                target_dir / "camo_mat",
                force=force,
            )
            # Dynamic-decal library (dyndecals mirror + manifest.json),
            # built one-time by `wows-build-decal-library`. Flat layout,
            # so `_publish_tree` picks up the `*_{d,p,e}.{dds,dd?}`
            # textures AND the `manifest.json` the consumer needs to read
            # the prototype table. No-op when the library hasn't been
            # built (the source dir simply isn't there).
            decal_lib = _publish_tree(
                workspace / "libraries" / "decals",
                target_dir / "decals",
                force=force,
                allow_json=True,
                allow_glb=False,
                allow_dds=True,
            )
            decals_counts = _combine_counts(masks, mats, decal_lib)
            ctx.annotate(
                f"copied={decals_counts.copied} skipped={decals_counts.skipped}",
                data={
                    "copied":      decals_counts.copied,
                    "skipped":     decals_counts.skipped,
                    "camo_masks":  {
                        "copied":  masks.copied, "skipped":  masks.skipped,
                    },
                    "camo_mat":   {
                        "copied":  mats.copied,  "skipped":  mats.skipped,
                    },
                    "decal_library": {
                        "copied":  decal_lib.copied,
                        "skipped": decal_lib.skipped,
                    },
                },
            )
    else:
        runner.emit("copy_decals", "skipped", detail="domain not requested")

    # ── Step: copy_environment ────────────────────────────────────────
    # The PMREM cube / BRDF-LUT cache + the per-weather IBL/tonemap
    # manifest, built by `wows-build-environment-library`. Unlike the
    # other domains, the `content/` + `library/` path prefixes are
    # PRESERVED under the target so the manifest's workspace-relative
    # URLs (`content/environment/...`) resolve identically against the
    # published root. No-op when the library hasn't been built.
    environment_counts = PublishCounts()
    if "environment" in domain_set:
        with runner.step("copy_environment") as ctx:
            env_dds = _publish_tree(
                workspace / "content" / "environment",
                target_dir / "content" / "environment",
                force=force,
                allow_json=False,
                allow_glb=False,
                allow_dds=True,
            )
            env_manifest = _publish_tree(
                workspace / "library" / "environment",
                target_dir / "library" / "environment",
                force=force,
                allow_json=True,
                allow_glb=False,
                allow_dds=False,
            )
            environment_counts = _combine_counts(env_dds, env_manifest)
            ctx.annotate(
                f"copied={environment_counts.copied} "
                f"skipped={environment_counts.skipped}",
                data={
                    "copied":  environment_counts.copied,
                    "skipped": environment_counts.skipped,
                    "cubes_brdf": {
                        "copied":  env_dds.copied, "skipped":  env_dds.skipped,
                    },
                    "manifest": {
                        "copied":  env_manifest.copied,
                        "skipped": env_manifest.skipped,
                    },
                },
            )
    else:
        runner.emit("copy_environment", "skipped", detail="domain not requested")

    # ── Step: copy_maps ───────────────────────────────────────────────
    # Cached map/space exports: manifest JSONs + the non-GLB sidecars
    # (shoreline SDF PNGs, raw terrain heightfield .r16, per-weather sky
    # .hdr). GLBs are deliberately EXCLUDED: the whole-map scene GLB is a
    # webview artifact (hundreds of MB), and map_local/*.glb prototype
    # meshes are editor-imported assets that belong under the consumer's
    # Pipeline folder, not StreamingAssets. OPT-IN — never in the default
    # domain fan-out.
    maps_counts = PublishCounts()
    if "maps" in domain_set:
        with runner.step("copy_maps") as ctx:
            # The workspace maps tree accumulates every map ever exported
            # (audits, one-off inspections); consumers typically want a
            # curated subset — `only_maps` restricts the copy per map dir.
            if only_maps:
                for map_name in only_maps:
                    src = workspace / "maps" / map_name
                    if not src.is_dir():
                        runner.emit(
                            "copy_maps", "progress",
                            detail=f"map not in workspace: {map_name}",
                        )
                        continue
                    maps_counts = _combine_counts(maps_counts, _publish_tree(
                        src,
                        target_dir / "maps" / map_name,
                        force=force,
                        allow_json=True,
                        allow_glb=False,
                        allow_dds=True,
                        extra_extensions=_MAP_SIDECAR_EXTENSIONS,
                    ))
            else:
                maps_counts = _publish_tree(
                    workspace / "maps",
                    target_dir / "maps",
                    force=force,
                    allow_json=True,
                    allow_glb=False,
                    allow_dds=True,
                    extra_extensions=_MAP_SIDECAR_EXTENSIONS,
                )
            ctx.annotate(
                f"copied={maps_counts.copied} skipped={maps_counts.skipped}",
                data={
                    "copied":  maps_counts.copied,
                    "skipped": maps_counts.skipped,
                },
            )
    else:
        runner.emit("copy_maps", "skipped", detail="domain not requested")

    return PublishResult(
        target_dir=target_dir,
        ships=ships_counts,
        library=library_counts,
        projectiles=projectiles_counts,
        aircraft=aircraft_counts,
        decals=decals_counts,
        environment=environment_counts,
        maps=maps_counts,
        shared_textures=shared_counts,
        warnings=tuple(warnings),
    )


__all__ = ["build_workspace_build", "publish"]
