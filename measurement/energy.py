#!/usr/bin/env python3
"""Measure one command against an already-running HWiNFO CSV power log.

The completion sidecar is observational. Missing or unqualified energy telemetry
never changes whether the wrapped engineering task was correct.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import time

from local_agent.persistence import sanitize_for_persistence


SENSOR_DOMAIN_STATUSES = (
    "unqualified",
    "confirmed_contains_target",
    "confirmed_excludes_target",
    "proxy_only",
)


def unobserved(reason, sampler=None, *, sensor_domain_status="unqualified", role="task"):
    return {
        "energy_joules": None,
        "sampler": sampler,
        "sample_hz": None,
        "measurement_quality": "unobserved",
        "reason": reason,
        "sensor_domain_status": sensor_domain_status,
        "claim_eligible": False,
        "role": role,
    }


def integrate(samples, start, end, max_gap_seconds=5):
    """Trapezoids clipped to the command window; never extrapolate missing edges."""
    if end <= start or len(samples) < 2:
        raise ValueError("measurement window or samples are insufficient")
    if any(not math.isfinite(t) or not math.isfinite(p) or p < 0 for t, p in samples):
        raise ValueError("nonfinite timestamp/power or negative power")
    if any(b[0] <= a[0] for a, b in zip(samples, samples[1:])):
        raise ValueError("duplicate or non-monotonic sensor timestamps")
    if samples[0][0] > start or samples[-1][0] < end:
        raise ValueError("samples do not bracket the whole command")
    joules = 0.0
    intervals = []
    for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
        lo, hi = max(start, t0), min(end, t1)
        if hi <= lo:
            continue
        if t1 - t0 > max_gap_seconds:
            raise ValueError("sensor gap exceeds declared maximum")
        a = p0 + (p1 - p0) * (lo - t0) / (t1 - t0)
        b = p0 + (p1 - p0) * (hi - t0) / (t1 - t0)
        joules += (a + b) * 0.5 * (hi - lo)
        intervals.append(t1 - t0)
    if not intervals:
        raise ValueError("no sensor interval overlaps the command window")
    return joules, len(intervals) / sum(intervals)


def read_hwinfo(path, sensor, timestamp_format, *, encoding="utf-8-sig", delimiter=","):
    data = Path(path).read_bytes()
    rows = csv.reader(io.StringIO(data.decode(encoding)), delimiter=delimiter)
    header = [x.strip() for x in next(rows)]
    for name in ("Date", "Time", sensor):
        if header.count(name) != 1:
            raise ValueError(f"expected one exact {name!r} column, found {header.count(name)}")
    date_col, time_col, power_col = [header.index(x) for x in ("Date", "Time", sensor)]
    samples = []
    for row in rows:
        if not row or not any(x.strip() for x in row):
            continue
        if [x.strip() for x in row] == header:
            continue
        if len(row) != len(header):
            raise ValueError("incomplete CSV row; logger may still be flushing")
        stamp = datetime.strptime(
            row[date_col].strip() + " " + row[time_col].strip(), timestamp_format
        )
        samples.append((stamp.timestamp(), float(row[power_col].strip())))
    return samples, hashlib.sha256(data).hexdigest()


def measure(
    path,
    sensor,
    timestamp_format,
    start,
    end,
    *,
    encoding="utf-8-sig",
    delimiter=",",
    max_gap_seconds=5,
    sensor_scope="selected sensor only; no baseline subtraction",
    sensor_domain_status="unqualified",
    role="task",
):
    if sensor_domain_status not in SENSOR_DOMAIN_STATUSES:
        raise ValueError(f"unknown sensor domain status: {sensor_domain_status}")
    if not path:
        return unobserved(
            "no sampler configured",
            sensor_domain_status=sensor_domain_status,
            role=role,
        )
    try:
        samples, digest = read_hwinfo(
            path, sensor, timestamp_format, encoding=encoding, delimiter=delimiter
        )
        joules, hz = integrate(samples, start, end, max_gap_seconds)
        return {
            "energy_joules": joules,
            "sampler": "HWiNFO64 CSV",
            "sample_hz": hz,
            "measurement_quality": "sampled",
            "sensor": sensor,
            "scope": sensor_scope,
            "sensor_domain_status": sensor_domain_status,
            "claim_eligible": sensor_domain_status == "confirmed_contains_target",
            "csv_sha256": digest,
            "csv_name": Path(path).name,
            "timestamp_basis": "host local time",
            "timestamp_format": timestamp_format,
            "max_gap_seconds": max_gap_seconds,
            "role": role,
        }
    except (OSError, ValueError, IndexError, StopIteration, UnicodeError) as exc:
        return unobserved(
            str(exc),
            "HWiNFO64 CSV",
            sensor_domain_status=sensor_domain_status,
            role=role,
        )


def joules_per_completed_task(total_joules, completed_tasks):
    if total_joules is None or completed_tasks is None:
        return None
    if completed_tasks < 0:
        raise ValueError("completed task count cannot be negative")
    if completed_tasks == 0:
        return None
    return total_joules / completed_tasks


def battery_delta(
    start_mwh=None,
    end_mwh=None,
    start_percent=None,
    end_percent=None,
    *,
    source=None,
    power_state="unknown",
):
    values = (start_mwh, end_mwh, start_percent, end_percent)
    if all(value is None for value in values):
        return {
            "measurement_quality": "unobserved",
            "drain_mwh": None,
            "drain_percent": None,
            "claim_eligible": False,
            "reason": "no battery observations supplied",
        }
    if (start_mwh is None) != (end_mwh is None):
        raise ValueError("battery mWh requires both start and end observations")
    if (start_percent is None) != (end_percent is None):
        raise ValueError("battery percent requires both start and end observations")
    if source is None:
        raise ValueError("battery observations require --battery-source")
    if power_state not in {"battery", "ac", "unknown"}:
        raise ValueError("battery power state must be battery, ac, or unknown")
    if start_mwh is not None:
        if not all(math.isfinite(v) and v >= 0 for v in (start_mwh, end_mwh)):
            raise ValueError("battery mWh observations must be finite and non-negative")
        if end_mwh > start_mwh:
            raise ValueError("battery end mWh exceeds start mWh; run is not a drain sample")
    if start_percent is not None:
        if not all(math.isfinite(v) and 0 <= v <= 100 for v in (start_percent, end_percent)):
            raise ValueError("battery percentages must be finite and between 0 and 100")
        if end_percent > start_percent:
            raise ValueError("battery end percent exceeds start percent; run is not a drain sample")
    drain_mwh = None if start_mwh is None else start_mwh - end_mwh
    drain_percent = None if start_percent is None else start_percent - end_percent
    return {
        "measurement_quality": "observed",
        "start_mwh": start_mwh,
        "end_mwh": end_mwh,
        "drain_mwh": drain_mwh,
        "start_percent": start_percent,
        "end_percent": end_percent,
        "drain_percent": drain_percent,
        "source": source,
        "power_state": power_state,
        "claim_eligible": power_state == "battery" and drain_mwh is not None,
    }


def _artifact_link(path: Path) -> dict[str, str]:
    return {
        "name": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--hwinfo-csv", type=Path)
    parser.add_argument("--sensor", default="CPU Package Power [W]")
    parser.add_argument(
        "--sensor-scope",
        default="selected sensor only; no baseline subtraction",
    )
    parser.add_argument(
        "--sensor-domain-status",
        choices=SENSOR_DOMAIN_STATUSES,
        default="unqualified",
    )
    parser.add_argument("--role", choices=("task", "idle"), default="task")
    parser.add_argument("--completed-tasks", type=int)
    parser.add_argument("--battery-start-mwh", type=float)
    parser.add_argument("--battery-end-mwh", type=float)
    parser.add_argument("--battery-start-percent", type=float)
    parser.add_argument("--battery-end-percent", type=float)
    parser.add_argument("--battery-source")
    parser.add_argument(
        "--battery-power-state",
        choices=("battery", "ac", "unknown"),
        default="unknown",
    )
    parser.add_argument("--timestamp-format", default="%d.%m.%Y %H:%M:%S.%f")
    parser.add_argument("--encoding", default="utf-8-sig")
    parser.add_argument("--delimiter", default=",")
    parser.add_argument("--max-gap-seconds", type=float, default=5)
    parser.add_argument("--flush-wait-seconds", type=float, default=5)
    parser.add_argument("--run-manifest", type=Path)
    parser.add_argument("--launch-record", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.max_gap_seconds <= 0 or not 0 <= args.flush_wait_seconds <= 60:
        parser.error("supply a command, positive max gap, and flush wait between 0 and 60 seconds")
    if args.completed_tasks is not None and args.completed_tasks < 0:
        parser.error("completed task count cannot be negative")
    try:
        battery = battery_delta(
            args.battery_start_mwh,
            args.battery_end_mwh,
            args.battery_start_percent,
            args.battery_end_percent,
            source=args.battery_source,
            power_state=args.battery_power_state,
        )
    except ValueError as exc:
        parser.error(str(exc))

    links = {}
    for name in ("run_manifest", "launch_record"):
        path = getattr(args, name)
        if path:
            links[name] = _artifact_link(path)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as fh:
        start = time.time()
        try:
            result = subprocess.run(command, check=False)
            code, error = result.returncode, None
        except OSError as exc:
            code, error = 127, str(exc)
        end = time.time()
        deadline = time.monotonic() + args.flush_wait_seconds
        while True:
            energy = measure(
                args.hwinfo_csv,
                args.sensor,
                args.timestamp_format,
                start,
                end,
                encoding=args.encoding,
                delimiter=args.delimiter,
                max_gap_seconds=args.max_gap_seconds,
                sensor_scope=args.sensor_scope,
                sensor_domain_status=args.sensor_domain_status,
                role=args.role,
            )
            if (
                not args.hwinfo_csv
                or energy["energy_joules"] is not None
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.25)

        wall_seconds = end - start
        per_task = joules_per_completed_task(
            energy["energy_joules"], args.completed_tasks
        )
        idle_average_watts = None
        if args.role == "idle" and energy["energy_joules"] is not None and wall_seconds > 0:
            idle_average_watts = energy["energy_joules"] / wall_seconds
        manifest = sanitize_for_persistence({
            "kind": "command_energy_completion",
            "command": command,
            "exit_code": code,
            "command_error": error,
            "start_unix": start,
            "end_unix": end,
            "wall_seconds": wall_seconds,
            "role": args.role,
            "completed_tasks": args.completed_tasks,
            "joules_per_completed_task": per_task,
            "idle_average_watts": idle_average_watts,
            "links": links,
            "battery": battery,
            **energy,
        })
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    print(json.dumps(manifest, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
