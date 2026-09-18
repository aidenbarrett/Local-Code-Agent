"""DESIGN CONTRACT ONLY. M4 device samplers, separate from proof/evaluation.

Implement host-labelled psutil CPU/RAM, then validated Windows GPU/NPU counters.
Missing NPU counters mean unavailable, never 0%. Advertised model device is not
observed execution device. Remote repo host and model host get separate samples.
TTFT/token counts already come from CallStats. Do not call non-streamed latency
TTFT, or label average total throughput decode rate. Operational telemetry does
not become research evidence without a newly frozen measurement protocol.
"""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DeviceSample:
    host_id: str
    device_id: str
    source: str
    timestamp_utc: str
    utilization_percent: float | None = None
    memory_bytes: int | None = None
    unavailable_reason: str | None = None


class DeviceSampler(Protocol):
    def sample(self) -> list[DeviceSample]: ...
