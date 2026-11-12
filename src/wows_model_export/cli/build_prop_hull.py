"""CLI for prop hulls: a minimal ship sidecar around a static prop model.

    wows-build-prop-hull OSV056_Spitz_Schute --prop OSV056_Spitz_Schute --name "Spitzschute barge"
    wows-build-prop-hull MyBarge --model content/ports/ship/service/OSC007/OSC007.model
    wows-build-prop-hull MyBoat --glb path/to/boat.glb --drop-nodes "helper|collision"
    wows-build-prop-hull --spec prop_hulls.json [--id ID ...]
    wows-build-prop-hull --validate [ID ...]          # every prop hull when no id is given

Outputs land under ``<workspace>/ships/<id>/`` in the ship export layout;
the spec format and the sidecar rules are documented in
``wows_model_export.prop_hull`` and ``reference/schemas/METADATA_SPEC.md``
section 17.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..errors import ConfigError, ToolkitError
from ..prop_hull import PropHullError, build_prop_hull, run_spec, validate_workspace
from ._args import add_build_args, resolve_config


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-build-prop-hull",
        description="Wrap a static prop model in a minimal ship sidecar (a prop hull) under <workspace>/ships/<id>/.",
    )
    ap.add_argument("id", nargs="*", help="prop hull id (the ships/<id>/ folder); with --spec or --validate, a filter")
    ap.add_argument("--id", nargs="+", dest="more_ids", default=[], metavar="ID",
                    help="same as the positional ids (as wows-compose-building spells it)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--prop", help="a WoWS map / port prop id (resolved to its .model through the VFS manifest)")
    src.add_argument("--model", help="a WoWS .model VFS path (old format: a loose .geometry beside it)")
    src.add_argument("--glb", help="a static .glb / .gltf on disk (relative: against the workspace root)")
    src.add_argument("--spec", type=Path, help="a spec JSON (wows_prop_hulls/v1) listing several props")
    src.add_argument("--validate", action="store_true", help="only check prop hulls already in the workspace")
    ap.add_argument("--name", help="display name (default: the id)")
    ap.add_argument("--nation", default=None, help="ship.nation (default: common)")
    ap.add_argument("--class", dest="cls", default=None, help="ship.class (default: AUX)")
    ap.add_argument("--scale", type=float, default=None, help="uniform scale on the Hull node (default 1.0)")
    ap.add_argument("--drop-nodes", help="regex: leave out nodes whose name matches, with their subtree")
    ap.add_argument("--drop-materials", help="regex: leave out primitives whose material name matches "
                                             "(default: ^WATEROCCLUDER$)")
    ap.add_argument("--note", action="append", default=[], help="a note recorded in prop_hull.notes (repeatable)")
    ap.add_argument("--skip-export", action="store_true",
                    help="keep the hull GLB on disk; only recompose the sidecar and placements doc")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    add_build_args(ap)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.id = list(args.id) + list(args.more_ids)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        if args.validate:
            result = validate_workspace(config=cfg, ids=args.id or None)
            bad = {k: v for k, v in result.items() if v}
            for pid, problems in sorted(result.items()):
                print(f"{pid}: " + ("valid" if not problems else f"{len(problems)} problem(s)"))
                for p in problems:
                    print(f"  - {p}")
            if not result:
                print("no prop hulls in the workspace")
            return 1 if bad else 0
        if args.spec:
            report = run_spec(args.spec, ids=args.id or None, skip_export=args.skip_export, config=cfg)
        else:
            if len(args.id) != 1 or not (args.prop or args.model or args.glb):
                print("error: give one id and one of --prop / --model / --glb (or --spec / --validate)",
                      file=sys.stderr)
                return 2
            source = {"prop": args.prop} if args.prop else {"model": args.model} if args.model else {"glb": args.glb}
            entry = {"id": args.id[0], "source": source}
            for key, val in (("name", args.name), ("nation", args.nation), ("class", args.cls),
                             ("scale", args.scale), ("drop_nodes", args.drop_nodes),
                             ("drop_materials", args.drop_materials)):
                if val is not None:
                    entry[key] = val
            if args.note:
                entry["notes"] = list(args.note)
            report = {"props": {args.id[0]: build_prop_hull(entry, config=cfg, skip_export=args.skip_export)}}
    except (PropHullError, ToolkitError, ConfigError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=1, default=str))
    problems = sum(len(r.get("problems") or []) for r in report["props"].values())
    print(f"done: {len(report['props'])} prop hull(s), {problems} validation problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
