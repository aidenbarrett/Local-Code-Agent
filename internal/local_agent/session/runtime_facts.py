"""Observed and declared inference facts for the public Session Hub.

A preset is intent. `/v1/models` is an observation. Keep the two separate so the Hub
never turns a configured device or model name into evidence about what is actually
serving requests.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import urlopen

from ..config import ModelConfig


ModelsFetcher = Callable[[str], bytes]


def _models_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("model endpoint must be an http(s) URL")
    return f"{parsed.scheme}://{parsed.netloc}/v1/models"


def fetch_models_document(models_url: str) -> bytes:
    with urlopen(models_url, timeout=3) as response:  # noqa: S310 - configured local endpoint
        return response.read(256_000)


def observed_model_id(base_url: str, *, fetch: ModelsFetcher = fetch_models_document) -> str | None:
    """Return one served model id from the endpoint's OpenAI-compatible model list."""
    raw = fetch(_models_url(base_url))
    document = json.loads(raw.decode("utf-8"))
    entries = document.get("data") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise ValueError("/v1/models did not return a data list")
    ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    ids = [value for value in ids if isinstance(value, str) and value.strip()]
    if not ids:
        return None
    return ids[0]


@dataclass(frozen=True)
class RuntimeFacts:
    preset: str
    endpoint: str
    declared_model: str
    declared_device: str
    observed_model: str | None
    execution_enabled: bool

    @classmethod
    def observe(
        cls,
        preset: str,
        config: ModelConfig,
        *,
        execution_enabled: bool,
        fetch: ModelsFetcher = fetch_models_document,
    ) -> "RuntimeFacts":
        try:
            observed = observed_model_id(config.base_url, fetch=fetch)
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            observed = None
        return cls(
            preset=preset,
            endpoint=config.base_url,
            declared_model=config.model,
            declared_device=config.device,
            observed_model=observed,
            execution_enabled=bool(execution_enabled),
        )

    def header(self) -> str:
        model = self.observed_model or "unobserved"
        execution = "enabled" if self.execution_enabled else "disabled"
        return (
            f"model {model} (observed) · device {self.declared_device} (declared) · "
            f"endpoint {self.endpoint} · execution {execution}"
        )

    def answer(self) -> str:
        observed = self.observed_model or "unavailable"
        execution = "enabled" if self.execution_enabled else "disabled"
        return (
            f"Observed endpoint: {self.endpoint}. Observed served model: {observed}. "
            f"Declared preset: {self.preset}. Declared model: {self.declared_model}. "
            f"Declared device: {self.declared_device}. Repository execution: {execution}. "
            "The device is configuration, not hardware-utilisation proof."
        )


__all__ = ["RuntimeFacts", "fetch_models_document", "observed_model_id"]
