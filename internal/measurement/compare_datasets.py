#!/usr/bin/env python3
"""Compare eval runs from two or more configurations and produce the report.

This is the artefact you put in front of people at work. It answers three
questions and refuses to answer any others:

  1. does it get the right answer            (score, per case)
  2. what does it cost in wall clock         (median seconds, TTFT)
  3. is it a daily driver or a batch tool    (against a threshold set in advance)

    python measurement/compare_datasets.py evals-30b-cpu.json evals-8b-npu.json \\
        --markdown comparison.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evaluation"))
sys.path.insert(0, str(REPO))

try:
    from task_contracts import DIAGNOSTIC_CASES, KILL_THRESHOLD
except Exception:  # pragma: no cover - keeps the script standalone
    DIAGNOSTIC_CASES = {
        "compile-error-locate", "compile-error-fix",
        "test-failure-diagnose", "segfault",
    }
    KILL_THRESHOLD = 0.6

# Above this, a turn is interactive. Beyond it you are in batch territory, and
# that is a different product with a different pitch, not a worse version of
# the same one.
INTERACTIVE_TURN_SECONDS = 10.0


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_path"] = str(path)
    return data


def _agg(rows: list[dict], case: str, key: str) -> float | None:
    values = [r.get(key) for r in rows if r["case"] == case and r.get(key) is not None]
    return round(statistics.median(values), 2) if values else None


def _metric(rows: list[dict], key: str) -> float | None:
    values = [
        r["metrics"][key]
        for r in rows
        if r.get("metrics") and r["metrics"].get(key) is not None
    ]
    return round(statistics.median(values), 2) if values else None


def _score(rows: list[dict], case: str) -> float | None:
    values = [r["score"] for r in rows if r["case"] == case]
    return round(statistics.mean(values), 2) if values else None


def compare(runs: list[dict[str, Any]]) -> dict[str, Any]:
    cases: list[str] = []
    for run in runs:
        for row in run["rows"]:
            if row["case"] not in cases:
                cases.append(row["case"])

    table = []
    for case in cases:
        entry: dict[str, Any] = {"case": case, "diagnostic": case in DIAGNOSTIC_CASES}
        for run in runs:
            label = run["label"]
            entry[label] = {
                "score": _score(run["rows"], case),
                "seconds": _agg(run["rows"], case, "elapsed_s"),
                "tool_calls": _agg(run["rows"], case, "tool_calls"),
            }
        table.append(entry)

    verdicts = []
    for run in runs:
        rows = run["rows"]
        diagnostic = [r for r in rows if r["case"] in DIAGNOSTIC_CASES]
        diag = round(statistics.mean([r["score"] for r in diagnostic]), 3) if diagnostic else None
        all_seconds = [r["elapsed_s"] for r in rows if r.get("elapsed_s")]
        ttft = _metric(rows, "median_ttft_s")
        compactions = sum(r.get("metrics", {}).get("compactions", 0) for r in rows)

        if diag is None:
            call = "no diagnostic cases were run"
        elif diag < KILL_THRESHOLD:
            call = f"below the {KILL_THRESHOLD} kill threshold: not a daily driver"
        elif ttft is not None and ttft > INTERACTIVE_TURN_SECONDS:
            call = (
                f"accurate enough, but {ttft:.1f}s to first token per turn: "
                "batch and background work, not interactive"
            )
        else:
            call = "accurate and responsive enough to sit in the loop"

        verdicts.append(
            {
                "label": run["label"],
                "model": run.get("model"),
                "device": run.get("device"),
                "overall": round(run.get("overall", 0.0), 3),
                "diagnostic_score": diag,
                "median_task_seconds": (
                    round(statistics.median(all_seconds), 1) if all_seconds else None
                ),
                "p90_task_seconds": (
                    round(sorted(all_seconds)[int(len(all_seconds) * 0.9) - 1], 1)
                    if len(all_seconds) >= 2
                    else None
                ),
                "median_ttft_s": ttft,
                "median_decode_tok_s": _metric(rows, "median_decode_tok_s"),
                "cache_hit_ratio": _metric(rows, "cache_hit_ratio"),
                "compactions": compactions,
                "verdict": call,
            }
        )

    return {"cases": table, "verdicts": verdicts, "kill_threshold": KILL_THRESHOLD}


def render(report: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    labels = [r["label"] for r in runs]
    lines = ["# Local agent: configuration comparison", ""]

    lines += ["## Verdict", ""]
    for v in report["verdicts"]:
        lines += [
            f"### {v['label']}",
            "",
            f"- model: `{v['model']}` on **{v['device']}**",
            f"- overall weighted score: **{v['overall']}**",
            f"- diagnostic subset: **{v['diagnostic_score']}** "
            f"(threshold {report['kill_threshold']})",
            f"- median task: {v['median_task_seconds']}s, p90 {v['p90_task_seconds']}s",
            f"- median time to first token: {v['median_ttft_s']}s, "
            f"decode {v['median_decode_tok_s']} tok/s",
            f"- prompt cache hit ratio: {v['cache_hit_ratio']}, "
            f"context compactions: {v['compactions']}",
            "",
            f"**{v['verdict']}**",
            "",
        ]

    header = "| case | " + " | ".join(f"{l} score | {l} s" for l in labels) + " |"
    sep = "|---|" + "".join("---:|---:|" for _ in labels)
    lines += ["## Per case", "", header, sep]
    for entry in report["cases"]:
        name = entry["case"] + (" *" if entry["diagnostic"] else "")
        cells = []
        for label in labels:
            data = entry.get(label) or {}
            cells.append(str(data.get("score")))
            cells.append(str(data.get("seconds")))
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "`*` diagnostic case: counts toward the kill threshold.", ""]

    lines += [
        "## Method",
        "",
        "Every case runs against a fresh copy of the same synthetic C++ "
        "repository with a known defect injected, in a fresh git repo, with the "
        "same skills and the same tool policy. Scores are the fraction of "
        "behavioural checks passed: did it run the build, did it name the right "
        "file and line, did it verify before claiming success, did it stay "
        "within the call budget. Nothing is graded on prose.",
        "",
        "Timings exclude model load. The first call of any session pays graph "
        "compilation and is discarded by the benchmark harness; the eval "
        "harness reports wall clock as the operator experiences it, which "
        "includes real cmake and ctest execution.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", help="evals*.json files from run_evaluation.py")
    parser.add_argument("--markdown", help="write the report here as well as stdout")
    parser.add_argument("--json", dest="json_out", help="write the structured comparison")
    args = parser.parse_args()

    runs = [load(Path(p)) for p in args.runs]
    for i, run in enumerate(runs):
        run.setdefault("label", run.get("model") or f"run{i}")

    report = compare(runs)
    text = render(report, runs)
    print(text)

    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8")
        print(f"markdown: {args.markdown}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"json: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
