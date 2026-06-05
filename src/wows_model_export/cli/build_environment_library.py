"""CLI wrapper for :func:`wows_model_export.compose.environment.build`.

Build the IBL / tonemap environment library: extract the per-space PMREM
reflection cubes + the shared ``env_brdf_lut.dds`` into the
``content/environment/`` cache, and emit ``library/environment/manifest.json``
keyed by space + weather (GT tonemap params, spherical-harmonics diffuse,
cube format). Consumers (webview, Unity / Blender via the ``environment``
publish domain) join against the manifest.

Argv shape::

    wows-build-environment-library
        [--space NAME ...]   # restrict to specific spaces (repeatable)
        [--no-extract]       # parse params only; skip DDS extraction
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback

from ..compose.environment import build
from ..errors import ConfigError, ToolkitError
from ._args import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_UNEXPECTED,
    add_common_args,
    resolve_config,
)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-build-environment-library",
        description="Build the PMREM cube / BRDF-LUT / HDR-param environment "
                    "library with a manifest.",
    )
    ap.add_argument(
        "--space",
        action="append",
        default=None,
        metavar="NAME",
        help="Restrict to a space (e.g. 14_Atlantic or spaces/14_Atlantic). "
             "Repeatable; default builds every space.",
    )
    ap.add_argument(
        "--no-extract",
        action="store_true",
        help="Parse HDR/SH params only; skip extracting the cube + BRDF DDS.",
    )
    add_common_args(ap)
    return ap


def _summarize(result: dict) -> str:
    bits = [
        f"manifest {result['manifest_path']}",
        f"spaces={result['space_count']}",
        f"cubes={result['cubes_extracted']}",
    ]
    if result.get("cubes_missing"):
        bits.append(f"missing={result['cubes_missing']}")
    if result.get("env_brdf_lut_url"):
        bits.append("brdf_lut=ok")
    else:
        bits.append("brdf_lut=MISSING")
    warns = result.get("warnings") or []
    if warns:
        bits.append(f"warnings={len(warns)}")
    return "  ".join(bits)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        result = build(
            config=cfg,
            spaces=args.space,
            extract_assets=not args.no_extract,
        )
    except (ConfigError, ToolkitError) as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(f"\nunexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_UNEXPECTED

    print(_summarize(result), file=sys.stderr)
    for warn in (result.get("warnings") or [])[:20]:
        print(f"  warn: {warn}", file=sys.stderr)
    return EXIT_OK


__all__ = ["main"]
