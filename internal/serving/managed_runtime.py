"""Conservative managed inference-server ownership for product entrypoints.

The one owner of "reuse, replace or start" for a profile's local server. Reuse
requires the recorded launch to equal the requested one (``serve.launch_differences``),
so a changed profile is never served by a stale process. The outcome says which
lifecycle transition happened; launch-to-ready is reported only for a launch this
call made and is never inferred for a reused server.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from local_agent.config import ModelConfig
from serving import serve


class RuntimeLifecycle(StrEnum):
    REUSED = "reused"
    STARTED = "started"
    REPLACED = "replaced"
    REFUSED = "refused"


class ReplacedReason(StrEnum):
    """Why an owned server recorded for this profile was not reused."""

    DEAD = "dead"
    UNHEALTHY = "unhealthy"
    CONFIG_CHANGED = "config_changed"


class RefusedReason(StrEnum):
    """Why no owned server is available; callers branch on this, never on message text."""

    FOREIGN_ENDPOINT = "foreign_endpoint"
    START_FAILED = "start_failed"
    NOT_HEALTHY = "not_healthy"


class RuntimeStep(StrEnum):
    """Progress a caller may render before a slow step; never a result."""

    STOPPING = "stopping"
    STARTING = "starting"


@dataclass(frozen=True)
class RuntimeEnsureResult:
    lifecycle: RuntimeLifecycle
    message: str
    replaced_reason: ReplacedReason | None = None
    refused_reason: RefusedReason | None = None
    launch_to_ready_ms: int | None = None
    # Launch fields that differed when the reason is CONFIG_CHANGED.
    changed_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        launched = self.lifecycle in (RuntimeLifecycle.STARTED, RuntimeLifecycle.REPLACED)
        if (self.lifecycle is RuntimeLifecycle.REPLACED) != (self.replaced_reason is not None):
            raise ValueError("a replaced_reason is required exactly when the server was replaced")
        if (self.lifecycle is RuntimeLifecycle.REFUSED) != (self.refused_reason is not None):
            raise ValueError("a refused_reason is required exactly when the server was refused")
        if self.launch_to_ready_ms is not None and not launched:
            raise ValueError("launch_to_ready_ms belongs only to a launch made by this call")
        if self.changed_fields and self.replaced_reason is not ReplacedReason.CONFIG_CHANGED:
            raise ValueError("changed_fields belong only to a configuration change")

    @property
    def ok(self) -> bool:
        return self.lifecycle is not RuntimeLifecycle.REFUSED


def endpoint_reachable(config: ModelConfig) -> bool:
    root = config.base_url.rstrip("/").rsplit("/", 1)[0]
    for url in (f"{config.base_url.rstrip('/')}/models", f"{root}/v1/models"):
        try:
            with urlopen(url, timeout=3):  # noqa: S310 - configured inference endpoint
                return True
        except (URLError, OSError, ValueError):
            continue
    return False


def _refused(reason: RefusedReason, message: str) -> RuntimeEnsureResult:
    return RuntimeEnsureResult(RuntimeLifecycle.REFUSED, message, refused_reason=reason)


def ensure_managed_runtime(
    profile: str,
    config: ModelConfig,
    runtime_root: Path,
    *,
    progress: Callable[[RuntimeStep], None] | None = None,
) -> RuntimeEnsureResult:
    """Ensure the configured server without claiming ownership of foreign processes."""
    executable = os.environ.get("LCA_OVMS_EXECUTABLE") if config.runtime == "ovms" else None
    plan = serve.make_plan(profile, config, runtime_root, executable=executable)
    record = serve.read_record(plan)
    replaced_reason: ReplacedReason | None = None
    changed: tuple[str, ...] = ()
    if record:
        state = serve.status(plan)
        changed = serve.launch_differences(record.get("plan") or {}, plan)
        if state.get("healthy") and not changed:
            return RuntimeEnsureResult(RuntimeLifecycle.REUSED,
                                       "owned model endpoint already ready")
        if state.get("process_alive"):
            replaced_reason = ReplacedReason.CONFIG_CHANGED if changed else ReplacedReason.UNHEALTHY
            if progress:
                progress(RuntimeStep.STOPPING)
            serve.stop(plan)
        else:
            replaced_reason = ReplacedReason.DEAD
        if replaced_reason is not ReplacedReason.CONFIG_CHANGED:
            changed = ()

    if endpoint_reachable(config):
        return _refused(
            RefusedReason.FOREIGN_ENDPOINT,
            "configured model endpoint is reachable but is not owned by Local Code Agent",
        )

    if progress:
        progress(RuntimeStep.STARTING)
    try:
        state = serve.start(plan, config, wait_seconds=900)
    except (serve.Refusal, OSError) as exc:
        return _refused(RefusedReason.START_FAILED,
                        f"managed model endpoint could not be started: {exc}")
    if not state.get("healthy"):
        return _refused(RefusedReason.NOT_HEALTHY, "managed model endpoint did not become healthy")
    elapsed = state.get("launch_to_ready_ms")
    return RuntimeEnsureResult(
        RuntimeLifecycle.REPLACED if replaced_reason else RuntimeLifecycle.STARTED,
        "managed model endpoint ready",
        replaced_reason=replaced_reason,
        launch_to_ready_ms=elapsed if isinstance(elapsed, int) else None,
        changed_fields=changed,
    )


__all__ = [
    "RefusedReason",
    "ReplacedReason",
    "RuntimeEnsureResult",
    "RuntimeLifecycle",
    "RuntimeStep",
    "endpoint_reachable",
    "ensure_managed_runtime",
]
