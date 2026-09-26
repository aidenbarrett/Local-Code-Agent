#!/usr/bin/env python3
"""Backend-agnostic performance harness for OpenAI-compatible chat endpoints."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from dataclasses import dataclass
from pathlib import Path
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
    memory_path: str | None = None
    identity_url: str | None = None
    identity_model_path: str | None = None
    identity_runtime_path: str | None = None
    identity_device_path: str | None = None
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
        memory_path=str(telemetry["memory_path"]) if telemetry.get("memory_path") else None,
        identity_url=str(identity["url"]) if identity.get("url") else None,
        identity_model_path=str(identity["model_path"]) if identity.get("model_path") else None,
        identity_runtime_path=str(identity["runtime_path"]) if identity.get("runtime_path") else None,
        identity_device_path=str(identity["device_path"]) if identity.get("device_path") else None,
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
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        if response.status >= 400:
            detail = response.read(4096).decode("utf-8", errors="replace")
            conn.close()
            raise RuntimeError(f"chat endpoint returned HTTP {response.status}: {detail}")
        return conn, response, started, response_headers

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


def measure_cold_start(client: EndpointClient, profile: Profile, timeout: float) -> dict[str, Any]:
    if not profile.start_command:
        return {"supported": False, "reason": "profile has no startup command"}
    started = time.perf_counter()
    process = subprocess.Popen(profile.start_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = started + timeout
    last_error = None
    try:
        while time.perf_counter() < deadline:
            try:
                client.get_json(profile.ready_url or urljoin(profile.base_url, "models"))
                ready_ms = (time.perf_counter() - started) * 1000.0
                result = {
                    "supported": True,
                    "client_observed_start_to_ready_ms": ready_ms,
                    "timing_source": "client_boundary",
                }
                compile_time = _backend_value(client, profile, profile.compile_time_path)
                if compile_time is not None:
                    result["backend_reported_compile_time"] = compile_time
                    result["compile_time_source"] = "backend_reported"
                return result
            except Exception as exc:
                last_error = str(exc)
                if process.poll() is not None:
                    raise RuntimeError(f"startup command exited {process.returncode} before ready")
                time.sleep(0.2)
        raise TimeoutError(f"endpoint did not become ready within {timeout}s: {last_error}")
    finally:
        if profile.stop_command:
            subprocess.run(profile.stop_command, check=False)
        elif process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def benchmark_generation(client: EndpointClient, prompt: str, max_tokens: int) -> dict[str, Any]:
    return client.stream_chat(prompt, max_tokens)


def benchmark_context_ladder(client: Any, targets: list[int]) -> list[dict[str, Any]]:
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
        rows.append({
            "requested_approx_tokens": target,
            "observed_prompt_tokens": result.get("prompt_tokens"),
            "request_to_first_token_ms": result.get("ttft_ms"),
            "supported": result.get("ttft_ms") is not None,
            "timing_source": "client_boundary",
        })
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

    report = {
        "schema_version": 1,
        "captured_at_unix": time.time(),
        "profile_name": profile.name,
        "identity": capture_identity(client, profile),
        "cold_start": measure_cold_start(client, profile, args.ready_timeout) if args.cold_start else {"supported": False, "reason": "not requested"},
        "generation": benchmark_generation(client, args.generation_prompt, args.max_tokens),
        "context_ladder": benchmark_context_ladder(client, targets),
        "cancellation": benchmark_cancellation(client, profile) if args.cancellation else {"supported": False, "reason": "not requested"},
        "soak": run_soak(client, profile, args.soak_hours, args.generation_prompt, args.max_tokens),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
