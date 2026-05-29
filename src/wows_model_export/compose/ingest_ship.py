"""Compose `ingest_ship` -- one-shot per-ship pipeline composer.

Lifted from ``tools/ship/ingest_ship.py`` on the I:-side warships repo.
This is the Layer 4 capstone orchestrator: it chains every per-ship
operation into a single callable, walking the RUNBOOK §§1 + 3.5 workflow
from raw ship name -> publishable consumer-side bundle.

Stepwise the composer is:

  1. ``resolve_identity``       -- resolve the user input to a
                                   ``(label, toolkit_name)`` pair.  If the
                                   toolkit reports ambiguity and
                                   ``interactive=True``, the user is
                                   prompted on stdin to pick a model_dir.
  2. ``prep_dirs``              -- create ``<workspace>/ships/<label>/models/``.
  3. ``scaffold``               -- invoke
                                   :func:`wows_model_export.compose.scaffold_ship.scaffold_ship`,
                                   the export-ship + armor + ammo +
                                   sidecar sub-composer.  Sub-composer
                                   step events flow through the parent's
                                   ``on_event`` callback verbatim; the
                                   ``step`` field uniquely identifies
                                   each.
  4. ``resolve_decoratives``    -- invoke
                                   :func:`wows_model_export.compose.skel_ext_resolve.resolve_decorative_placements`
                                   to merge the toolkit-emitted skel_ext
                                   candidates JSON into
                                   ``<label>_accessories.json``.  When
                                   the candidates JSON exists, a
                                   subsequent scaffold-refresh pass picks
                                   the merged decoratives back into the
                                   sidecar.  Skipped when no candidates
                                   JSON is on disk (HP_-only sidecar).
  5. ``build_library``          -- invoke
                                   :func:`wows_model_export.compose.accessory_library.build_accessory_library`
                                   when ``build_library=True`` or
                                   ``rebuild_library=True``.  Additive
                                   (existing library assets kept;
                                   ``rebuild_library=True`` forces a full
                                   regenerate).  Followed by an automatic
                                   post-library scaffold refresh so the
                                   variant-swap bone-mismatch correction
                                   sees the freshly-built variant GLBs.
  6. ``publish``                -- invoke
                                   :func:`wows_model_export.compose.publish.publish`
                                   when ``and_publish=True``.  Generic
                                   publisher; pass ``publish_target`` to
                                   point at any consumer-side root.

Sub-composer error handling: a child composer's :class:`StepError`
propagates up wrapped in the parent's
``StepError(step="<parent_step>", underlying=child_error)`` -- consumers
that branch on ``.step`` get the parent's name; the original error chain
is preserved via ``raise ... from`` and accessible at ``.underlying``.

Refactor notes vs the I:-side ``ingest(*, ship_input=, out_root=, ...)``:

* ``out_root`` -> ``workspace`` (defaults to ``config.workspace``).
* ``game_dir`` + ``wowsunpack_path`` -> resolved via :class:`PipelineConfig`.
* ``IngestStepError`` dropped; package-level :class:`StepError` covers
  the same use case with a richer payload.
* ``publish_target`` replaces the hard-coded consumer path the old
  publish script carried -- the new ``compose.publish`` requires
  a ``target_dir``.
* Returns :class:`IngestResult` (frozen dataclass) instead of a
  free-form dict.
* The ``_run_*`` thin subprocess wrappers from the original module are
  replaced with direct in-process calls to the corresponding lifted
  composers.  ``turret_autorig`` is no longer invoked from here --
  ``build_accessory_library`` runs it per gun asset internally.
"""
from __future__ import annotations

import re
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from ..config import PipelineConfig
from ..errors import StepError, ToolkitError
from ..resolve import sidecar as _sidecar
from ..toolkit import armor_json as _toolkit_armor_json
from ..types import IngestResult, OnEvent, ScaffoldResult
from . import accessory_library as _accessory_library_mod
from . import publish as _publish_mod
from . import scaffold_ship as _scaffold_ship_mod
from . import skel_ext_resolve as _skel_ext_resolve_mod
from ._step_runner import StepRunner

# ---------------------------------------------------------------------------
# Ambiguity probe regexes (lifted verbatim from I:-side)
# ---------------------------------------------------------------------------

_AMBIGUITY_MARKER = "Multiple ships match"
_CANDIDATE_RE = re.compile(
    r"^\s+(?P<display>.+?)\s+\((?P<idx>[A-Z]+\d+)\)\s*->\s*(?P<model_dir>\S+)\s*$"
)

_FS_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")


# ---------------------------------------------------------------------------
# Identity resolution (lifted)
# ---------------------------------------------------------------------------


def _probe_ship(
    name: str,
    *,
    config: PipelineConfig | None,
) -> tuple[bool, str]:
    """Run a light toolkit call to check whether ``name`` resolves
    unambiguously. Returns ``(is_unambiguous, stderr_text)``.

    Uses ``armor-json`` because it goes through the same ``find_ship``
    code path as ``export-ship`` but fails fast on ambiguity without
    writing anything we care about. Output goes to a temp file we
    delete immediately.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        tmp_path = Path(tf.name)
    try:
        _toolkit_armor_json(name, tmp_path, config=config)
        return True, ""
    except ToolkitError as e:
        # ToolkitError carries stderr as a structured field; fall back to
        # the rendered message when the wrapped subprocess died before
        # printing anything.
        text = e.stderr if e.stderr else str(e)
        return False, text
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _parse_ambiguity(stderr_text: str) -> list[tuple[str, str, str]]:
    """Parse the toolkit's 'Multiple ships match' error message.

    Returns a list of ``(display_name, param_index, model_dir)`` tuples
    in the order the toolkit listed them.
    """
    out: list[tuple[str, str, str]] = []
    for line in stderr_text.splitlines():
        m = _CANDIDATE_RE.match(line)
        if m:
            out.append((m["display"].strip(), m["idx"].strip(), m["model_dir"].strip()))
    return out


def _sanitize_for_fs(s: str) -> str:
    """Turn a display name into a filesystem-safe label.

    ``Baltimore (Old)`` -> ``Baltimore_Old``; collapses runs of ``_``.
    """
    cleaned = _FS_SAFE_RE.sub("_", s).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned


def resolve_ship_identity(
    user_input: str,
    *,
    config: PipelineConfig | None = None,
    interactive: bool = False,
    forced_label: str | None = None,
) -> tuple[str, str]:
    """Return ``(label, toolkit_name)`` for the ship.

    * If the toolkit resolves ``user_input`` unambiguously, both values
      are the input (or ``forced_label`` if set for ``label``).
    * If ambiguous and ``interactive`` is True, prompts the user to
      pick a model_dir from the listing and (if no ``forced_label``
      given) accepts a folder label with a sanitized-display-name
      default.
    * If ambiguous and ``interactive`` is False, raises :class:`StepError`
      with the candidate listing in ``detail``.
    """
    ok, err = _probe_ship(user_input, config=config)
    if ok:
        label = forced_label or user_input
        return label, user_input

    if _AMBIGUITY_MARKER not in err:
        raise StepError(
            step="resolve_identity",
            underlying=RuntimeError(err),
            detail=f"toolkit error resolving {user_input!r}",
        )

    cands = _parse_ambiguity(err)
    if not cands:
        raise StepError(
            step="resolve_identity",
            underlying=RuntimeError(err),
            detail=f"couldn't parse ambiguity listing for {user_input!r}",
        )

    if not interactive:
        listing = "\n".join(
            f"  {disp}  ({idx})  ->  {mdir}" for disp, idx, mdir in cands
        )
        raise StepError(
            step="resolve_identity",
            underlying=ValueError(
                f"multiple ships match {user_input!r}; pass a model_dir "
                f"directly or interactive=True"
            ),
            detail=f"ambiguous {user_input!r}; candidates:\n{listing}",
            data={
                "candidates": [
                    {"display": d, "param_index": i, "model_dir": m}
                    for d, i, m in cands
                ],
            },
        )

    print(f"\nMultiple ships match {user_input!r}. Pick one:", file=sys.stderr)
    for i, (disp, idx, mdir) in enumerate(cands):
        print(f"  [{i}] {disp}  ({idx})  ->  {mdir}", file=sys.stderr)
    while True:
        raw = input(f"Select [0..{len(cands) - 1}]: ").strip()
        try:
            pick = int(raw)
        except ValueError:
            print("  not a number, try again", file=sys.stderr)
            continue
        if 0 <= pick < len(cands):
            break
        print(f"  out of range [0..{len(cands) - 1}], try again", file=sys.stderr)

    disp, idx, mdir = cands[pick]

    if forced_label:
        label = forced_label
    else:
        default_label = _sanitize_for_fs(disp) or mdir
        raw = input(f"Folder label [default: {default_label}]: ").strip()
        label = raw or default_label

    return label, mdir


# ---------------------------------------------------------------------------
# Reusable sidecar refresh + deferred library build (shared with the queue)
# ---------------------------------------------------------------------------


def refresh_ship_sidecar(
    label: str,
    *,
    workspace: Path,
    config: PipelineConfig,
    toolkit_ship: str | None = None,
    gameparams_ship_id: str | None = None,
    variant_permoflage: str | None = "auto",
    class_override: str | None = None,
    ship_key_suffix: str | None = None,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> "_scaffold_ship_mod.ScaffoldResult":
    """Re-fold a ship's sidecar with every heavy step skipped.

    Runs :func:`scaffold_ship` with ``skip_export / skip_armor / skip_ammo
    / skip_gameparams_autofill / skip_materials_skins / skip_geometry_hitbox``
    all set — so it only re-derives the placement/decorative/variant folds
    against whatever is already on disk. Used (a) after a decoratives
    merge, and (b) after the accessory-library build (the mesh-swap
    variant Ry(180°) bone correction needs the variant accessory GLBs to
    exist, which only happens post-library). A no-op for ships without
    variant swaps, but always cheap (no toolkit subprocess, no GameParams
    autofill).
    """
    return _scaffold_ship_mod.scaffold_ship(
        label,
        workspace=workspace,
        config=config,
        class_override=class_override,
        ship_key_suffix=ship_key_suffix,
        toolkit_ship=toolkit_ship,
        gameparams_ship_id=gameparams_ship_id,
        skip_export=True,
        skip_armor=True,
        skip_ammo=True,
        skip_gameparams_autofill=True,
        skip_materials_skins=True,
        skip_geometry_hitbox=True,
        variant_permoflage=variant_permoflage,
        on_event=on_event,
        cancel=cancel,
    )


def build_library_and_refresh(
    *,
    workspace: Path,
    config: PipelineConfig | None = None,
    refresh_specs: list[dict] | None = None,
    rebuild_library: bool = False,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> IngestResult:
    """Build the fleet-wide accessory library ONCE, then post-library
    refresh each given ship's sidecar.

    This is the queue-drain counterpart to the per-ingest
    ``build_library`` step: instead of re-scanning the whole library
    (re-reading ~1k GLBs + an all-tree rglob, ~30 s) once *per* queued
    ship, the queue defers it to a single pass after the queue drains.
    The final library state + sidecars are identical to the per-item path
    (the library build is additive; the union is the same either way).

    ``refresh_specs`` is a list of ``{label, toolkit_ship,
    gameparams_ship_id, permoflage}`` dicts — one per ship ingested since
    the last build. Ships whose ``<label>_skel_ext.json`` candidates file
    is absent are skipped (the post-library refresh is a no-op for them).
    Per-ship refresh failures are collected as warnings rather than
    aborting the whole drain (matching ``ingest_ship``'s post-library
    soft-fail).
    """
    cfg = config or PipelineConfig.load()
    workspace = Path(workspace).resolve()
    timer = StepRunner(on_event, cancel=cancel)
    warnings: list[str] = []

    timer.start("build_library", detail=("rebuild" if rebuild_library else "additive"))
    try:
        _accessory_library_mod.build_accessory_library(
            workspace=workspace,
            config=cfg,
            rebuild=rebuild_library,
            on_event=on_event,
            cancel=cancel,
        )
        timer.complete()
    except StepError as e:
        timer.fail("build_library", detail=f"step={e.step!r}")
        raise
    except Exception as e:
        timer.fail("build_library", detail=f"{type(e).__name__}: {e}")
        raise StepError(
            step="build_library", underlying=e,
            detail="build_accessory_library failed",
        ) from e

    for spec in (refresh_specs or []):
        label = str(spec.get("label") or "")
        if not label:
            continue
        candidates = (
            workspace / "ships" / label / _sidecar.MODELS_SUBDIR
            / f"{label}_skel_ext.json"
        )
        if not candidates.is_file():
            continue
        toolkit_ship = spec.get("toolkit_ship") or None
        if toolkit_ship == label:
            toolkit_ship = None
        try:
            refresh_ship_sidecar(
                label,
                workspace=workspace,
                config=cfg,
                toolkit_ship=toolkit_ship,
                gameparams_ship_id=spec.get("gameparams_ship_id") or None,
                variant_permoflage=spec.get("permoflage") or "auto",
                on_event=on_event,
                cancel=cancel,
            )
        except StepError as e:
            warnings.append(
                f"post-library refresh failed for {label!r} at "
                f"step {e.step!r}: {e.detail or e}"
            )
        except Exception as e:
            warnings.append(
                f"post-library refresh failed for {label!r}: "
                f"{type(e).__name__}: {e}"
            )

    return IngestResult(
        ship_id="",
        label="__library_drain__",
        workspace_dir=workspace,
        scaffold=None,
        accessories_json_path=None,
        library_refreshed=True,
        published_to=None,
        warnings=tuple(warnings),
        step_timings_ms=dict(timer.step_timings_ms),
    )


# ---------------------------------------------------------------------------
# Public composer entry
# ---------------------------------------------------------------------------


def ingest_ship(
    ship_input: str,
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    forced_label: str | None = None,
    interactive: bool = False,
    class_override: str | None = None,
    ship_key_suffix: str | None = None,
    build_library: bool = False,
    rebuild_library: bool = False,
    and_publish: bool = False,
    publish_target: Path | None = None,
    publish_force: bool = False,
    variant_permoflage: str | None = "auto",
    toolkit_ship_override: str | None = None,
    gameparams_ship_id: str | None = None,
    provenance_build_library: bool | None = None,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> IngestResult:
    """Run the full per-ship ingest pipeline end-to-end.

    The composer chains every per-ship operation: identity resolution,
    working-directory prep, scaffold (export-ship + armor + ammo +
    sidecar), decorative-placement merge, optional accessory-library
    refresh, and optional publish.

    Parameters:
        ship_input
            Display name (``Montana``), model_dir
            (``ASB017_Montana_1945``), or GameParams Vehicle id.
        workspace
            Per-ship working-dir root.  Defaults to
            ``config.workspace`` (which defaults to ``cwd``).  Per-ship
            working dir is ``<workspace>/ships/<label>``.
        config
            Pre-resolved :class:`PipelineConfig`; loaded on demand when
            ``None``.  Replaces the I:-side ``game_dir`` /
            ``wowsunpack_path`` kwargs.
        forced_label
            Filesystem folder label override (e.g. ``Baltimore_Old``).
            Default: the input name, or the user's choice on ambiguity.
        interactive
            Allow prompting on stdin (ambiguous identity).  Default
            ``False`` (CI / scripted use); the composer raises
            :class:`StepError` rather than blocking.
        class_override
            Override toolkit species mapping
            (``CA``/``CL``/``BB``/``DD``/``CV``/``SS``/...).
        ship_key_suffix
            Trailing ``ship_key`` segment (e.g. ``B`` for hull
            variants).
        build_library
            After ingest, refresh the fleet-wide accessory library.
            Additive -- existing assets are kept.
        rebuild_library
            Implies ``build_library=True``; passes
            ``rebuild=True`` to the library builder (regenerate every
            asset GLB + DDS from scratch).
        and_publish
            After ingest (and library build, if any), publish this
            ship's outputs.  Requires ``publish_target`` to be set.
        publish_target
            Consumer-side destination root.  Required when
            ``and_publish=True``.
        publish_force
            Implies ``and_publish=True``; passes ``force=True`` to the
            publisher (ignore mtime/size cache).
        variant_permoflage
            Mesh-swap permoflage routing mode.  ``"auto"`` (default)
            picks ``Vehicle.nativePermoflage`` automatically when an
            Exterior carries a full hull mesh swap.  Pass an Exterior
            id (e.g. ``PJES478_RED_TAKAO``) to scaffold a non-default
            variant; ``"none"`` to disable.
        toolkit_ship_override
            Override the name passed to ``wowsunpack export-ship`` /
            ``armor-json`` / ``ammo``.  Useful when disambiguation
            forced a model_dir like ``ASC017_Baltimore_1944`` but the
            folder should stay friendly.
        gameparams_ship_id
            Override the GameParams Vehicle id used for autofill +
            permoflage discovery.  Required when multiple Vehicles
            share one model_dir.
        on_event
            Optional progress callback.  Receives :class:`StepEvent`s
            at the canonical step boundaries listed in the module
            docstring; sub-composers' events flow through verbatim.
        cancel
            Optional :class:`threading.Event` for cooperative cancel.
            When set, the next step boundary raises
            :class:`wows_model_export.errors.CancelledError` (a
            :class:`StepError` subclass).  Forwarded verbatim into
            every sub-composer, so a single flag cancels the whole
            pipeline.  Default ``None`` keeps the legacy
            no-cancel-checks behavior for CLI / library callers.

    Returns:
        :class:`IngestResult` wrapping the inner :class:`ScaffoldResult`
        plus the follow-up paths (merged accessories JSON, library
        refresh flag, publish target).

    Raises:
        :class:`StepError`
            On any step failure.  ``.step`` is the canonical step name
            (one of those listed in the module docstring).
            ``.underlying`` holds the original exception; sub-composer
            errors are wrapped so the parent step name surfaces while
            the chain is preserved.
    """
    # ------------------------------------------------------------------
    # Config / paths
    # ------------------------------------------------------------------
    cfg = config or PipelineConfig.load()
    if workspace is None:
        workspace = cfg.workspace
    workspace = Path(workspace).resolve()
    if publish_target is not None:
        publish_target = Path(publish_target).resolve()

    # `rebuild_library` and `publish_force` imply the parent flag.
    if rebuild_library:
        build_library = True
    if publish_force:
        and_publish = True

    if and_publish and publish_target is None:
        raise StepError(
            step="publish",
            underlying=ValueError(
                "and_publish=True requires publish_target to be set"
            ),
            detail="publish_target is unset",
        )

    timer = StepRunner(on_event, cancel=cancel)
    warnings: list[str] = []

    accessories_json_path: Path | None = None
    library_refreshed = False
    published_to: Path | None = None

    # ------------------------------------------------------------------
    # Step: resolve_identity
    # ------------------------------------------------------------------
    timer.start("resolve_identity", detail=ship_input)
    try:
        if toolkit_ship_override:
            toolkit_name = toolkit_ship_override
            label = forced_label or toolkit_ship_override
            timer.complete(
                detail=f"override toolkit={toolkit_name!r} label={label!r}",
                data={"toolkit_name": toolkit_name, "label": label, "override": True},
            )
        else:
            label, toolkit_name = resolve_ship_identity(
                ship_input,
                config=cfg,
                interactive=interactive,
                forced_label=forced_label,
            )
            timer.complete(
                detail=f"label={label!r} toolkit={toolkit_name!r}",
                data={"toolkit_name": toolkit_name, "label": label, "override": False},
            )
    except StepError:
        timer.fail("resolve_identity", detail=f"identity resolution failed for {ship_input!r}")
        raise
    except Exception as e:
        timer.fail("resolve_identity", detail=f"{type(e).__name__}: {e}")
        raise StepError(
            step="resolve_identity",
            underlying=e,
            detail=f"identity resolution failed for {ship_input!r}",
        ) from e

    # ------------------------------------------------------------------
    # Step: prep_dirs
    # ------------------------------------------------------------------
    timer.start("prep_dirs", detail=label)
    try:
        ship_dir = (workspace / "ships" / label).resolve()
        ship_models = ship_dir / _sidecar.MODELS_SUBDIR
        ship_models.mkdir(parents=True, exist_ok=True)
        timer.complete(
            detail=f"{ship_dir}",
            data={
                "ship_dir":         str(ship_dir),
                "ship_models":      str(ship_models),
            },
        )
    except Exception as e:
        timer.fail("prep_dirs", detail=f"{type(e).__name__}: {e}")
        raise StepError(
            step="prep_dirs",
            underlying=e,
            detail=f"failed to prepare working dirs for {label!r}",
        ) from e

    # ------------------------------------------------------------------
    # Step: scaffold (the big sub-composer)
    # ------------------------------------------------------------------
    timer.start(
        "scaffold",
        detail=(
            f"toolkit={toolkit_name!r}"
            + (f" gp={gameparams_ship_id}" if gameparams_ship_id else "")
        ),
    )
    try:
        scaffold_result: ScaffoldResult = _scaffold_ship_mod.scaffold_ship(
            label,
            workspace=workspace,
            config=cfg,
            class_override=class_override,
            ship_key_suffix=ship_key_suffix,
            toolkit_ship=toolkit_name if toolkit_name != label else None,
            gameparams_ship_id=gameparams_ship_id,
            variant_permoflage=variant_permoflage,
            on_event=on_event,
            cancel=cancel,
        )
        warnings.extend(scaffold_result.warnings)
        timer.complete(
            detail=str(scaffold_result.sidecar_path) if scaffold_result.sidecar_path else "",
            data={
                "variant_routed":       scaffold_result.variant_routed,
                "variant_permoflage":   scaffold_result.variant_permoflage,
                "warnings":             len(scaffold_result.warnings),
            },
        )
    except StepError as e:
        timer.fail("scaffold", detail=f"sub-composer failed: step={e.step!r}")
        raise StepError(
            step="scaffold",
            underlying=e,
            detail=f"scaffold_ship({label!r}) failed at step {e.step!r}",
        ) from e
    except Exception as e:
        timer.fail("scaffold", detail=f"{type(e).__name__}: {e}")
        raise StepError(
            step="scaffold",
            underlying=e,
            detail=f"scaffold_ship({label!r}) failed",
        ) from e

    # Compute follow-up paths from the scaffold result.
    placements_json = scaffold_result.placements_json or (
        ship_models / f"{label}_placements.json"
    )
    candidates_json = scaffold_result.skel_ext_json or (
        ship_models / f"{label}_skel_ext.json"
    )
    accessories_json = ship_models / f"{label}_accessories.json"

    def _refresh_sidecar() -> "_scaffold_ship_mod.ScaffoldResult":
        """Re-run scaffold_ship with all heavy steps skipped (sidecar fold only)."""
        return refresh_ship_sidecar(
            label,
            workspace=workspace,
            config=cfg,
            class_override=class_override,
            ship_key_suffix=ship_key_suffix,
            toolkit_ship=toolkit_name if toolkit_name != label else None,
            gameparams_ship_id=gameparams_ship_id,
            variant_permoflage=variant_permoflage,
            on_event=on_event,
            cancel=cancel,
        )

    # ------------------------------------------------------------------
    # Step: resolve_decoratives
    # ------------------------------------------------------------------
    if not candidates_json.is_file():
        timer.skip(
            "resolve_decoratives",
            detail="no skel_ext candidates JSON (HP_-only)",
        )
    else:
        timer.start(
            "resolve_decoratives",
            detail=f"{placements_json.name} + {candidates_json.name}",
        )
        try:
            _skel_ext_resolve_mod.resolve_decorative_placements(
                placements_json,
                candidates_json=candidates_json,
                output_json=accessories_json,
                config=cfg,
                on_event=on_event,
                cancel=cancel,
            )
            accessories_json_path = accessories_json
            timer.complete(detail=str(accessories_json.name))
        except StepError as e:
            timer.fail("resolve_decoratives", detail=f"sub-composer failed: step={e.step!r}")
            raise StepError(
                step="resolve_decoratives",
                underlying=e,
                detail=f"resolve_decorative_placements failed at step {e.step!r}",
            ) from e
        except Exception as e:
            timer.fail("resolve_decoratives", detail=f"{type(e).__name__}: {e}")
            raise StepError(
                step="resolve_decoratives",
                underlying=e,
                detail="skel_ext resolve failed",
            ) from e

        # Sidecar refresh now that accessories.json carries the merged
        # decoratives. The refresh skips the expensive paths (export +
        # armor + ammo + autofill + materials + geometry) and just folds
        # the new decoratives into the sidecar via merge_preserving.
        try:
            refresh_result = _refresh_sidecar()
            warnings.extend(refresh_result.warnings)
            # Re-bind so the consumer sees the post-refresh ScaffoldResult.
            scaffold_result = refresh_result
        except StepError as e:
            raise StepError(
                step="resolve_decoratives",
                underlying=e,
                detail=(
                    f"sidecar refresh after decoratives merge failed at "
                    f"step {e.step!r}"
                ),
            ) from e
        except Exception as e:
            raise StepError(
                step="resolve_decoratives",
                underlying=e,
                detail="sidecar refresh after decoratives merge failed",
            ) from e

    # ------------------------------------------------------------------
    # Step: build_library
    # ------------------------------------------------------------------
    if not build_library:
        timer.skip("build_library", detail="not requested")
    else:
        timer.start(
            "build_library",
            detail=("rebuild" if rebuild_library else "additive"),
        )
        try:
            _accessory_library_mod.build_accessory_library(
                workspace=workspace,
                config=cfg,
                rebuild=rebuild_library,
                on_event=on_event,
                cancel=cancel,
            )
            library_refreshed = True
            timer.complete()
        except StepError as e:
            timer.fail("build_library", detail=f"sub-composer failed: step={e.step!r}")
            raise StepError(
                step="build_library",
                underlying=e,
                detail=f"build_accessory_library failed at step {e.step!r}",
            ) from e
        except Exception as e:
            timer.fail("build_library", detail=f"{type(e).__name__}: {e}")
            raise StepError(
                step="build_library",
                underlying=e,
                detail="build_accessory_library failed",
            ) from e

        # Post-library scaffold refresh -- closes the bone-mismatch race
        # for mesh-swap permoflages (variant accessory GLBs only exist
        # after the library build; the first scaffold pass therefore
        # can't run the Ry(180°) correction against the target GLB
        # extents). No-op for ships without variant swaps.
        if candidates_json.is_file():
            try:
                refresh_result = _refresh_sidecar()
                warnings.extend(refresh_result.warnings)
                scaffold_result = refresh_result
            except StepError as e:
                # Treat post-library refresh failures as warnings, not
                # hard errors -- the library is built, the user can rerun
                # scaffold_ship manually with the skip_* flags set.
                warnings.append(
                    f"post-library scaffold refresh failed at step "
                    f"{e.step!r}: {e.detail or e}"
                )
            except Exception as e:
                warnings.append(
                    f"post-library scaffold refresh failed: "
                    f"{type(e).__name__}: {e}"
                )

    # ------------------------------------------------------------------
    # Step: stamp_provenance
    # ------------------------------------------------------------------
    # Record the args this ingest was called with into the sidecar so a
    # later "clean and re-extract" pass can replay the run lossless-ly
    # (compose.clean_workspace reads this block; falls back to
    # ship.wg_ship_full_id + permoflage="auto" when absent).
    #
    # Soft-fail: we treat a read/write hiccup as a warning rather than
    # aborting the whole ingest — the workspace artifacts are already
    # written by this point and the provenance block is recovery-only.
    sidecar_to_stamp = scaffold_result.sidecar_path
    if sidecar_to_stamp is None or not sidecar_to_stamp.is_file():
        timer.skip("stamp_provenance", detail="sidecar missing")
    else:
        timer.start("stamp_provenance", detail=sidecar_to_stamp.name)
        try:
            doc = _sidecar.read(sidecar_to_stamp)
            doc["provenance"] = {
                "extract_args": {
                    "vehicle":       gameparams_ship_id or toolkit_name,
                    # The resolved toolkit model directory name. `vehicle`
                    # above is the GameParams top_key, which the toolkit's
                    # find_ship REJECTS on replay; this is the identifier
                    # compose.clean_and_reextract feeds back as
                    # `toolkit_ship_override`. (Older sidecars omit it and
                    # fall back to a snapshot join.)
                    "model_dir":     toolkit_name,
                    "label":         label,
                    "permoflage":    variant_permoflage,
                    # Record the library-build INTENT for replay, which can
                    # differ from the value this call ran with: the queue
                    # forces build_library=False per item (the library is
                    # built once at queue drain) but a clean-and-reextract
                    # replay should still rebuild the library, so the queue
                    # passes provenance_build_library=<the user's intent>.
                    "build_library": bool(
                        provenance_build_library
                        if provenance_build_library is not None
                        else build_library
                    ),
                },
                "extracted_at": datetime.now(UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
            _sidecar.write(doc, sidecar_to_stamp)
            timer.complete(detail="extract_args recorded")
        except Exception as e:
            timer.fail("stamp_provenance", detail=f"{type(e).__name__}: {e}")
            warnings.append(
                f"provenance stamp failed ({type(e).__name__}: {e}); "
                f"re-extract via compose.clean_workspace will fall back "
                f"to permoflage=\"auto\" for this ship."
            )

    # ------------------------------------------------------------------
    # Step: publish
    # ------------------------------------------------------------------
    if not and_publish:
        timer.skip("publish", detail="not requested")
    else:
        # `publish_target is None` was already rejected in pre-flight,
        # but assert again for the type checker.
        assert publish_target is not None
        timer.start("publish", detail=str(publish_target))
        try:
            _publish_mod.publish(
                target_dir=publish_target,
                workspace=workspace,
                config=cfg,
                only_ships=(label,),
                force=publish_force,
                on_event=on_event,
                cancel=cancel,
            )
            published_to = publish_target
            timer.complete(detail=str(publish_target))
        except StepError as e:
            timer.fail("publish", detail=f"sub-composer failed: step={e.step!r}")
            raise StepError(
                step="publish",
                underlying=e,
                detail=f"publish failed at step {e.step!r}",
            ) from e
        except Exception as e:
            timer.fail("publish", detail=f"{type(e).__name__}: {e}")
            raise StepError(
                step="publish",
                underlying=e,
                detail=f"publish to {publish_target} failed",
            ) from e

    # Re-bind accessories_json_path off the final scaffold output when
    # the resolve_decoratives step didn't run (HP_-only sidecar) but
    # scaffold itself absorbed an already-merged accessories JSON.
    if accessories_json_path is None and accessories_json.is_file():
        accessories_json_path = accessories_json

    return IngestResult(
        ship_id=toolkit_name,
        label=label,
        workspace_dir=ship_dir,
        scaffold=scaffold_result,
        accessories_json_path=accessories_json_path,
        library_refreshed=library_refreshed,
        published_to=published_to,
        warnings=tuple(warnings),
        step_timings_ms=dict(timer.step_timings_ms),
    )


__all__ = [
    "ingest_ship",
    "resolve_ship_identity",
    "refresh_ship_sidecar",
    "build_library_and_refresh",
]
