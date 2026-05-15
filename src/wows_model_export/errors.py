"""Exception hierarchy for the pipeline.

Consumers catch `PipelineError` for the broad "something the pipeline
raised" filter, or a more specific subclass when they want to distinguish
config problems from toolkit subprocess failures from per-step composer
errors.

Layer mapping:

    PipelineError           — base, never raised directly
      ConfigError           — bad / missing config (resolved at load time)
      ToolkitError          — layer 2: wowsunpack subprocess failed
      ResolveError          — layer 3: input shape didn't fit the transform
      StepError             — layer 4: a composer step failed; wraps the
                              original exception with the step name so the
                              consumer doesn't have to parse stack traces

`ToolkitError` and `StepError` carry structured fields beyond the
message, so consumers can render them however they want without
re-parsing the human-readable text.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class PipelineError(Exception):
    """Base class for everything the pipeline raises.

    Catch this when you want "any pipeline error" semantics. Catch a more
    specific subclass when you want to branch on the failure category.
    """


class ConfigError(PipelineError):
    """A required config value is missing or malformed.

    Raised by `PipelineConfig.load()` when the game directory, toolkit
    binary, or workspace can't be resolved from env vars + defaults. The
    message names the missing piece and (when possible) suggests the env
    var to set.
    """


@dataclass
class ToolkitError(PipelineError):
    """A wowsunpack subprocess invocation failed (non-zero exit).

    Carries the exact argv, exit code, and stderr so the consumer can
    surface them without re-running the call. Stringification renders a
    short summary; access the fields directly for the full payload.
    """

    cmd: tuple[str, ...]
    exit_code: int
    stderr: str
    message: str = ""

    def __post_init__(self) -> None:
        # `Exception.__init__` populates `.args`; we override with a
        # concise human summary that includes exit code + the argv head.
        head = " ".join(self.cmd[:3])
        if len(self.cmd) > 3:
            head += " …"
        summary = self.message or f"wowsunpack failed: {head} (exit {self.exit_code})"
        super().__init__(summary)


class ResolveError(PipelineError):
    """A `resolve.*` transform couldn't make sense of its input.

    Raised when structured input violates an invariant the transform
    depends on (e.g. a skel_ext candidates JSON with no record at the
    requested offset, or a variant Exterior whose `peculiarityModels`
    points at an asset that isn't in the library index).
    """


@dataclass
class StepError(PipelineError):
    """A composer step failed.

    Composers (`compose.*`) wrap the underlying exception with the
    canonical step name (matching the `StepEvent.step` field) so callers
    can identify *which* step died without parsing the traceback. The
    original exception is accessible via `.underlying`; `raise from` is
    used to preserve the chain.
    """

    step: str
    underlying: BaseException
    detail: str = ""
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        summary = f"step {self.step!r} failed"
        if self.detail:
            summary += f": {self.detail}"
        super().__init__(summary)


class CancelledError(StepError):
    """A composer was cancelled cooperatively at a step boundary.

    Subclasses :class:`StepError` so existing `except StepError` clauses
    continue to catch cancellation as "the step died" — the `.step`
    field still names the canonical step that was active when the
    cancel flag was observed (or the next step that hadn't yet started).

    Raised by :class:`wows_model_export.compose._step_runner.StepRunner`
    when its optional ``cancel: threading.Event`` is set. Carries
    ``state="cancelled"`` so the server-side job runner can flip the
    job's terminal state without parsing the message.

    Subclassing rather than declaring a fresh dataclass keeps the
    inherited ``(step, underlying, detail, data)`` constructor — callers
    pass ``underlying=KeyboardInterrupt()`` (or any sentinel) to keep
    the dataclass shape; consumers branch on ``isinstance(exc,
    CancelledError)`` rather than the underlying type.
    """

    # Discriminator field for code that branches on the terminal job
    # state without `isinstance` (e.g. JSON-serialised error payloads).
    state: str = "cancelled"


__all__ = [
    "PipelineError",
    "ConfigError",
    "ToolkitError",
    "ResolveError",
    "StepError",
    "CancelledError",
]
