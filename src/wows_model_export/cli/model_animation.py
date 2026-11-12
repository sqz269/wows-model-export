"""Write the animation document (``wows_animation/v1``) of any WoWS model.

The aircraft library writes these for its airframes; this command writes
one for any other model (a ship accessory such as a radar, a ship part, a
map prop) beside a GLB the toolkit exported from it::

    python -m wows_model_export.cli.model_animation MODEL --glb GLB [--out JSON]
        [--census] [common flags ...]

MODEL is the model's VFS path (``content/.../X.model``; a ``.geometry`` or
``.visual`` path is taken as its ``.model``). The channels are checked
against GLB's nodes and rest pose; ``--out`` defaults to
``<GLB stem>.animation.json`` beside the GLB. ``--census`` prints, instead,
how many models of the game files load a state machine, by top folder.
``--ship LABEL`` (re)writes the documents of a workspace ship's hull GLBs
and its sidecar's ``animation`` section, as an ingest does
(:mod:`wows_model_export.compose.ship_animation`).
Specified in ``reference/schemas/METADATA_SPEC.md`` §18.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

from .._glb import parse_glb
from ..compose import model_animation
from ..errors import ConfigError
from ._args import EXIT_CONFIG_ERROR, EXIT_OK, EXIT_STEP_ERROR, add_common_args, resolve_config


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m wows_model_export.cli.model_animation",
        description="Write a model's animation document (wows_animation/v1) beside its GLB.",
    )
    ap.add_argument("model", nargs="?", help="VFS path of the model (.model, .geometry or .visual).")
    ap.add_argument("--glb", type=Path, help="The toolkit GLB of that model.")
    ap.add_argument("--out", type=Path, default=None, help="Output JSON (default: beside the GLB).")
    ap.add_argument("--census", action="store_true",
                    help="Count the models that load a state machine, by top folder, and exit.")
    ap.add_argument("--ship", metavar="LABEL", action="append",
                    help="Write the hull animation documents and sidecar section of "
                         "<workspace>/ships/LABEL (repeatable), and exit.")
    add_common_args(ap)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if args.ship:
        from ..compose import ship_animation
        if cfg.workspace is None:
            print("config error: no workspace configured", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        failed = 0
        for label in args.ship:
            warnings: list[str] = []
            try:
                r = ship_animation.animate_ship(Path(cfg.workspace) / "ships" / label, config=cfg,
                                                warnings=warnings)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"{label}: failed: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            for w in warnings:
                print(f"  {label}: {w}", file=sys.stderr)
            print(f"{label}: {r['hulls']} hull GLB(s) with animation, {r['clips']} clip(s), "
                  f"{r['missing_node_clips']} on nodes the GLB lacks; sidecar "
                  f"{'written' if r['written'] else 'unchanged'}")
        return EXIT_STEP_ERROR if failed else EXIT_OK
    from ..read import model_prototypes
    from ..toolkit import assets_bin
    ab = assets_bin.ensure_dump(config=cfg)
    if args.census:
        counts = model_prototypes.model_state_machine_census(ab)
        by_top = collections.Counter("/".join(m.split("/")[:4]) for m in counts)
        for top, n in by_top.most_common():
            print(f"{n:6d}  {top}")
        print(f"{len(counts)} model(s) load a state machine", file=sys.stderr)
        return EXIT_OK
    if not args.model or not args.glb:
        print("error: MODEL and --glb are required (or --census)", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    model = args.model.replace("\\", "/").lstrip("/").rsplit(".", 1)[0] + ".model"
    machines = model_prototypes.model_state_machines(ab, [model])[model]
    gltf, _ = parse_glb(args.glb.read_bytes())
    doc, problems = model_animation.build_document(
        model, machines, gltf=gltf, root=model_animation.cache_root(cfg), config=cfg,
        glb_name=args.glb.name)
    for p in problems:
        print(f"  problem: {p}", file=sys.stderr)
    if doc is None:
        print(f"{model}: loads no state machine; nothing written", file=sys.stderr)
        return EXIT_OK
    out = args.out or args.glb.with_name(f"{args.glb.stem}.animation.json")
    model_animation.write_document(doc, out)
    bad = model_animation.validate_document(doc, gltf)
    for p in bad:
        print(f"  invalid: {p}", file=sys.stderr)
    summary = model_animation.clip_summary(doc)
    print(json.dumps({"file": str(out), "state_machines": [m["vfs"] for m in doc["state_machines"]],
                      "clips": {k: {x: v[x] for x in ("duration_s", "loop", "nodes", "starts_at_rest",
                                                       "ends_at_rest", "triggers")}
                                for k, v in summary.items()}}, indent=1))
    return EXIT_OK if not (problems or bad) else EXIT_STEP_ERROR


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
