"""Game-update tracking: what changed between builds, which exports are stale.

::

    wows-builds status                     builds on disk, manifests, cache stamps, export states
    wows-builds snapshot [--build N] [--refresh]
    wows-builds diff [--from N] [--to N] [--json] [--limit N]
    wows-builds stale [--kind ship|map|prop_hull] [--all] [--json]
    wows-builds reexport [NAME ...] [--kind ship|map|prop_hull] [--include-untracked]
                         [--dry-run] [--no-skins] [--no-library]

``snapshot`` saves the current build's manifest (fingerprint of every game
file, ``assets.bin`` record, GameParams entity and ``camouflages.xml``
entry) under ``<cache>/builds/<build>/``. The launcher deletes the old
build's idx files one update later, so run it (or any of the other
commands, which snapshot on demand) once per update to keep a diffable
history.

``stale`` compares each export's ``build_deps.json`` against the current
build's manifest. ``reexport`` re-runs stale exports with the arguments
they were made with (``--include-untracked`` also re-runs exports made
before tracking existed, from their sidecar / ``export.json``).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections import Counter
from typing import Any

from ..compose import build_manifest as bm
from ..compose import build_tracking as bt
from ..errors import ConfigError
from ..toolkit import build as tb
from ._args import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_STEP_ERROR,
    EXIT_UNEXPECTED,
    add_common_args,
    build_printer,
    resolve_config,
)

_CACHE_FILES = (
    "gameparams.json",
    "assets.bin",
    "camouflages.xml",
    "wows_manifest.json",
    "skel_ext_hashes.json",
)


def _out(msg: str = "") -> None:
    print(msg)


def _dump_json(doc: Any) -> None:
    json.dump(doc, sys.stdout, indent=1)
    sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _cmd_status(args: argparse.Namespace, cfg) -> int:
    builds = tb.available_builds(cfg)
    current = builds[-1] if builds else None
    stored = bm.stored_builds(cfg)
    cache = cfg.require_cache_dir()
    caches = {}
    for name in _CACHE_FILES:
        path = cache / name
        caches[name] = {
            "exists": path.is_file(),
            "build": tb.read_stamp(path),
            "current": path.is_file() and tb.read_stamp(path) == current,
        }
    exports: dict[str, Counter[str]] = {}
    if current is not None and current in stored:
        _, statuses = bt.workspace_status(config=cfg)
        for st in statuses:
            exports.setdefault(st.kind, Counter())[st.state] += 1
    doc = {
        "current_build": current,
        "builds_with_idx": builds,
        "stored_manifests": stored,
        "caches": caches,
        "exports": {k: dict(v) for k, v in exports.items()},
    }
    if args.json:
        _dump_json(doc)
        return EXIT_OK

    _out(f"current build     {current}")
    _out(f"builds with idx   {', '.join(map(str, builds)) or '-'}")
    _out(f"saved manifests   {', '.join(map(str, stored)) or '-'}")
    if current is not None and current not in stored:
        _out("                  (no manifest for the current build yet: run `wows-builds snapshot`)")
    _out("")
    _out("caches")
    for name, c in caches.items():
        if not c["exists"]:
            state = "absent"
        elif c["current"]:
            state = "current"
        elif c["build"] is None:
            state = "unstamped (rebuilt on next use)"
        else:
            state = f"build {c['build']} (rebuilt on next use)"
        _out(f"  {name:<22} {state}")
    if exports:
        _out("")
        _out("exports")
        for kind, counts in sorted(exports.items()):
            parts = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
            _out(f"  {kind + 's':<8} {parts}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def _cmd_snapshot(args: argparse.Namespace, cfg) -> int:
    path = bm.ensure_manifest(args.build, config=cfg, refresh=args.refresh)
    m = bm.load_manifest(args.build, config=cfg, create=False)
    _out(f"build {m.build}: {path}")
    _out(f"  {len(m.files)} files, {len(m.records)} records, "
         f"{len(m.gameparams)} GameParams entities, {len(m.camouflages)} camouflage entries")
    bad = {k: v for k, v in m.sections.items() if v not in ("ok", "decoded")}
    for k, v in bad.items():
        _out(f"  {k}: {v}")
    if args.refresh:
        n = bt.refresh_fingerprints(m.build, config=cfg)
        if n:
            _out(f"  re-fingerprinted {n} export(s) made on build {m.build}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def _pick_builds(args: argparse.Namespace, cfg) -> tuple[int, int]:
    to_build = args.to or tb.current_build(cfg)
    if to_build is None:
        raise ConfigError("no game build with idx files found")
    if args.from_:
        return args.from_, to_build
    candidates = sorted(set(bm.stored_builds(cfg)) | set(tb.available_builds(cfg)))
    older = [b for b in candidates if b < to_build]
    if not older:
        raise ConfigError(
            f"no build older than {to_build} on disk or in saved manifests; "
            "nothing to diff against"
        )
    return older[-1], to_build


def _short(paths: list[str], limit: int) -> list[str]:
    if limit <= 0 or len(paths) <= limit:
        return paths
    return paths[:limit] + [f"... and {len(paths) - limit} more"]


def _cmd_diff(args: argparse.Namespace, cfg) -> int:
    old_b, new_b = _pick_builds(args, cfg)
    old = bm.load_manifest(old_b, config=cfg)
    new = bm.load_manifest(new_b, config=cfg)
    diff = bm.diff_manifests(old, new)
    if args.json:
        _dump_json(diff)
        return EXIT_OK

    lim = args.limit
    s = diff["summary"]
    _out(f"build {old_b} -> {new_b}")
    _out("")
    for section in ("files", "records", "gameparams", "camouflages"):
        c = s[section]
        _out(f"  {section:<12} +{c['added']:<6} -{c['removed']:<6} ~{c['changed']}")
    if s["records_changed_by_type"]:
        types = ", ".join(f"{t} {n}" for t, n in sorted(
            s["records_changed_by_type"].items(), key=lambda kv: -kv[1]))
        _out(f"  records changed by type: {types}")
    if diff["incomparable"]:
        _out(f"  hashing differs between the two manifests, changes not compared for: "
             f"{', '.join(diff['incomparable'])}")
        _out(f"  (rebuild the older one with `wows-builds snapshot --build {old_b} --refresh` "
             "while its idx files are still installed)")

    if diff["format_signals"]:
        _out("")
        _out("format signals (nearly every file of a kind changed: check the parser)")
        for sig in diff["format_signals"]:
            _out(f"  {sig['kind']:<28} {sig['changed']}/{sig['total']}")

    _out("")
    _out("files changed by area")
    for area, n in diff["by_area"]["files_changed"].items():
        _out(f"  {area:<32} {n}")

    spaces = diff["spaces"]
    if spaces:
        _out("")
        _out(f"maps touched ({len(spaces)})")
        for name, kinds in spaces.items():
            parts = ", ".join(f"{k} {len(v)}" for k, v in kinds.items())
            _out(f"  {name:<36} {parts}")

    ships = diff["ship_model_dirs"]
    if ships:
        _out("")
        _out(f"ship model dirs touched ({len(ships)})")
        for name in _short(list(ships), lim):
            _out(f"  {name}" + (f"  ({ships[name]})" if name in ships else ""))

    particles = [
        p for p in diff["records"]["changed"] + diff["records"]["added"]
        if (new.records.get(p) or ["", "", ""])[2] in bm.PARTICLE_TYPES
    ]
    if particles:
        _out("")
        _out(f"particle records changed or added ({len(particles)})")
        for p in _short(particles, lim):
            _out(f"  {p}")

    gp = diff["gameparams"]
    if gp["added"] or gp["changed"]:
        _out("")
        _out(f"GameParams added ({len(gp['added'])})")
        for k in _short(gp["added"], lim):
            _out(f"  {k}")
        _out(f"GameParams changed ({len(gp['changed'])})")
        for k in _short(gp["changed"], lim):
            _out(f"  {k}")

    camo = diff["camouflages"]
    if any(camo.values()):
        _out("")
        _out("camouflages.xml entries")
        for kind in ("added", "removed", "changed"):
            for k in _short(camo[kind], lim):
                _out(f"  {kind:<8} {k}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# stale / reexport selection
# ---------------------------------------------------------------------------


def _select(args: argparse.Namespace, cfg, *, include_untracked: bool) -> list[bt.ExportStatus]:
    _, statuses = bt.workspace_status(config=cfg)
    names = set(getattr(args, "names", None) or ())
    out = []
    for st in statuses:
        if args.kind and st.kind != args.kind:
            continue
        if names:
            if st.name in names:
                out.append(st)
            continue
        if st.state == "stale" or (include_untracked and st.state in ("untracked", "error")):
            out.append(st)
    if names:
        missing = names - {st.name for st in out}
        if missing:
            raise ConfigError(f"no export named {', '.join(sorted(missing))}")
    return out


def _cmd_stale(args: argparse.Namespace, cfg) -> int:
    manifest, statuses = bt.workspace_status(config=cfg)
    if args.kind:
        statuses = [s for s in statuses if s.kind == args.kind]
    shown = statuses if args.all else [s for s in statuses if s.state != "current"]
    library = bt.library_status(cfg, manifest) if args.kind in (None, "ship") else None
    if args.json:
        _dump_json({
            "build": manifest.build,
            "exports": [s.as_dict(limit=None if args.all else 50) for s in shown],
            "library": library,
        })
        return EXIT_OK

    counts = Counter(s.state for s in statuses)
    _out(f"build {manifest.build}: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    stale = [s for s in shown if s.state == "stale"]
    if stale:
        _out("")
        _out("stale")
        for st in stale:
            _out(f"  {st.kind:<4} {st.name}  (exported on {st.build}, {st.changed_count} inputs changed)")
            for section, keys in st.changed.items():
                if keys:
                    _out(f"         {section}: " + ", ".join(_short(keys, args.limit)))
            for section, keys in st.removed.items():
                if keys:
                    _out(f"         {section} removed: " + ", ".join(_short(keys, args.limit)))
    unchecked = [s for s in shown if s.incomparable]
    if unchecked:
        _out("")
        _out("partly unchecked (hashing changed since export)")
        for st in unchecked:
            _out(f"  {st.kind:<4} {st.name}  {st.note}")
    untracked = [s for s in shown if s.state in ("untracked", "error")]
    if untracked:
        _out("")
        _out(f"untracked ({len(untracked)}): exported before build tracking; "
             "`wows-builds reexport --include-untracked` re-runs them")
        for st in untracked:
            when = f"  {st.exported_at}" if st.exported_at else ""
            note = f"  [{st.note}]" if st.state == "error" else ""
            _out(f"  {st.kind:<4} {st.name}{when}{note}")
    removed = [s for s in shown if s.state == "removed"]
    if removed:
        _out("")
        _out(f"removed from the game ({len(removed)}): can't be re-exported")
        for st in removed:
            _out(f"  {st.kind:<4} {st.name}  [{st.note}]")
    if library is not None:
        lib_stale = library["stale"]
        _out("")
        _out(f"accessory library: {library['current']} current, {len(lib_stale)} stale, "
             f"{library['untracked']} without a build record"
             + (f", {library['unused']} unused by any ship" if library.get("unused") else ""))
        if lib_stale or library["untracked"]:
            _out("  rebuilt by the next library build (`wows-builds reexport`, or "
                 "`wows-build-accessory-library`)")
        n = args.limit or len(lib_stale)
        for entry in lib_stale[:n]:
            _out(f"  {entry['asset_id']}  ({entry['reason']})")
        if len(lib_stale) > n:
            _out(f"  ... {len(lib_stale) - n} more")
    if args.all:
        current = [s for s in shown if s.state == "current"]
        if current:
            _out("")
            _out(f"current ({len(current)})")
            for st in current:
                _out(f"  {st.kind:<4} {st.name}")
    return EXIT_OK


def _cmd_reexport(args: argparse.Namespace, cfg) -> int:
    targets = _select(args, cfg, include_untracked=args.include_untracked)
    if not targets:
        lib = None if (args.no_library or args.names or args.kind == "map") else bt.library_status(cfg)
        pending = (len(lib["stale"]) + lib["untracked"]) if lib else 0
        if not pending or args.dry_run:
            _out("nothing to re-export" + (
                f"; the library build would rebuild {pending} accessory asset(s)" if pending else ""))
            return EXIT_OK
        _out(f"no exports to re-export; rebuilding {pending} out-of-date accessory library asset(s)")
    if args.dry_run:
        plans = []
        for st in targets:
            try:
                plans.append({"state": st.state, **bt.reexport_plan(st, cfg)})
            except Exception as err:  # noqa: BLE001
                plans.append({"kind": st.kind, "name": st.name, "state": st.state,
                              "error": f"{type(err).__name__}: {err}"})
        if args.json:
            _dump_json(plans)
        else:
            for p in plans:
                _out(json.dumps(p))
        return EXIT_OK

    def progress(i: int, total: int, st: bt.ExportStatus) -> None:
        print(f"[{i + 1}/{total}] {st.kind} {st.name} ({st.state})", file=sys.stderr)

    report = bt.reexport_exports(
        targets,
        config=cfg,
        replay_skins=not args.no_skins,
        build_library=not args.no_library,
        jobs=args.jobs,
        on_event=build_printer(args),
        on_progress=progress,
    )
    if args.json:
        _dump_json(report)
    else:
        attempted = len(targets) - len(report["skipped"])
        _out(f"re-exported {len(report['reexported'])}/{attempted}")
        for s in report["skipped"]:
            _out(f"skipped {s['kind']} {s['name']}: {s['reason']}")
        if report["skins_replayed"]:
            _out(f"skin packs replayed: {len(report['skins_replayed'])}")
        if report["library"]:
            lib = report["library"]
            _out(f"accessory library: {'ok' if lib['ok'] else 'FAILED ' + lib.get('error', '')}")
        for f in report["failed"]:
            _out(f"FAILED {f['kind']} {f['name']}: {f['error']}")
    return EXIT_STEP_ERROR if report["failed"] else EXIT_OK


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-builds",
        description="Diff game builds and find / re-run exports a game update made stale.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="Builds, saved manifests, cache stamps, export states.")
    p.add_argument("--json", action="store_true")
    add_common_args(p)

    p = sub.add_parser("snapshot", help="Save a build's manifest (default: current build).")
    p.add_argument("--build", type=int, default=None)
    p.add_argument("--refresh", action="store_true", help="Rebuild even if saved.")
    add_common_args(p)

    p = sub.add_parser("diff", help="What changed between two builds.")
    p.add_argument("--from", dest="from_", type=int, default=None,
                   help="Older build (default: the newest one before --to).")
    p.add_argument("--to", type=int, default=None, help="Newer build (default: current).")
    p.add_argument("--json", action="store_true", help="Full wows.build_diff/v1 document.")
    p.add_argument("--limit", type=int, default=40, help="Max names per list (0 = all).")
    add_common_args(p)

    p = sub.add_parser("stale", help="Exports whose game inputs changed since they were made.")
    p.add_argument("--kind", choices=("ship", "map", "prop_hull"), default=None)
    p.add_argument("--all", action="store_true", help="Also list current exports.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=8, help="Max changed inputs shown per section.")
    add_common_args(p)

    p = sub.add_parser("reexport", help="Re-run stale exports with their original arguments.")
    p.add_argument("names", nargs="*", help="Only these exports (ship label / map name).")
    p.add_argument("--kind", choices=("ship", "map", "prop_hull"), default=None)
    p.add_argument("--include-untracked", action="store_true",
                   help="Also re-run exports made before build tracking.")
    p.add_argument("--dry-run", action="store_true", help="Print the plan only.")
    p.add_argument("--no-skins", action="store_true", help="Don't replay skin packs.")
    p.add_argument("--no-library", action="store_true",
                   help="Skip the accessory-library build at the end.")
    p.add_argument("--jobs", "-j", type=int, default=bt.DEFAULT_REEXPORT_JOBS,
                   help=f"Exports to run at once (default {bt.DEFAULT_REEXPORT_JOBS}).")
    p.add_argument("--json", action="store_true")
    add_common_args(p)
    return ap


_COMMANDS = {
    "status": _cmd_status,
    "snapshot": _cmd_snapshot,
    "diff": _cmd_diff,
    "stale": _cmd_stale,
    "reexport": _cmd_reexport,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
        return _COMMANDS[args.cmd](args, cfg)
    except ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt:
        return EXIT_STEP_ERROR
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
