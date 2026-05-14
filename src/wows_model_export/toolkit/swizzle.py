"""`wowsunpack swizzle-dir` subcommand wrapper.

Walks a directory of WG-pack DDS files and emits glTF-conformant
siblings:

- ``<stem>_n.dd?``  → ``<stem>_normal.dd?`` + ``<stem>_nbmask.dd?``
- ``<stem>_mg.dd?`` → ``<stem>_mr.dd?``

Idempotent: existing siblings are skipped.

Use case: player-authored content-SDK skin packs ship raw WG-pack
``_n`` / ``_mg`` layouts that don't go through the toolkit's
VFS-extract pipeline (and miss the implicit Phase B swizzle that
``--raw-dds-dir`` runs on ``export-ship`` / ``export-model``). Running
this on the mod folder before classifying DDS files lets the rest of
the pipeline consume conformant siblings via the same
``_SUFFIX_PRIORITY`` ordering as VFS extracts.

The returned `ToolkitResult.data` carries the parsed counts:
    `{"processed": N, "siblings_written": M}`
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from ..config import PipelineConfig
from ..types import ToolkitResult
from ._subprocess import run_toolkit

_SUMMARY_RE = re.compile(
    r"processed (\d+) WG-pack source file\(s\), wrote (\d+) conformant sibling"
)


def swizzle_dir(
    input_dir: Path | str | os.PathLike,
    *,
    out_dir: Path | str | os.PathLike | None = None,
    recursive: bool = True,
    config: PipelineConfig | None = None,
) -> ToolkitResult:
    """Emit glTF-conformant DDS siblings for every WG-pack file under
    ``input_dir``.

    Does NOT need ``--game-dir`` (pure on-disk transformation), so the
    invocation skips it to avoid wowsunpack's VFS preamble cost.

    Returns a `ToolkitResult` with `data` populated as
    ``{"processed": N, "siblings_written": M}``. If the toolkit's
    summary wording shifts and the regex misses, falls back to walking
    the target dir for source/sibling counts; in that case the
    returned counts conflate "just wrote" with "already present" but
    at least surface a non-zero status for sanity checks.
    """
    inp = Path(input_dir).resolve()
    if not inp.is_dir():
        raise NotADirectoryError(f"swizzle_dir: input is not a directory: {inp}")

    cfg = config or PipelineConfig.load()

    argv = ["swizzle-dir", "--input", str(inp)]
    if out_dir is not None:
        out_p = Path(out_dir).resolve()
        out_p.mkdir(parents=True, exist_ok=True)
        argv += ["--out-dir", str(out_p)]
    if recursive:
        argv.append("--recursive")

    # No `expect_outputs` — swizzle writes a variable number of sibling
    # files; we trust exit code 0 + summary parse.
    result = run_toolkit(argv, config=cfg)

    # Parse the toolkit's summary line. wowsunpack prints it to stdout,
    # but scan stderr too in case a future toolkit build routes it
    # there.
    m = _SUMMARY_RE.search(result.stdout) or _SUMMARY_RE.search(result.stderr)
    if m:
        processed, siblings_written = int(m.group(1)), int(m.group(2))
        return ToolkitResult(
            output_paths=result.output_paths,
            stderr=result.stderr,
            elapsed_ms=result.elapsed_ms,
            data={"processed": processed, "siblings_written": siblings_written},
            stdout=result.stdout,
        )

    # Regex miss — fall back to counting files on disk.
    target = Path(out_dir) if out_dir is not None else inp
    walk = target.rglob("*") if recursive else target.iterdir()
    sources = 0
    siblings = 0
    for p in walk:
        if not p.is_file():
            continue
        s = p.suffix.lower()
        if s not in (".dd0", ".dd1", ".dd2", ".dds"):
            continue
        stem = p.stem
        if stem.endswith("_n") or stem.endswith("_mg"):
            sources += 1
        elif (stem.endswith("_normal") or stem.endswith("_nbmask")
              or stem.endswith("_mr")):
            siblings += 1
    print(
        f"warn: swizzle-dir summary regex missed (toolkit wording "
        f"changed?); fell back to file-counting in {target}",
        file=sys.stderr,
    )
    return ToolkitResult(
        output_paths=result.output_paths,
        stderr=result.stderr,
        elapsed_ms=result.elapsed_ms,
        data={"processed": sources, "siblings_written": siblings, "fallback": True},
        stdout=result.stdout,
    )


__all__ = ["swizzle_dir"]
