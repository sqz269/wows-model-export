"""Append-only archive of game builds.

The install keeps only the newest two builds' idx files (``bin/<build>/idx``;
the launcher deletes the older one with the next update), and an update can
replace or drop packages. The archive keeps every build the pipeline has
seen, laid out like the install, so the toolkit can still read any of them
(``--idx-files <archive>/bin/<build>/idx --pkg-dir <archive>/res_packages``;
in the pipeline, ``--build <build>``)::

    <archive>/bin/<build>/     the install's bin/<build>/ folder, for every
                               build that had idx files when it was copied
    <archive>/res_packages/    every .pkg the install has had; never deleted
    <archive>/logs/            one log per copy

:func:`plan` compares the install with the archive: the files of each
``bin/<build>/`` that has idx files, and every package, missing from the
archive or differing (size, or newer in the install); the client's runtime
logs (``*.log``) are not compared. :func:`run` copies
what the plan lists. It uses the archive's own ``snapshot.ps1`` when it has
one (robocopy ``/E /XO``, which only adds and updates), else a built-in
copier that writes each new or changed file under a temporary name and
renames it into place. Neither ever deletes a file or writes to the install.

A run is a no-op when the plan is empty, so it can run on a timer. It waits
while the install is still changing (any file it would copy written in the
last ``settle_s`` seconds, as during a game update) and lets one run at a
time hold ``logs/.archive.lock``. :func:`install_schedule` registers a
Windows scheduled task that runs ``wows-builds archive`` at logon and once a
day.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..config import PipelineConfig
from ..errors import ConfigError
from ..toolkit import build as _build

SCHEMA = "wows.build_archive_plan/v1"
SNAPSHOT_SCRIPT = "snapshot.ps1"
LOCK_NAME = ".archive.lock"
# A lock older than this belongs to a run that died; the next run takes it.
LOCK_STALE_S = 6 * 3600.0
# Files written this recently mean the install is still changing.
DEFAULT_SETTLE_S = 30 * 60.0
# The archive filesystem may round mtimes; an install file counts as newer
# only past this.
_MTIME_SLACK_S = 2.0
# Runtime logs the client rewrites in bin/<build>/bin64 each time it runs
# (debug.log, gettext_proxy.log): not build data, never compared.
_VOLATILE_SUFFIXES = (".log",)

TASK_NAME = "wows-model-export build archive"

Reason = Literal["missing", "size", "newer"]


@dataclass(frozen=True)
class FileDelta:
    """One install file the archive lacks or holds an older copy of."""

    rel: str
    size: int
    mtime: float
    reason: Reason

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.rel, "size": self.size, "reason": self.reason}


@dataclass
class BuildPlan:
    """One install build (``bin/<build>/`` with idx files) against the archive."""

    build: int
    files: int
    in_archive: bool
    deltas: list[FileDelta] = field(default_factory=list)

    @property
    def archived(self) -> bool:
        return self.in_archive and not self.deltas

    def as_dict(self, limit: int | None = 20) -> dict[str, Any]:
        d = self.deltas if limit is None else self.deltas[:limit]
        return {
            "build": self.build,
            "files": self.files,
            "in_archive": self.in_archive,
            "archived": self.archived,
            "to_copy": len(self.deltas),
            "bytes_to_copy": sum(x.size for x in self.deltas),
            "deltas": [x.as_dict() for x in d],
        }


@dataclass
class ArchivePlan:
    game_dir: Path
    archive_dir: Path
    install_builds: list[int]
    archived_builds: list[int]
    builds: list[BuildPlan]
    packages: int
    package_deltas: list[FileDelta]
    made_at: float = field(default_factory=time.time)

    @property
    def deltas(self) -> Iterator[FileDelta]:
        for b in self.builds:
            yield from b.deltas
        yield from self.package_deltas

    @property
    def up_to_date(self) -> bool:
        return not any(True for _ in self.deltas)

    @property
    def bytes_to_copy(self) -> int:
        return sum(x.size for x in self.deltas)

    @property
    def newest_change_age_s(self) -> float | None:
        """Seconds since the newest install file the plan would copy was
        written (``None`` when nothing is to copy)."""
        mtimes = [x.mtime for x in self.deltas]
        return (self.made_at - max(mtimes)) if mtimes else None

    def settled(self, settle_s: float) -> bool:
        age = self.newest_change_age_s
        return age is None or age >= settle_s

    def as_dict(self, limit: int | None = 20) -> dict[str, Any]:
        pk = self.package_deltas if limit is None else self.package_deltas[:limit]
        return {
            "schema": SCHEMA,
            "game_dir": str(self.game_dir),
            "archive_dir": str(self.archive_dir),
            "install_builds": self.install_builds,
            "archived_builds": self.archived_builds,
            "up_to_date": self.up_to_date,
            "unarchived_builds": [b.build for b in self.builds if not b.archived],
            "builds": [b.as_dict(limit) for b in self.builds],
            "packages": {
                "install": self.packages,
                "to_copy": len(self.package_deltas),
                "bytes_to_copy": sum(x.size for x in self.package_deltas),
                "deltas": [x.as_dict() for x in pk],
            },
            "bytes_to_copy": self.bytes_to_copy,
            "newest_change_age_s": self.newest_change_age_s,
        }


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _walk(root: Path) -> Iterator[tuple[str, os.stat_result]]:
    """Every file under ``root`` as (path relative to ``root`` with ``/``,
    stat)."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        if e.name.lower().endswith(_VOLATILE_SUFFIXES):
                            continue
                        rel = Path(e.path).relative_to(root).as_posix()
                        yield rel, e.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue


def _compare(src_root: Path, dst_root: Path, rels: Iterator[tuple[str, os.stat_result]],
             prefix: str) -> tuple[int, list[FileDelta]]:
    n = 0
    deltas: list[FileDelta] = []
    for rel, st in rels:
        n += 1
        dst = dst_root / rel
        try:
            dst_st = dst.stat()
        except FileNotFoundError:
            deltas.append(FileDelta(f"{prefix}{rel}", st.st_size, st.st_mtime, "missing"))
            continue
        if dst_st.st_size != st.st_size:
            deltas.append(FileDelta(f"{prefix}{rel}", st.st_size, st.st_mtime, "size"))
        elif st.st_mtime > dst_st.st_mtime + _MTIME_SLACK_S:
            deltas.append(FileDelta(f"{prefix}{rel}", st.st_size, st.st_mtime, "newer"))
    return n, deltas


def _roots(config: PipelineConfig) -> tuple[Path, Path]:
    game = config.require_game_dir()
    archive = config.require_archive_dir()
    if not (game / "bin").is_dir():
        raise ConfigError(f"no bin/ folder in the game install {game}")
    if game.resolve() == archive.resolve():
        raise ConfigError("the archive directory is the game install")
    return game, archive


def plan(config: PipelineConfig | None = None) -> ArchivePlan:
    """Compare the install with the archive. Reads only file metadata."""
    cfg = (config or PipelineConfig.load()).for_build(None)
    game, archive = _roots(cfg)
    builds: list[BuildPlan] = []
    install_builds = _build.available_builds(cfg)
    for b in install_builds:
        src = game / "bin" / str(b)
        dst = archive / "bin" / str(b)
        n, deltas = _compare(src, dst, _walk(src), f"bin/{b}/")
        builds.append(BuildPlan(build=b, files=n, in_archive=(dst / "idx").is_dir(), deltas=deltas))
    pkg_src = game / "res_packages"
    pkgs = [(p.name, p.stat()) for p in pkg_src.glob("*.pkg")] if pkg_src.is_dir() else []
    n_pkg, pkg_deltas = _compare(pkg_src, archive / "res_packages", iter(pkgs), "res_packages/")
    return ArchivePlan(
        game_dir=game,
        archive_dir=archive,
        install_builds=install_builds,
        archived_builds=_build.archived_builds(cfg),
        builds=builds,
        packages=n_pkg,
        package_deltas=pkg_deltas,
    )


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------


@contextmanager
def _run_lock(archive: Path) -> Iterator[bool]:
    """Exclusive ``logs/.archive.lock``. Yields False when another run holds
    it (one younger than ``LOCK_STALE_S``)."""
    logs = archive / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    lock = logs / LOCK_NAME
    try:
        if lock.is_file() and time.time() - lock.stat().st_mtime > LOCK_STALE_S:
            lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started": _stamp()}, f)
        yield True
    finally:
        lock.unlink(missing_ok=True)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _copy_with_script(script: Path, game: Path, archive: Path, log: Callable[[str], None]) -> int:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        raise ConfigError(f"{script} needs PowerShell (pwsh), which is not on PATH")
    cmd = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(script), "-Game", str(game), "-Archive", str(archive)]
    log("running " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_no_window())
    for line in (proc.stdout or "").splitlines():
        if line.strip():
            log(f"  {line.rstrip()}")
    if proc.stderr and proc.stderr.strip():
        log("  stderr: " + proc.stderr.strip())
    return proc.returncode


def _copy_builtin(p: ArchivePlan, log: Callable[[str], None]) -> int:
    """Copy every delta: to ``<name>.archive-tmp``, then rename into place.
    Never deletes anything but its own temp files."""
    copied = 0
    for d in p.deltas:
        src = p.game_dir / d.rel
        dst = p.archive_dir / d.rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".archive-tmp")
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            copied += 1
        except FileNotFoundError:
            log(f"  gone from the install before it was copied: {d.rel}")
        finally:
            tmp.unlink(missing_ok=True)
    log(f"copied {copied} file(s)")
    return 0


@dataclass
class RunResult:
    status: Literal["up_to_date", "copied", "dry_run", "unsettled", "busy", "incomplete"]
    before: ArchivePlan
    after: ArchivePlan | None = None
    copier: str | None = None
    copier_exit: int | None = None
    elapsed_s: float = 0.0
    log_path: Path | None = None
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("up_to_date", "copied", "dry_run", "unsettled", "busy")

    def as_dict(self, limit: int | None = 20) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "copier": self.copier,
            "copier_exit": self.copier_exit,
            "elapsed_s": round(self.elapsed_s, 1),
            "log": str(self.log_path) if self.log_path else None,
            "before": self.before.as_dict(limit),
            "after": self.after.as_dict(limit) if self.after else None,
            "messages": self.messages,
        }


def run(
    config: PipelineConfig | None = None,
    *,
    dry_run: bool = False,
    settle_s: float = DEFAULT_SETTLE_S,
    copier: Literal["auto", "script", "builtin"] = "auto",
    log: Callable[[str], None] | None = None,
) -> RunResult:
    """Copy whatever the install has that the archive lacks.

    ``dry_run`` only plans. A run that finds the install still changing
    (``settle_s``) or another run in progress copies nothing and says so."""
    say = log or (lambda _msg: None)
    t0 = time.perf_counter()
    before = plan(config)
    res = RunResult(status="up_to_date", before=before)

    def note(msg: str) -> None:
        res.messages.append(msg)
        say(msg)

    if before.up_to_date:
        note(f"archive up to date: install builds {before.install_builds} and "
             f"{before.packages} packages are all in {before.archive_dir}")
        res.elapsed_s = time.perf_counter() - t0
        return res
    todo = [b.build for b in before.builds if not b.archived]
    note(f"to copy: builds {todo or '[]'}, {len(before.package_deltas)} package(s), "
         f"{before.bytes_to_copy / 1e9:.2f} GB")
    if dry_run:
        res.status = "dry_run"
        res.elapsed_s = time.perf_counter() - t0
        return res
    if not before.settled(settle_s):
        age = before.newest_change_age_s or 0.0
        note(f"the install changed {age / 60:.1f} min ago (an update may still be "
             f"running); waiting until it has been quiet for {settle_s / 60:.0f} min")
        res.status = "unsettled"
        res.elapsed_s = time.perf_counter() - t0
        return res

    archive = before.archive_dir
    with _run_lock(archive) as got:
        if not got:
            note(f"another archive run holds {archive / 'logs' / LOCK_NAME}")
            res.status = "busy"
            res.elapsed_s = time.perf_counter() - t0
            return res
        script = archive / SNAPSHOT_SCRIPT
        use_script = copier == "script" or (copier == "auto" and script.is_file()
                                            and sys.platform == "win32")
        if use_script:
            if not script.is_file():
                raise ConfigError(f"no {SNAPSHOT_SCRIPT} in the archive {archive}")
            res.copier = str(script)
            res.copier_exit = _copy_with_script(script, before.game_dir, archive, note)
        else:
            res.copier = "builtin"
            res.copier_exit = _copy_builtin(before, note)
        res.after = plan(config)
        res.status = "copied" if res.after.up_to_date else "incomplete"
        if not res.after.up_to_date:
            left = list(res.after.deltas)
            note(f"still missing after the copy: {len(left)} file(s), e.g. "
                 + ", ".join(d.rel for d in left[:5]))
        res.elapsed_s = time.perf_counter() - t0
        res.log_path = archive / "logs" / f"archive_{datetime.now():%Y%m%d_%H%M%S}.json"
        res.log_path.write_text(json.dumps(res.as_dict(limit=200), indent=1) + "\n",
                                encoding="utf-8")
    return res


# ---------------------------------------------------------------------------
# Scheduled task (Windows)
# ---------------------------------------------------------------------------


def _python_for_task() -> str:
    """pythonw.exe beside the running interpreter (no console window), else
    the interpreter itself."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.is_file() else exe)


def schedule_command(config: PipelineConfig) -> tuple[str, str]:
    """(program, arguments) the scheduled task runs."""
    game, archive = _roots(config)
    log = archive / "logs" / "scheduled.log"
    args = (f'-m wows_model_export.cli.builds archive --game-dir "{game}" '
            f'--archive-dir "{archive}" --log "{log}"')
    return _python_for_task(), args


def _ps(script: str) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        raise ConfigError("registering a scheduled task needs PowerShell on PATH")
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, creationflags=_no_window(),
    )


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def install_schedule(config: PipelineConfig, *, daily_at: str = "05:00") -> dict[str, Any]:
    """Register (or replace) the per-user scheduled task that runs the
    archive at logon (after 15 min) and daily at ``daily_at``. It runs
    below normal priority, without a window, and skips a start while a
    previous one still runs."""
    if sys.platform != "win32":
        raise ConfigError("scheduled archiving is only built for Windows; run "
                          "`wows-builds archive` from cron or a systemd timer instead")
    program, arguments = schedule_command(config)
    workdir = str(config.require_archive_dir())
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(program)} "
        f"-Argument {_ps_quote(arguments)} -WorkingDirectory {_ps_quote(workdir)}",
        "$t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME",
        "$t1.Delay = 'PT15M'",
        f"$t2 = New-ScheduledTaskTrigger -Daily -At {_ps_quote(daily_at)}",
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -Priority 7 "
        "-ExecutionTimeLimit (New-TimeSpan -Hours 4)",
        "$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited",
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $a -Trigger $t1,$t2 "
        "-Settings $s -Principal $p -Force "
        "-Description 'Copies a new World of Warships build into the append-only build archive "
        "(wows-builds archive). Reads the install only; never deletes.' | Out-Null",
        f"Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} | "
        "Select-Object TaskName,@{n='State';e={[string]$_.State}} | ConvertTo-Json",
    ])
    proc = _ps(script)
    if proc.returncode != 0:
        raise ConfigError(f"Register-ScheduledTask failed: {(proc.stderr or proc.stdout).strip()}")
    return {"task": TASK_NAME, "program": program, "arguments": arguments,
            "daily_at": daily_at, "state": json.loads(proc.stdout or "{}").get("State")}


def schedule_status() -> dict[str, Any] | None:
    """The scheduled task's state, last run and next run, or ``None`` when it
    is not registered (or not on Windows)."""
    if sys.platform != "win32":
        return None
    script = "\n".join([
        f"$t = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue",
        "if ($null -eq $t) { 'null'; exit 0 }",
        "$i = $t | Get-ScheduledTaskInfo",
        "[pscustomobject]@{ state = [string]$t.State; "
        "last_run = if ($i.LastRunTime -and $i.LastRunTime.Year -ge 2000) "
        "{ $i.LastRunTime.ToString('s') } else { $null }; "
        "last_result = if ($i.LastRunTime -and $i.LastRunTime.Year -ge 2000) "
        "{ $i.LastTaskResult } else { $null }; "
        "next_run = if ($i.NextRunTime) { $i.NextRunTime.ToString('s') } else { $null }; "
        "command = ($t.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }) -join '; ' } "
        "| ConvertTo-Json",
    ])
    try:
        proc = _ps(script)
    except ConfigError:
        return None
    if proc.returncode != 0:
        return None
    doc = json.loads(proc.stdout or "null")
    if doc is None:
        return None
    return {"task": TASK_NAME, **doc}


def remove_schedule() -> bool:
    """Unregister the scheduled task. Returns False when it was not there."""
    if schedule_status() is None:
        return False
    proc = _ps(f"Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Confirm:$false")
    if proc.returncode != 0:
        raise ConfigError(f"Unregister-ScheduledTask failed: {(proc.stderr or proc.stdout).strip()}")
    return True


__all__ = [
    "ArchivePlan",
    "BuildPlan",
    "FileDelta",
    "RunResult",
    "TASK_NAME",
    "install_schedule",
    "plan",
    "remove_schedule",
    "run",
    "schedule_command",
    "schedule_status",
]
