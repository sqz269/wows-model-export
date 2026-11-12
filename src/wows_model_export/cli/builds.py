"""Game-update tracking: what changed between builds, which exports are stale.

::

    wows-builds status                     builds on disk, manifests, cache stamps, export states
    wows-builds snapshot [--build N] [--refresh]
    wows-builds diff [--from N] [--to N] [--json] [--limit N]
    wows-builds stale [--kind ship|map|prop_hull] [--all] [--json]
    wows-builds reexport [NAME ...] [--kind ship|map|prop_hull] [--include-untracked]
                         [--dry-run] [--no-skins] [--no-library]
    wows-builds archive [--dry-run] [--json] [--settle-minutes N] [--copier auto|script|builtin]
                        [--log FILE]
    wows-builds archive --install-schedule [--at HH:MM] | --remove-schedule | --schedule-status

``archive`` copies every install build (``bin/<build>/`` with idx files) and
package the build archive lacks into it (``--archive-dir`` /
``WOWS_ARCHIVE_DIR``; see :mod:`wows_model_export.compose.build_archive`).
It only reads the install, never deletes, and does nothing when the archive
is up to date, so ``--install-schedule`` can run it at logon and once a day.
``status``, ``stale`` and ``reexport`` take ``--build N`` to look at the
workspace of an archived build (``<workspace>/builds/<N>/``).

``snapshot`` saves the current build's manifest (fingerprint of every game
file, ``assets.bin`` record, GameParams entity and ``camouflages.xml``
entry) under ``<cache>/builds/<build>/``. The launcher deletes the old
build's idx files one update later, so run it (or any of the other
commands, which snapshot on demand) once per update to keep a diffable
history.

``stale`` compares each export's ``build_deps.json`` against the current
build's manifest. ``reexport`` re-runs stale exports with the arguments
they were made with (``--include-untracked`` also re-runs exports made
before tracking existed, from their sidecar / ``export.json``). With
``--kind map`` or ``--kind prop_hull`` it never rebuilds the accessory
library, which neither kind uses.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from ..compose import build_archive as ba
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


def _archive_summary(cfg) -> dict[str, Any] | None:
    """Archive state for ``status`` (``None`` without an archive)."""
    if cfg.archive_dir is None:
        return None
    doc: dict[str, Any] = {"dir": str(cfg.archive_dir), "builds": tb.archived_builds(cfg)}
    try:
        p = ba.plan(cfg)
        doc["up_to_date"] = p.up_to_date
        doc["unarchived_builds"] = [b.build for b in p.builds if not b.archived]
        doc["packages_to_copy"] = len(p.package_deltas)
    except ConfigError as err:
        doc["error"] = str(err)
    return doc


def _cmd_status(args: argparse.Namespace, cfg) -> int:
    builds = tb.available_builds(cfg)
    current = tb.current_build(cfg)
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
    archive = _archive_summary(cfg)
    source = tb.config_source(cfg)
    doc = {
        "current_build": current,
        "pinned": cfg.build is not None,
        "workspace": str(cfg.workspace),
        "source": source.as_dict() if source else None,
        "builds_with_idx": builds,
        "archive": archive,
        "stored_manifests": stored,
        "caches": caches,
        "exports": {k: dict(v) for k, v in exports.items()},
    }
    if args.json:
        _dump_json(doc)
        return EXIT_OK

    if cfg.build is not None:
        _out(f"pinned build      {current}  (read from the {source.origin if source else '?'}: "
             f"{source.root if source else '?'})")
        _out(f"workspace         {cfg.workspace}")
    else:
        _out(f"current build     {current}")
    _out(f"builds with idx   {', '.join(map(str, builds)) or '-'}")
    if archive is not None:
        line = f"archive           {archive['dir']}: {', '.join(map(str, archive['builds'])) or '-'}"
        if archive.get("error"):
            line += f"  ({archive['error']})"
        elif archive.get("up_to_date"):
            line += "  (every install build and package is archived)"
        else:
            line += (f"  (not yet archived: builds {archive['unarchived_builds'] or '[]'}, "
                     f"{archive['packages_to_copy']} package(s); run `wows-builds archive`)")
        _out(line)
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
    path = bm.ensure_manifest(args.snapshot_build, config=cfg, refresh=args.refresh)
    m = bm.load_manifest(args.snapshot_build, config=cfg, create=False)
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
    candidates = sorted(set(bm.stored_builds(cfg)) | set(tb.known_builds(cfg)))
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
    # Maps and prop hulls use no accessory library: a re-export of either
    # kind never falls back to rebuilding it.
    no_library = args.no_library or args.kind in ("map", "prop_hull")
    if not targets:
        lib = None if (no_library or args.names) else bt.library_status(cfg)
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
        build_library=not no_library,
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
# archive
# ---------------------------------------------------------------------------


def _cmd_archive(args: argparse.Namespace, cfg) -> int:
    if args.schedule_status:
        st = ba.schedule_status()
        if args.json:
            _dump_json(st)
        elif st is None:
            _out(f"no scheduled task '{ba.TASK_NAME}'")
        else:
            for k, v in st.items():
                _out(f"{k:<12} {v}")
        return EXIT_OK
    if args.remove_schedule:
        removed = ba.remove_schedule()
        _out(f"removed the scheduled task '{ba.TASK_NAME}'" if removed
             else f"no scheduled task '{ba.TASK_NAME}' to remove")
        return EXIT_OK
    if args.install_schedule:
        info = ba.install_schedule(cfg, daily_at=args.at)
        if args.json:
            _dump_json(info)
        else:
            _out(f"scheduled task '{info['task']}' ({info['state']}): at logon (+15 min) "
                 f"and daily at {info['daily_at']}")
            _out(f"  runs: {info['program']} {info['arguments']}")
        return EXIT_OK

    res = ba.run(
        cfg,
        dry_run=args.dry_run,
        settle_s=max(0.0, args.settle_minutes) * 60.0,
        copier=args.copier,
        log=None if args.json else _out,
    )
    if args.json:
        _dump_json(res.as_dict(limit=None if args.full else 20))
    elif args.dry_run:
        for b in res.before.builds:
            state = ("archived" if b.archived else
                     f"{len(b.deltas)} of {b.files} file(s) to copy" if b.in_archive else "not archived")
            _out(f"  bin/{b.build:<10} {state}")
        _out(f"  res_packages    {len(res.before.package_deltas)} of {res.before.packages} "
             "package(s) to copy")
    return EXIT_OK if res.ok else EXIT_STEP_ERROR


class _Tee:
    """Write to the original stream (when there is one) and a log file."""

    def __init__(self, stream, log):
        self._stream = stream
        self._log = log

    def write(self, s: str) -> int:
        if self._stream is not None:
            try:
                self._stream.write(s)
            except (OSError, ValueError):
                pass
        self._log.write(s)
        self._log.flush()
        return len(s)

    def flush(self) -> None:
        if self._stream is not None:
            try:
                self._stream.flush()
            except (OSError, ValueError):
                pass


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
    p.add_argument("--build", dest="snapshot_build", type=int, default=None,
                   help="Build to fingerprint: the install's, or an archived one.")
    p.add_argument("--refresh", action="store_true", help="Rebuild even if saved.")
    add_common_args(p, build_option=False)

    p = sub.add_parser("diff", help="What changed between two builds.")
    p.add_argument("--from", dest="from_", type=int, default=None,
                   help="Older build (default: the newest one before --to).")
    p.add_argument("--to", type=int, default=None, help="Newer build (default: current).")
    p.add_argument("--json", action="store_true", help="Full wows.build_diff/v1 document.")
    p.add_argument("--limit", type=int, default=40, help="Max names per list (0 = all).")
    add_common_args(p, build_option=False)

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

    p = sub.add_parser(
        "archive",
        help="Copy new install builds and packages into the build archive (append-only).",
        description="Copy every install build (bin/<build>/ with idx files) and package "
                    "the archive lacks into it. Reads the install only and never deletes; "
                    "a no-op when the archive is up to date.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Only report what is not archived yet.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--full", action="store_true", help="With --json: list every file to copy.")
    p.add_argument("--settle-minutes", type=float, default=ba.DEFAULT_SETTLE_S / 60.0,
                   help="Copy only once no file to copy has changed for this long "
                        "(default %(default).0f; a game update may still be writing).")
    p.add_argument("--copier", choices=("auto", "script", "builtin"), default="auto",
                   help="auto: the archive's snapshot.ps1 when it has one (Windows), "
                        "else the built-in copier.")
    p.add_argument("--log", type=Path, default=None,
                   help="Also append the output to this file (for scheduled runs).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--install-schedule", action="store_true",
                   help="Register a Windows scheduled task that runs this at logon "
                        "(+15 min) and daily.")
    g.add_argument("--remove-schedule", action="store_true",
                   help="Unregister that scheduled task.")
    g.add_argument("--schedule-status", action="store_true",
                   help="Show the scheduled task's state and last run.")
    p.add_argument("--at", default="05:00", help="Daily start time for --install-schedule.")
    add_common_args(p, build_option=False)
    return ap


_COMMANDS = {
    "status": _cmd_status,
    "snapshot": _cmd_snapshot,
    "diff": _cmd_diff,
    "stale": _cmd_stale,
    "reexport": _cmd_reexport,
    "archive": _cmd_archive,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    log_path = getattr(args, "log", None)
    if log_path is None:
        return _run(args)
    # Scheduled runs go through pythonw, which has no stdout or stderr.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    out, err = sys.stdout, sys.stderr
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n== {datetime.now():%Y-%m-%d %H:%M:%S} wows-builds {args.cmd}\n")
        sys.stdout, sys.stderr = _Tee(out, log), _Tee(err, log)
        try:
            rc = _run(args)
            log.write(f"== exit {rc}\n")
            return rc
        finally:
            sys.stdout, sys.stderr = out, err


def _run(args: argparse.Namespace) -> int:
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
