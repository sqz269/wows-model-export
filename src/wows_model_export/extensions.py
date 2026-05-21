"""Extension-point protocol for downstream consumer plugins.

A consumer is any Python package that registers a descriptor factory
under the ``wows_model_export.consumers`` entry-point group. The
producer discovers descriptors at startup via :mod:`importlib.metadata`
and exposes them through ``/api/consumers`` — it never imports a
consumer by name.

Consumer ``pyproject.toml`` registration::

    [project.entry-points."wows_model_export.consumers"]
    <id> = "<package>.<module>:descriptor"

Descriptor factory signature: ``() -> ConsumerDescriptor``.

Discovery is cached for the process lifetime; restart the webview
server (``wows-webview-serve``) to pick up a newly-installed consumer.
A consumer that raises during ``load()`` is logged and skipped — one
broken plugin never breaks discovery for the rest.
"""
from __future__ import annotations

import importlib.metadata as md
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "wows_model_export.consumers"

# v1 widget kinds. Extend as new consumer needs surface.
#   bool          -> checkbox
#   ships_picker  -> multi-select populated from /api/ships
#   string        -> single-line text input
ParamKind = Literal["bool", "ships_picker", "string"]

# Handler signature matches the ``compose.*`` shape used by the in-
# process job runner: keyword args + ``on_event`` + ``cancel``. The
# route handler injects ``config`` (PipelineConfig) and the job runner
# injects ``on_event`` (OnEvent) + ``cancel`` (threading.Event); the
# remaining kwargs come from the request body, filtered against the
# action's declared params.
ConsumerHandler = Callable[..., Any]


@dataclass(frozen=True)
class ConsumerParam:
    """One input field exposed by a consumer action.

    Attributes:
        id            kwarg name passed to the handler
        label         display label shown next to the widget
        kind          widget kind (see :data:`ParamKind`)
        default       default value used when the request body omits ``id``
        description   optional helper text rendered under the widget
    """

    id: str
    label: str
    kind: ParamKind
    default: Any = None
    description: str = ""


@dataclass(frozen=True)
class ConsumerAction:
    """One callable action exposed by a consumer.

    A consumer with one action is the common shape; multiple actions
    let a consumer surface distinct buttons (e.g. "Publish" vs.
    "Refresh PNGs only" for the Blender consumer).
    """

    id: str
    label: str
    description: str = ""
    params: tuple[ConsumerParam, ...] = ()
    # Excluded from ``repr`` so log lines and HTTPException bodies don't
    # leak the qualified handler path of a consumer.
    handler: ConsumerHandler | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ConsumerDescriptor:
    """Top-level consumer registration.

    Attributes:
        id              stable slug (matches the entry-point name by
                        convention; used in the API path)
        display_name    human-readable label rendered in the webview
        description     optional one-line description rendered under
                        the consumer card
        actions         one or more callable actions
    """

    id: str
    display_name: str
    description: str = ""
    actions: tuple[ConsumerAction, ...] = ()


_cache: list[ConsumerDescriptor] | None = None


def discover() -> list[ConsumerDescriptor]:
    """Return all registered consumer descriptors.

    Cached for the process lifetime. The webview backend is a long-
    running uvicorn process so we never need to invalidate at runtime;
    restart the server to pick up new installs.
    """
    global _cache
    if _cache is not None:
        return _cache

    out: list[ConsumerDescriptor] = []
    for ep in md.entry_points(group=ENTRY_POINT_GROUP):
        try:
            factory = ep.load()
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "consumer entry-point %r: import failed (%s: %s)",
                ep.name, type(exc).__name__, exc,
            )
            continue
        try:
            d = factory()
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "consumer entry-point %r: factory raised (%s: %s)",
                ep.name, type(exc).__name__, exc,
            )
            continue
        if not isinstance(d, ConsumerDescriptor):
            logger.warning(
                "consumer entry-point %r: factory returned %r, "
                "expected ConsumerDescriptor",
                ep.name, type(d).__name__,
            )
            continue
        out.append(d)

    _cache = out
    return out


def _reset_cache_for_tests() -> None:
    """Drop the discovery cache. Test-only hook."""
    global _cache
    _cache = None


__all__ = [
    "ENTRY_POINT_GROUP",
    "ConsumerAction",
    "ConsumerDescriptor",
    "ConsumerHandler",
    "ConsumerParam",
    "ParamKind",
    "discover",
]
