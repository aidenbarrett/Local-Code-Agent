"""Managed local inference runtime startup shared by product entrypoints.

Ownership stays conservative: a healthy matching owned server is reused, an owned stale
server may be stopped, and an unowned process already listening on the configured endpoint
is never adopted or killed.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .config import ModelConfig
from measurement import serve


@dataclass(frozen=True)
class RuntimeEnsureResult:
    ok: bool
    message: str


def endpoint_reachable(config: ModelConfig) -> bool:
    root = config.base_url.rstrip("/").rsplit("/", 1)[0]
    for url in (f"{config.base_url.rstrip('/')}/models", f"{root}/v1/models"):
        try:
            with urlopen(url, timeout=3):  # noqa: S310 - configured inference endpoint
                return True
        except (URLError, OSError, ValueError):
            continue
    return False


def ensure_managed_runtime(
    profile: str,
    config: ModelConfig,
    runtime_root: Path,
) -> RuntimeEnsureResult:
    """Ensure the configured server using the same ownership/status rules as raw chat."""
    executable = os.environ.get("LCA_OVMS_EXECUTABLE") if config.runtime == "ovms" else None
    plan = serve.make_plan(profile, config, runtime_root, executable=executable)
    record = serve.read_record(plan)
    if record:
        state = serve.status(plan)
        recorded = (record.get("plan") or {}).get("model_configuration") or {}
        record_device = recorded.get("device")
        record_model = recorded.get("model")
        if state.get("healthy") and record_device == config.device and record_model == config.model:
            return RuntimeEnsureResult(True, "owned model endpoint already ready")
        if state.get("process_alive"):
            serve.stop(plan)

    if endpoint_reachable(config):
        return RuntimeEnsureResult(
            False,
            "configured model endpoint is reachable but is not owned by Local Code Agent",
        )

    try:
        state = serve.start(plan, config, wait_seconds=900)
    except (serve.Refusal, OSError) as exc:
        return RuntimeEnsureResult(False, f"managed model endpoint could not be started: {exc}")
    if not state.get("healthy"):
        return RuntimeEnsureResult(False, "managed model endpoint did not become healthy")
    return RuntimeEnsureResult(True, "managed model endpoint ready")


__all__ = ["RuntimeEnsureResult", "endpoint_reachable", "ensure_managed_runtime"]
