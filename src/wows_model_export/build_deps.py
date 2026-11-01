"""Record which game inputs an export reads.

An export (a ship ingest, a map export) opens a :func:`recording` block.
While it is active:

- every ``wowsunpack`` call made through
  :func:`wows_model_export.toolkit._subprocess.run_toolkit` gets
  ``--deps-out`` and its file / assets.bin record list is merged in;
- code that reads game data from a pipeline cache instead of the toolkit
  (``camouflages.xml``, GameParams, the cached ``assets.bin``) calls
  :func:`record_file` / :func:`record_gameparams` / :func:`record_record`.

The block's collector is then turned into the export's ``build_deps.json``
by :mod:`wows_model_export.compose.build_tracking`, which adds the
fingerprint of every input in the current game build. After a game update,
comparing those fingerprints against the new build shows exactly which
exports are out of date.

The active collector lives in a :class:`contextvars.ContextVar`. Thread
pools don't copy context by default, so code that fans work out to a pool
inside a recording block should submit through :func:`bind` (or use
``contextvars.copy_context().run``) for the workers' reads to count.
"""

from __future__ import annotations

import contextvars
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


def norm_path(path: str) -> str:
    """VFS path in manifest form: forward slashes, one leading slash."""
    p = path.replace("\\", "/")
    return "/" + p.lstrip("/")


@dataclass
class DepsCollector:
    """Inputs one export read. Thread-safe."""

    files: set[str] = field(default_factory=set)
    records: set[str] = field(default_factory=set)
    gameparams: set[str] = field(default_factory=set)
    toolkit_calls: int = 0
    toolkit_builds: set[int] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _tmp: Path | None = field(default=None, repr=False)

    def add_files(self, paths: Iterable[str]) -> None:
        with self._lock:
            self.files.update(norm_path(p) for p in paths)

    def add_records(self, paths: Iterable[str]) -> None:
        with self._lock:
            self.records.update(norm_path(p) for p in paths)

    def add_gameparams(self, ids: Iterable[str]) -> None:
        with self._lock:
            self.gameparams.update(i for i in ids if i)

    def new_deps_file(self) -> Path:
        """A fresh path for one toolkit call's ``--deps-out``."""
        with self._lock:
            if self._tmp is None:
                self._tmp = Path(tempfile.mkdtemp(prefix="wms-deps-"))
            fd, name = tempfile.mkstemp(dir=self._tmp, suffix=".json")
        os.close(fd)
        Path(name).unlink(missing_ok=True)
        return Path(name)

    def merge_toolkit_deps(self, path: Path) -> None:
        """Fold one ``wowsunpack.deps/v1`` file in, then delete it."""
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        finally:
            path.unlink(missing_ok=True)
        self.add_files(doc.get("files") or ())
        self.add_records(doc.get("records") or ())
        with self._lock:
            self.toolkit_calls += 1
            if isinstance(doc.get("build"), int):
                self.toolkit_builds.add(doc["build"])

    def close(self) -> None:
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "files": sorted(self.files),
                "records": sorted(self.records),
                "gameparams": sorted(self.gameparams),
                "toolkit_calls": self.toolkit_calls,
                "toolkit_builds": sorted(self.toolkit_builds),
            }


_current: contextvars.ContextVar[DepsCollector | None] = contextvars.ContextVar(
    "wms_build_deps", default=None
)


def active() -> DepsCollector | None:
    """The collector of the innermost :func:`recording` block, if any."""
    return _current.get()


@contextmanager
def recording(collector: DepsCollector | None = None) -> Iterator[DepsCollector]:
    """Collect every game input read inside the block.

    Nested blocks share the outer collector, so an export that calls
    another composer records into one set."""
    outer = _current.get()
    if outer is not None and collector is None:
        yield outer
        return
    coll = collector or DepsCollector()
    token = _current.set(coll)
    try:
        yield coll
    finally:
        _current.reset(token)
        if outer is None:
            coll.close()


def record_file(path: str) -> None:
    c = _current.get()
    if c is not None:
        c.add_files((path,))


def record_record(path: str) -> None:
    c = _current.get()
    if c is not None:
        c.add_records((path,))


def record_gameparams(ids: Iterable[str]) -> None:
    c = _current.get()
    if c is not None:
        c.add_gameparams(ids)


def bind(fn: Callable[..., T]) -> Callable[..., T]:
    """Wrap ``fn`` so it runs in a copy of the caller's context.

    Use when submitting to a thread pool from inside a recording block:
    ``pool.submit(build_deps.bind(work), arg)``. Each call gets its own
    copy, so one wrapped function can run on several workers at once."""
    ctx = contextvars.copy_context()

    def run(*args: Any, **kwargs: Any) -> T:
        return ctx.copy().run(fn, *args, **kwargs)

    return run
