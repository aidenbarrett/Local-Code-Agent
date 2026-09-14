#!/usr/bin/env python3
"""Measure an explicitly requested command using an already-running HWiNFO CSV log.

Writes a new completion manifest. Never rewrites an immutable pre-run manifest.
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
import platform
import subprocess
import sys
import time


def unobserved(reason, sampler=None):
    return {"energy_joules": None, "sampler": sampler, "sample_hz": None,
            "measurement_quality": "unobserved", "reason": reason}


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
        if [x.strip() for x in row] == header:  # HWiNFO repeats its header on close.
            continue
        if len(row) != len(header):
            raise ValueError("incomplete CSV row; logger may still be flushing")
        stamp = datetime.strptime(row[date_col].strip() + " " + row[time_col].strip(), timestamp_format)
        samples.append((stamp.timestamp(), float(row[power_col].strip())))
    return samples, hashlib.sha256(data).hexdigest()


def measure(path, sensor, timestamp_format, start, end, *, encoding="utf-8-sig",
            delimiter=",", max_gap_seconds=5):
    if not path:
        return unobserved("no sampler configured")
    try:
        samples, digest = read_hwinfo(path, sensor, timestamp_format, encoding=encoding, delimiter=delimiter)
        joules, hz = integrate(samples, start, end, max_gap_seconds)
        return {"energy_joules": joules, "sampler": "HWiNFO64 CSV",
                "sample_hz": hz, "measurement_quality": "sampled",
                "sensor": sensor, "scope": "selected sensor only; no baseline subtraction",
                "csv_sha256": digest, "csv_path": str(Path(path).resolve()),
                "timestamp_basis": "host local time", "timestamp_format": timestamp_format,
                "max_gap_seconds": max_gap_seconds}
    except (OSError, ValueError, IndexError, StopIteration, UnicodeError) as exc:
        return unobserved(str(exc), "HWiNFO64 CSV")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--hwinfo-csv", type=Path)
    parser.add_argument("--sensor", default="CPU Package Power [W]")
    # Explicit format avoids interpreting 9/10 as either September or October.
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
    links = {}
    for name in ("run_manifest", "launch_record"):
        path = getattr(args, name)
        if path:
            links[name] = {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the output before running anything, so existing results cannot be overwritten.
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
            energy = measure(args.hwinfo_csv, args.sensor, args.timestamp_format, start, end,
                             encoding=args.encoding, delimiter=args.delimiter,
                             max_gap_seconds=args.max_gap_seconds)
            if not args.hwinfo_csv or energy["energy_joules"] is not None or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        manifest = {"kind": "command_energy_completion", "command": command,
                    "exit_code": code, "command_error": error, "start_unix": start,
                    "end_unix": end, "host": platform.node(), "platform": platform.platform(),
                    "python": platform.python_version(), "links": links, **energy}
        json.dump(manifest, fh, indent=2); fh.write("\n")
    print(json.dumps(manifest, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
