"""Host telemetry sampled during a benchmark.

Best effort by design. Every source here is optional and every one of them
degrades to "not available on this host" rather than failing a run, because a
benchmark that refuses to start because it cannot read a power counter is
worse than a benchmark with one column missing.

What it tries, in order of preference:

  CPU and memory   psutil if installed, else /proc on Linux, else nothing
  package energy   Intel RAPL via /sys/class/powercap on Linux, else nothing

Energy is the interesting column for an NPU argument. Joules per completed task
is a far better number than watts, because it is invariant to how long the task
took, and "same answer for a fifth of the energy" is an argument that lands
with people who do not care about tokens per second.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_RAPL_ROOT = Path("/sys/class/powercap")


def _psutil():
    try:
        import psutil  # type: ignore

        return psutil
    except ImportError:
        return None


def _rapl_domains() -> list[tuple[str, Path]]:
    """Intel RAPL package energy counters, if this kernel exposes them."""
    domains: list[tuple[str, Path]] = []
    if not _RAPL_ROOT.is_dir():
        return domains
    for entry in sorted(_RAPL_ROOT.glob("intel-rapl:*")):
        energy = entry / "energy_uj"
        name_file = entry / "name"
        if energy.is_file() and name_file.is_file():
            try:
                energy.read_text()
            except OSError:
                continue  # readable only as root on many distributions
            domains.append((name_file.read_text().strip(), energy))
    return domains


def _read_energy_uj(domains: list[tuple[str, Path]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, path in domains:
        try:
            out[name] = int(path.read_text().strip())
        except (OSError, ValueError):
            continue
    return out


def _proc_cpu_times() -> tuple[float, float] | None:
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    except (OSError, IndexError):
        return None
    values = [float(v) for v in fields]
    idle = values[3] + (values[4] if len(values) > 4 else 0.0)
    return sum(values), idle


def _proc_mem_used_gb() -> float | None:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            info[key] = float(rest.strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    if total is None or available is None:
        return None
    return round((total - available) / 1e6, 2)


@dataclass
class TelemetrySample:
    t: float
    cpu_percent: float | None
    mem_used_gb: float | None


@dataclass
class TelemetryReport:
    available: dict[str, bool] = field(default_factory=dict)
    duration_s: float = 0.0
    samples: int = 0
    mean_cpu_percent: float | None = None
    peak_cpu_percent: float | None = None
    peak_mem_used_gb: float | None = None
    energy_j: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def package_energy_j(self) -> float | None:
        for key in ("package-0", "package", "psys"):
            if key in self.energy_j:
                return self.energy_j[key]
        return next(iter(self.energy_j.values()), None)

    def average_watts(self) -> float | None:
        energy = self.package_energy_j
        if energy is None or self.duration_s <= 0:
            return None
        return round(energy / self.duration_s, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "duration_s": round(self.duration_s, 2),
            "samples": self.samples,
            "mean_cpu_percent": self.mean_cpu_percent,
            "peak_cpu_percent": self.peak_cpu_percent,
            "peak_mem_used_gb": self.peak_mem_used_gb,
            "energy_j": {k: round(v, 1) for k, v in self.energy_j.items()},
            "package_energy_j": (
                round(self.package_energy_j, 1) if self.package_energy_j else None
            ),
            "average_watts": self.average_watts(),
            "notes": self.notes,
        }


class HostMonitor:
    """Samples the host in a background thread for the duration of a `with`."""

    def __init__(self, interval_s: float = 1.0) -> None:
        self.interval_s = interval_s
        self._samples: list[TelemetrySample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._psutil = _psutil()
        self._rapl = _rapl_domains()
        self._energy_start: dict[str, int] = {}
        self._started = 0.0
        self.report = TelemetryReport()

    # ------------------------------------------------------------ sampling

    def _sample(self) -> TelemetrySample:
        cpu = mem = None
        if self._psutil is not None:
            cpu = self._psutil.cpu_percent(interval=None)
            mem = round(self._psutil.virtual_memory().used / 1e9, 2)
        else:
            times = _proc_cpu_times()
            if times is not None and getattr(self, "_last_times", None) is not None:
                total_delta = times[0] - self._last_times[0]
                idle_delta = times[1] - self._last_times[1]
                if total_delta > 0:
                    cpu = round(100.0 * (1.0 - idle_delta / total_delta), 1)
            self._last_times = times
            mem = _proc_mem_used_gb()
        return TelemetrySample(t=time.monotonic(), cpu_percent=cpu, mem_used_gb=mem)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self._samples.append(self._sample())
            except Exception:  # pragma: no cover - telemetry must never fail a run
                break

    # -------------------------------------------------------------- context

    def __enter__(self) -> "HostMonitor":
        self._last_times = _proc_cpu_times()
        if self._psutil is not None:
            self._psutil.cpu_percent(interval=None)  # prime the counter
        self._energy_start = _read_energy_uj(self._rapl)
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s * 2)

        duration = time.monotonic() - self._started
        cpu_values = [s.cpu_percent for s in self._samples if s.cpu_percent is not None]
        mem_values = [s.mem_used_gb for s in self._samples if s.mem_used_gb is not None]

        energy_end = _read_energy_uj(self._rapl)
        energy: dict[str, float] = {}
        for name, end in energy_end.items():
            start = self._energy_start.get(name)
            if start is None:
                continue
            delta = end - start
            if delta < 0:  # the 32 bit counter wrapped; the sample is unusable
                continue
            energy[name] = delta / 1e6

        report = TelemetryReport(
            available={
                "psutil": self._psutil is not None,
                "proc": _proc_cpu_times() is not None,
                "rapl": bool(self._rapl),
            },
            duration_s=duration,
            samples=len(self._samples),
            mean_cpu_percent=(
                round(sum(cpu_values) / len(cpu_values), 1) if cpu_values else None
            ),
            peak_cpu_percent=round(max(cpu_values), 1) if cpu_values else None,
            peak_mem_used_gb=max(mem_values) if mem_values else None,
            energy_j=energy,
        )
        if not report.available["rapl"]:
            report.notes.append(
                "no readable RAPL counters, so no energy figure. On Linux these "
                "live under /sys/class/powercap and often need root. On Windows "
                "use Intel Power Gadget or SoC Watch alongside the run and paste "
                "the figure in by hand."
            )
        if not report.available["psutil"] and not report.available["proc"]:
            report.notes.append(
                "no CPU or memory sampling available; pip install psutil for it"
            )
        self.report = report
