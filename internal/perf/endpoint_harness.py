#!/usr/bin/env python3
"""Backend-agnostic performance harness for OpenAI-compatible chat endpoints."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
import secrets
import statistics
import subprocess
import time
import tomllib
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

import psutil


@dataclass(frozen=True)
class Profile:
    name: str
    base_url: str
    model: str
    runtime: str
    device: str
    api_key_env: str | None = None
    ready_url: str | None = None
    start_command: tuple[str, ...] | None = None
    stop_command: tuple[str, ...] | None = None
    telemetry_url: str | None = None
    compile_time_path: str | None = None
    prefill_time_path: str | None = None
    memory_path: str | None = None
    identity_url: str | None = None
    identity_model_path: str | None = None
    identity_runtime_path: str | None = None
    identity_device_path: str | None = None
    identity_instance_path: str | None = None
    active_requests_url: str | None = None
    active_request_ids_path: str | None = None
    request_id_header: str | None = None


def _deep_get(value: Any, path: str | None) -> Any:
    if not path:
        return None
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _command(value: Any, name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise ValueError(f"{name} must be a non-empty TOML string array")
    return tuple(value)


def load_profile(path: Path) -> Profile:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    endpoint = data.get("endpoint") or {}
    identity = data.get("identity") or {}
    startup = data.get("startup") or {}
    telemetry = data.get("telemetry") or {}
    cancellation = data.get("cancellation") or {}
    required = {
        "endpoint.base_url": endpoint.get("base_url"),
        "endpoint.model": endpoint.get("model"),
        "identity.runtime": identity.get("runtime"),
        "identity.device": identity.get("device"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("profile missing required values: " + ", ".join(missing))
    base_url = str(endpoint["base_url"]).rstrip("/") + "/"
    return Profile(
        name=str(data.get("name") or path.stem),
        base_url=base_url,
        model=str(endpoint["model"]),
        runtime=str(identity["runtime"]),
        device=str(identity["device"]),
        api_key_env=str(endpoint["api_key_env"]) if endpoint.get("api_key_env") else None,
        ready_url=str(startup.get("ready_url") or urljoin(base_url, "models")),
        start_command=_command(startup.get("command"), "startup.command"),
        stop_command=_command(startup.get("stop_command"), "startup.stop_command"),
        telemetry_url=str(telemetry["url"]) if telemetry.get("url") else None,
        compile_time_path=str(telemetry["compile_time_path"]) if telemetry.get("compile_time_path") else None,
        prefill_time_path=str(telemetry["prefill_time_path"]) if telemetry.get("prefill_time_path") else None,
        memory_path=str(telemetry["memory_path"]) if telemetry.get("memory_path") else None,
        identity_url=str(identity["url"]) if identity.get("url") else None,
        identity_model_path=str(identity["model_path"]) if identity.get("model_path") else None,
        identity_runtime_path=str(identity["runtime_path"]) if identity.get("runtime_path") else None,
        identity_device_path=str(identity["device_path"]) if identity.get("device_path") else None,
        identity_instance_path=(
            str(identity["instance_path"]) if identity.get("instance_path") else None
        ),
        active_requests_url=str(cancellation["active_requests_url"]) if cancellation.get("active_requests_url") else None,
        active_request_ids_path=str(cancellation["active_request_ids_path"]) if cancellation.get("active_request_ids_path") else None,
        request_id_header=str(cancellation["request_id_header"]) if cancellation.get("request_id_header") else None,
    )


class EndpointClient:
    def __init__(self, profile: Profile, timeout: float = 120.0):
        self.profile = profile
        self.timeout = timeout

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.profile.api_key_env:
            token = os.environ.get(self.profile.api_key_env)
            if not token:
                raise RuntimeError(f"missing API key environment variable: {self.profile.api_key_env}")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def get_json(self, url: str) -> Any:
        request = Request(url, headers=self.headers(), method="GET")
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - configured endpoint
            return json.loads(response.read().decode("utf-8"))

    def ready(self) -> bool:
        try:
            self.get_json(self.profile.ready_url or urljoin(self.profile.base_url, "models"))
            return True
        except Exception:
            return False

    def open_stream(self, prompt: str, max_tokens: int):
        target = urlsplit(urljoin(self.profile.base_url, "chat/completions"))
        cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        conn = cls(target.hostname, target.port, timeout=self.timeout)
        body = json.dumps({
            "model": self.profile.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        })
        path = target.path or "/"
        if target.query:
            path += "?" + target.query
        started = time.perf_counter()
        conn.request("POST", path, body=body, headers=self.headers())
        response = conn.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        if response.status >= 400:
            detail = response.read(4096).decode("utf-8", errors="replace")
            conn.close()
            raise RuntimeError(f"chat endpoint returned HTTP {response.status}: {detail}")
        return conn, response, started, headers

    @staticmethod
    def next_event(response) -> dict[str, Any] | None:
        while True:
            raw = response.readline()
            if not raw:
                return None
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return None
            try:
                value = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value

    @staticmethod
    def content(event: dict[str, Any]) -> str | None:
        choices = event.get("choices") or []
        if not choices:
            return None
        value = (choices[0].get("delta") or {}).get("content")
        return value if isinstance(value, str) and value else None

    def stream_chat(self, prompt: str, max_tokens: int) -> dict[str, Any]:
        conn, response, started, response_headers = self.open_stream(prompt, max_tokens)
        first = None
        last = None
        usage = None
        chunks = 0
        try:
            while True:
                event = self.next_event(response)
                if event is None:
                    break
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                if self.content(event):
                    now = time.perf_counter()
                    first = first or now
                    last = now
                    chunks += 1
        finally:
            response.close()
            conn.close()
        ended = time.perf_counter()
        completion_tokens = usage.get("completion_tokens") if usage else None
        prompt_tokens = usage.get("prompt_tokens") if usage else None
        decode_seconds = max(0.0, last - first) if first is not None and last is not None else None
        decode_tps = None
        if isinstance(completion_tokens, int) and completion_tokens > 1 and decode_seconds and decode_seconds > 0:
            decode_tps = (completion_tokens - 1) / decode_seconds
        return {
            "request_elapsed_ms": (ended - started) * 1000.0,
            "ttft_ms": (first - started) * 1000.0 if first is not None else None,
            "decode_seconds": decode_seconds,
            "decode_tokens_per_second": decode_tps,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "stream_content_chunks": chunks,
            "response_headers": response_headers,
            "timing_source": "client_boundary",
        }


def _backend_value(client: EndpointClient, profile: Profile, path: str | None) -> Any:
    if not profile.telemetry_url or not path:
        return None
    return _deep_get(client.get_json(profile.telemetry_url), path)


def _run_stop(profile: Profile) -> None:
    if profile.stop_command:
        subprocess.run(profile.stop_command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_not_ready(client: EndpointClient, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not client.ready():
            return True
        time.sleep(0.1)
    return not client.ready()


def start_for_cold_measurement(
    client: EndpointClient,
    profile: Profile,
    timeout: float,
) -> tuple[dict[str, Any], subprocess.Popen[Any] | None]:
    if not profile.start_command:
        return {"supported": False, "reason": "profile has no startup command"}, None

    if profile.stop_command:
        _run_stop(profile)
        if not _wait_not_ready(client):
            return {"supported": False, "reason": "endpoint remained ready after configured stop command"}, None
    elif client.ready():
        return {
            "supported": False,
            "reason": "endpoint is already ready and no stop command is configured; cold state cannot be established",
        }, None

    started = time.perf_counter()
    process = subprocess.Popen(profile.start_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = started + timeout
    last_error = None
    while time.perf_counter() < deadline:
        try:
            client.get_json(profile.ready_url or urljoin(profile.base_url, "models"))
            result: dict[str, Any] = {
                "supported": True,
                "client_observed_start_to_ready_ms": (time.perf_counter() - started) * 1000.0,
                "timing_source": "client_boundary",
            }
            compile_time = _backend_value(client, profile, profile.compile_time_path)
            if compile_time is not None:
                result["backend_reported_compile_time"] = compile_time
                result["compile_time_source"] = "backend_reported"
            return result, process
        except Exception as exc:
            last_error = str(exc)
            rc = process.poll()
            if rc not in (None, 0):
                raise RuntimeError(f"startup command exited {rc} before ready")
            time.sleep(0.2)
    raise TimeoutError(f"endpoint did not become ready within {timeout}s: {last_error}")


def stop_started_endpoint(profile: Profile, process: subprocess.Popen[Any] | None) -> None:
    if profile.stop_command:
        _run_stop(profile)
        return
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def benchmark_generation(client: EndpointClient, prompt: str, max_tokens: int) -> dict[str, Any]:
    return client.stream_chat(prompt, max_tokens)


def benchmark_context_ladder(client: Any, profile: Profile, targets: list[int]) -> list[dict[str, Any]]:
    rows = []
    for target in targets:
        prompt = "x " * target
        try:
            result = client.stream_chat(prompt, max_tokens=1)
        except Exception as exc:
            rows.append({
                "requested_approx_tokens": target,
                "supported": False,
                "error": str(exc),
                "timing_source": "client_boundary",
            })
            break
        row = {
            "requested_approx_tokens": target,
            "observed_prompt_tokens": result.get("prompt_tokens"),
            "client_observed_prompt_to_first_token_ms": result.get("ttft_ms"),
            "supported": result.get("ttft_ms") is not None,
            "timing_source": "client_boundary",
        }
        backend_prefill = _backend_value(client, profile, profile.prefill_time_path)
        if backend_prefill is not None:
            row["backend_reported_prefill_time"] = backend_prefill
            row["prefill_time_source"] = "backend_reported"
        rows.append(row)
    return rows


def _active_ids(client: EndpointClient, profile: Profile) -> set[str]:
    payload = client.get_json(profile.active_requests_url or "")
    value = _deep_get(payload, profile.active_request_ids_path)
    if not isinstance(value, list):
        raise RuntimeError("active request probe did not return a list")
    return {str(item) for item in value}


def benchmark_cancellation(
    client: Any,
    profile: Profile,
    poll_interval: float = 0.01,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if not (profile.active_requests_url and profile.active_request_ids_path and profile.request_id_header):
        return {"supported": False, "reason": "endpoint-side cancellation observation hook is not configured"}
    conn, response, _started, headers = client.open_stream(
        "Continue producing text until the request is cancelled. " * 128,
        max_tokens=4096,
    )
    request_id = headers.get(profile.request_id_header.lower())
    if not request_id:
        response.close()
        conn.close()
        return {"supported": False, "reason": "response did not expose configured request id header"}
    try:
        while True:
            event = client.next_event(response)
            if event is None:
                return {"supported": False, "reason": "stream ended before cancellation test", "request_id": request_id}
            if client.content(event):
                break
        if request_id not in _active_ids(client, profile):
            return {"supported": False, "reason": "endpoint probe could not prove request active before disconnect", "request_id": request_id}
        dropped = time.perf_counter()
        response.close()
        conn.close()
        deadline = dropped + timeout
        while time.perf_counter() < deadline:
            if request_id not in _active_ids(client, profile):
                return {
                    "supported": True,
                    "request_id": request_id,
                    "disconnect_to_endpoint_stop_ms": (time.perf_counter() - dropped) * 1000.0,
                    "timing_source": "endpoint_observation",
                }
            time.sleep(poll_interval)
        return {"supported": False, "reason": "endpoint stop not observed before timeout", "request_id": request_id}
    finally:
        try:
            response.close()
        finally:
            conn.close()


def run_soak(client: EndpointClient, profile: Profile, hours: float, prompt: str, max_tokens: int) -> dict[str, Any]:
    if hours <= 0:
        return {"supported": False, "reason": "soak duration is zero"}
    deadline = time.monotonic() + hours * 3600.0
    samples = []
    errors = []
    process = psutil.Process()
    while time.monotonic() < deadline:
        timestamp = time.time()
        try:
            measurement = benchmark_generation(client, prompt, max_tokens)
            sample = {
                "timestamp_unix": timestamp,
                "ttft_ms": measurement["ttft_ms"],
                "decode_tokens_per_second": measurement["decode_tokens_per_second"],
                "request_elapsed_ms": measurement["request_elapsed_ms"],
                "client_rss_bytes": process.memory_info().rss,
            }
            backend_memory = _backend_value(client, profile, profile.memory_path)
            if backend_memory is not None:
                sample["backend_reported_memory"] = backend_memory
                sample["memory_source"] = "backend_reported"
            samples.append(sample)
        except Exception as exc:
            errors.append({"timestamp_unix": timestamp, "type": type(exc).__name__, "message": str(exc)})
    ttfts = [float(row["ttft_ms"]) for row in samples if row.get("ttft_ms") is not None]
    drift = None
    if len(ttfts) >= 4:
        quarter = max(1, len(ttfts) // 4)
        drift = statistics.median(ttfts[-quarter:]) - statistics.median(ttfts[:quarter])
    return {
        "supported": True,
        "requested_hours": hours,
        "iterations": len(samples) + len(errors),
        "successful_iterations": len(samples),
        "error_count": len(errors),
        "errors": errors,
        "ttft_median_ms": statistics.median(ttfts) if ttfts else None,
        "ttft_end_minus_start_median_ms": drift,
        "samples": samples,
    }


FIRST_AFTER_COLD = "first_after_established_cold_start"
FIRST_OBSERVED = "first_observed_request"
SAME_ENDPOINT_WARM = "same_endpoint_warm"
CONTINUITY_UNPROVEN = "subsequent_request_endpoint_reuse_unproven"
NEW_SEQUENCE = "lifecycle_boundary_observed_new_sequence"


# Failures of an optional observation probe. A missing API key (RuntimeError) is a
# configuration error and must still propagate.
PROBE_ERRORS: tuple[type[BaseException], ...] = (OSError, ValueError, http.client.HTTPException)


@dataclass(frozen=True)
class EndpointSnapshot:
    """Observable facts that must hold unchanged for a request to count as warm."""

    ready: bool
    model_ids: tuple[str, ...] | None
    instance: str | None
    instance_source: str | None


def _model_ids(client: Any, profile: Profile) -> tuple[str, ...] | None:
    try:
        payload = client.get_json(urljoin(profile.base_url, "models"))
    except PROBE_ERRORS:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    return tuple(sorted(str(item.get("id")) for item in data if isinstance(item, dict)))


def _instance(client: Any, profile: Profile) -> str | None:
    if not (profile.identity_url and profile.identity_instance_path):
        return None
    try:
        payload = client.get_json(profile.identity_url)
    except PROBE_ERRORS:
        return None
    value = _deep_get(payload, profile.identity_instance_path)
    return None if value is None else str(value)


def snapshot_endpoint(client: Any, profile: Profile) -> EndpointSnapshot:
    ready = bool(client.ready())
    model_ids = _model_ids(client, profile)
    instance = _instance(client, profile)
    return EndpointSnapshot(
        ready=ready,
        model_ids=model_ids,
        instance=instance,
        instance_source="endpoint_observed" if instance is not None else None,
    )


def _owned_process_state(process: subprocess.Popen[Any] | None) -> str:
    if process is None:
        return "not_owned_by_harness"
    rc = process.poll()
    if rc is None:
        return "alive"
    # A launcher that exits 0 after handing off to a daemon proves nothing about the server.
    return "launcher_exited" if rc == 0 else "exited_with_error"


def _compare_snapshots(
    phase: str,
    before: EndpointSnapshot,
    after: EndpointSnapshot,
    profile: Profile,
) -> tuple[list[str], list[str]]:
    """Return (observed breaks, unobservable facts) for one pair of snapshots."""
    boundary: list[str] = []
    missing: list[str] = []
    if not after.ready:
        boundary.append(f"{phase}: endpoint not ready")
    if before.model_ids is None or after.model_ids is None:
        missing.append(f"{phase}: model list not observable")
    elif before.model_ids != after.model_ids or profile.model not in after.model_ids:
        boundary.append(f"{phase}: served model set changed")
    if before.instance is not None and after.instance is not None:
        if before.instance != after.instance:
            boundary.append(f"{phase}: endpoint instance identity changed")
    elif before.instance is not None or after.instance is not None:
        missing.append(f"{phase}: endpoint instance identity not observable")
    return boundary, missing


def judge_continuity(
    snapshots: tuple[EndpointSnapshot, ...],
    profile: Profile,
    process: subprocess.Popen[Any] | None,
) -> dict[str, Any]:
    """Decide whether consecutive requests were served by one still-running endpoint.

    ``snapshots`` are taken before the first request, between requests and after the last.
    An observed break is a lifecycle boundary; an unobservable fact is missing evidence.
    Neither may be reported as warm, and missing evidence is never relabelled as a boundary.
    """
    phases = ("between requests", "after warm request")
    boundary: list[str] = []
    missing: list[str] = []
    for phase, (before, after) in zip(phases, pairwise(snapshots), strict=True):
        pair_boundary, pair_missing = _compare_snapshots(phase, before, after, profile)
        boundary.extend(pair_boundary)
        missing.extend(pair_missing)

    process_state = _owned_process_state(process)
    if process_state == "exited_with_error":
        boundary.append("harness-owned endpoint process exited")
    instances = {snap.instance for snap in snapshots}
    if process_state == "alive":
        reuse_source: str | None = "harness_owned_process_alive"
    elif None not in instances and len(instances) == 1:
        reuse_source = "endpoint_reported_instance_unchanged"
    else:
        reuse_source = None

    if boundary:
        classification, reused = NEW_SEQUENCE, False
    elif reuse_source and not missing:
        classification, reused = SAME_ENDPOINT_WARM, True
    else:
        classification, reused = CONTINUITY_UNPROVEN, None
    return {
        "classification": classification,
        "endpoint_process_reused": reused,
        "reuse_evidence": reuse_source if reused else None,
        "boundary_observations": boundary,
        "missing_evidence": missing,
        "harness_process_state": process_state,
    }


def _backend_memory(client: Any, profile: Profile) -> dict[str, Any]:
    if not (profile.telemetry_url and profile.memory_path):
        return {"supported": False, "reason": "no backend memory telemetry hook configured"}
    try:
        value = _backend_value(client, profile, profile.memory_path)
    except PROBE_ERRORS as exc:
        return {"supported": False, "reason": f"backend memory probe failed: {exc}"}
    if value is None:
        return {"supported": False, "reason": "backend memory hook returned no value"}
    return {
        "supported": True,
        "backend_reported_memory": value,
        "memory_source": "backend_reported",
    }


def _sequence_prompt(base: str, nonce: str, index: int) -> str:
    # Differs at the first character so prompt/prefix caching cannot pose as residency.
    return f"{index}/{nonce} perf: {base}"


@dataclass(frozen=True)
class LifecycleRun:
    """One measured endpoint lifetime: what to send and what the harness established."""

    prompt: str
    max_tokens: int
    nonce: str
    established_cold: bool
    process: subprocess.Popen[Any] | None = None


def measure_lifecycle_sequence(
    client: Any,
    profile: Profile,
    run: LifecycleRun,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return (lifecycle, first generation, warm generation) for one endpoint lifetime."""
    prompt, max_tokens, nonce = run.prompt, run.max_tokens, run.nonce
    established_cold, process = run.established_cold, run.process
    before = snapshot_endpoint(client, profile)
    first = benchmark_generation(client, _sequence_prompt(prompt, nonce, 1), max_tokens)
    first["sequence_label"] = FIRST_AFTER_COLD if established_cold else FIRST_OBSERVED
    first["backend_memory"] = _backend_memory(client, profile)
    between = snapshot_endpoint(client, profile)
    warm = benchmark_generation(client, _sequence_prompt(prompt, nonce, 2), max_tokens)
    after = snapshot_endpoint(client, profile)
    continuity = judge_continuity((before, between, after), profile, process)
    warm.update(continuity)
    warm["sequence_label"] = continuity["classification"]
    warm["backend_memory"] = _backend_memory(client, profile)
    lifecycle = {
        "provenance": (
            "harness_established_cold_start" if established_cold else "pre_existing_endpoint"
        ),
        "first_request_label": first["sequence_label"],
        "warm_request_label": warm["sequence_label"],
        "endpoint_process_reused": continuity["endpoint_process_reused"],
        "prompt_prefixes_distinct": True,
        "sequence_nonce": nonce,
        "endpoint_instance_source": before.instance_source,
    }
    return lifecycle, first, warm


def capture_identity(client: EndpointClient, profile: Profile) -> dict[str, Any]:
    result = {
        "model": profile.model,
        "runtime": profile.runtime,
        "device": profile.device,
        "model_source": "profile_declared",
        "runtime_source": "profile_declared",
        "device_source": "profile_declared",
    }
    try:
        models = client.get_json(urljoin(profile.base_url, "models"))
        ids = [str(item.get("id")) for item in (models.get("data") or []) if isinstance(item, dict)]
        result["endpoint_model_ids"] = ids
        result["model_verified_by_models_endpoint"] = profile.model in ids
    except Exception as exc:
        result["model_verified_by_models_endpoint"] = False
        result["model_verification_error"] = str(exc)
    if profile.identity_url:
        try:
            payload = client.get_json(profile.identity_url)
            for field, path in (
                ("model", profile.identity_model_path),
                ("runtime", profile.identity_runtime_path),
                ("device", profile.identity_device_path),
            ):
                value = _deep_get(payload, path)
                if value is not None:
                    result[f"observed_{field}"] = value
                    result[f"{field}_matches_observed"] = str(value) == str(result[field])
                    result[f"{field}_source"] = "endpoint_observed"
        except Exception as exc:
            result["identity_probe_error"] = str(exc)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("endpoint-benchmark.json"))
    parser.add_argument("--cold-start", action="store_true")
    parser.add_argument("--ready-timeout", type=float, default=900.0)
    parser.add_argument("--context-sizes", default="2048,8192,32768")
    parser.add_argument("--generation-prompt", default="Explain deterministic verification in two short paragraphs.")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--cancellation", action="store_true")
    parser.add_argument("--soak-hours", type=float, default=0.0)
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)
    client = EndpointClient(profile)
    targets = [int(item.strip()) for item in args.context_sizes.split(",") if item.strip()]
    if any(value <= 0 for value in targets):
        parser.error("--context-sizes values must be positive")
    if args.max_tokens <= 0 or args.ready_timeout <= 0 or args.soak_hours < 0:
        parser.error("token/time values must be positive; soak may be zero")

    started_process = None
    cold = {"supported": False, "reason": "not requested"}
    try:
        if args.cold_start:
            cold, started_process = start_for_cold_measurement(client, profile, args.ready_timeout)
        if not client.ready():
            raise RuntimeError("endpoint is not ready; configure startup.command or start it before running the harness")
        identity = capture_identity(client, profile)
        lifecycle, first, warm = measure_lifecycle_sequence(client, profile, LifecycleRun(
            prompt=args.generation_prompt,
            max_tokens=args.max_tokens,
            nonce=secrets.token_hex(4),
            established_cold=bool(cold.get("supported")),
            process=started_process,
        ))
        report = {
            "schema_version": 2,
            "captured_at_unix": time.time(),
            "profile_name": profile.name,
            "identity": identity,
            "cold_start": cold,
            "lifecycle": lifecycle,
            "generation": first,
            "warm_generation": warm,
            "context_ladder": benchmark_context_ladder(client, profile, targets),
            "cancellation": benchmark_cancellation(client, profile) if args.cancellation else {"supported": False, "reason": "not requested"},
            "soak": run_soak(client, profile, args.soak_hours, args.generation_prompt, args.max_tokens),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(args.output)
        return 0
    finally:
        if args.cold_start:
            stop_started_endpoint(profile, started_process)


if __name__ == "__main__":
    raise SystemExit(main())
