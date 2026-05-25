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

Canonical :class:`StepEvent` step names emitted to ``on_event``:

    "discover_domain_files"  "copy_ships"  "copy_library"
    "copy_projectiles"       "copy_decals"

Each step emits ``started`` -> ``completed`` (or ``skipped``).

Domain selection: pass ``domains=("ships", "library", ...)`` to restrict
the operation; the default fans out across all four. ``only_ships``
filters the ``ships`` domain to a subset; absent it, every ship that
carries a ``<Name>.meta.json`` sidecar gets published.
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

from ..config import PipelineConfig
from ..read import sidecar as read_sidecar
from ..types import OnEvent, PublishCounts, PublishResult
from ._step_runner import StepRunner

# Extensions we consider "publishable" under textures_dds/ and the
# library DDS roots. Matches the I:-side `DDS_EXTENSIONS` tuple.
_DDS_EXTENSIONS: tuple[str, ...] = (".dd0", ".dd1", ".dd2", ".dds")

# Domain step-name registry. Kept as a module-level constant so callers
# importing the names directly stay stable across releases.
_DOMAIN_STEPS: dict[str, str] = {
    "ships":       "copy_ships",
    "library":     "copy_library",
    "projectiles": "copy_projectiles",
    "decals":      "copy_decals",
}


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
    skip_suffix: tuple[str, ...] = (),
) -> PublishCounts:
    """Walk ``src_root`` recursively and mirror eligible files into ``dst_root``.

    Used by the three library copies (accessories / projectiles / decals).
    The flag tuple lets each call narrow what's published — the camo-mask
    + camo_mat trees only carry DDS, while accessories + projectiles
    need GLBs and JSON too.
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


def publish(
    *,
    target_dir: Path,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    only_ships: tuple[str, ...] | None = None,
    domains: tuple[str, ...] = ("ships", "library", "projectiles", "decals"),
    force: bool = False,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
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
        domains      Which of the four domains to publish. Default
                     fans out across all four; pass a tuple subset to
                     opt out cleanly. Unknown names raise ``ValueError``.
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
                # asset_attachments_resolve, never consumed downstream).
                skip_suffix=(".skel_ext_candidates.json",),
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

    return PublishResult(
        target_dir=target_dir,
        ships=ships_counts,
        library=library_counts,
        projectiles=projectiles_counts,
        decals=decals_counts,
        warnings=tuple(warnings),
    )


__all__ = ["publish"]
