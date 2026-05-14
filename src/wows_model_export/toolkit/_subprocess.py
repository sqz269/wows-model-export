"""Shared subprocess runner for `toolkit.*` modules.

Every wowsunpack call routes through `run_toolkit()`. It:

- Resolves the binary via `PipelineConfig.require_toolkit_bin()` (env
  var → `shutil.which` → error).
- Captures stdout + stderr.
- Measures wall time.
- Raises `ToolkitError` (with structured `cmd` / `exit_code` / `stderr`)
  on non-zero exit or timeout.
- Validates expected output paths exist after a successful exit (catches
  the surprising case where the toolkit returns 0 but doesn't write what
  the caller asked for).

The returned `ToolkitResult` carries the resolved output paths + the
captured stderr + the elapsed time. Callers don't need to invoke
`subprocess` directly.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..errors import ToolkitError
from ..types import ToolkitResult

# Default per-call timeout. Picked to comfortably cover the slowest
# observed call (export-ship with full PBR textures on the heaviest hull,
# ~60s on release / ~3-4 min on debug) while still bounding wedged
# subprocesses. Tunable per call or via `PipelineConfig.toolkit_timeout_s`.
DEFAULT_TIMEOUT_S: float = 600.0


# Module-level sentinel so `timeout=None` can unambiguously mean "wait
# forever". A bare `None` default would conflict with that semantics.
_USE_CONFIG_DEFAULT: Any = object()


def run_toolkit(
    argv: list[str],
    *,
    config: PipelineConfig | None = None,
    expect_outputs: tuple[Path, ...] = (),
    timeout: float | None = _USE_CONFIG_DEFAULT,
    cwd: Path | None = None,
) -> ToolkitResult:
    """Invoke `wowsunpack <argv>` and return a typed result.

    Args:
        argv: arguments AFTER the binary path. Does not include the
            binary itself or `--game-dir` — callers compose their own
            (most subcommands need `--game-dir` before the subcommand
            name; swizzle-dir does not).
        config: resolved pipeline config; falls back to
            `PipelineConfig.load()` if `None`.
        expect_outputs: paths the caller asserts the toolkit will write.
            Validated after a clean exit; a missing file raises
            `ToolkitError` with a "reported success but X is missing"
            message.
        timeout: per-call timeout in seconds. Default = use
            `config.toolkit_timeout_s` or `DEFAULT_TIMEOUT_S`. Pass
            `None` to wait forever; pass a number for an explicit
            override.
        cwd: working directory for the subprocess. Rarely needed —
            wowsunpack is path-absolute via `--game-dir`.

    Returns:
        `ToolkitResult` with `output_paths` = `expect_outputs`,
        `stderr` = captured stderr (even on success — wowsunpack prints
        diagnostics there), `elapsed_ms` = wall time.

    Raises:
        ToolkitError: non-zero exit, timeout, missing expected outputs,
            or binary not found on disk.
    """
    cfg = config or PipelineConfig.load()
    bin_path = cfg.require_toolkit_bin()

    cmd: tuple[str, ...] = (str(bin_path), *argv)

    if not bin_path.is_file():
        raise ToolkitError(
            cmd=cmd,
            exit_code=-1,
            stderr="",
            message=(
                f"wowsunpack binary not found at {bin_path}. Set "
                f"WOWS_TOOLKIT_BIN, put wowsunpack on PATH, or build it "
                f"from the wows-toolkit fork "
                f"(cargo build --release --bin wowsunpack)."
            ),
        )

    if timeout is _USE_CONFIG_DEFAULT:
        timeout_resolved: float | None = cfg.toolkit_timeout_s or DEFAULT_TIMEOUT_S
    else:
        timeout_resolved = timeout

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_resolved,
        )
    except subprocess.TimeoutExpired as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        raise ToolkitError(
            cmd=cmd,
            exit_code=-1,
            stderr=(e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")),
            message=(
                f"wowsunpack timed out after {timeout_resolved}s "
                f"(elapsed={elapsed_ms:.0f}ms); pass timeout=None to wait forever."
            ),
        ) from e

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    if proc.returncode != 0:
        raise ToolkitError(
            cmd=cmd,
            exit_code=proc.returncode,
            stderr=proc.stderr or "",
        )

    # Verify expected outputs landed on disk. The toolkit occasionally
    # exits 0 even though a target file is missing (bad VFS path that
    # silently no-ops on a specific subcommand). The caller's contract
    # is that `expect_outputs` lists "files the toolkit promises to
    # write" — if any are missing, we treat it as a failure.
    for expected in expect_outputs:
        if not expected.is_file():
            raise ToolkitError(
                cmd=cmd,
                exit_code=0,
                stderr=proc.stderr or "",
                message=(
                    f"wowsunpack reported success but {expected} is missing"
                ),
            )

    return ToolkitResult(
        output_paths=tuple(expect_outputs),
        stderr=proc.stderr or "",
        elapsed_ms=elapsed_ms,
    )


__all__ = ["run_toolkit", "DEFAULT_TIMEOUT_S"]
