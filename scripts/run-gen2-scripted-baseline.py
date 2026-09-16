#!/usr/bin/env python3
"""Run the preregistered deterministic Generation-2 scripted baseline.

This runner deliberately has no model client. It executes fixed procedures on a
fresh copy of the benchmark fixture, freezes a templated evidence report, and
only then scores that frozen report against the preregistered observable facts.

It is a comparator for the eight non-repair pilot tasks, not a model row.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "benchmark_fixture" / "cpp_project"
CASES = (
    "clean-build",
    "compile-error-locate",
    "link-error",
    "test-failure-diagnose",
    "segfault",
    "timeout",
    "navigation",
    "review-restraint",
)
SCENARIO = {
    "clean-build": "clean",
    "compile-error-locate": "compile_error",
    "link-error": "link_error",
    "test-failure-diagnose": "test_failure",
    "segfault": "crash",
    "timeout": "timeout",
    "navigation": "clean",
    "review-restraint": "clean",
}


@dataclass
class CommandEvidence:
    argv: list[str]
    cwd: str
    returncode: int
    elapsed_s: float
    stdout: str
    stderr: str


def run(argv: list[str], cwd: Path, timeout: float = 120.0) -> CommandEvidence:
    started = time.monotonic()
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return CommandEvidence(
            argv=argv,
            cwd=str(cwd),
            returncode=p.returncode,
            elapsed_s=round(time.monotonic() - started, 3),
            stdout=p.stdout,
            stderr=p.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandEvidence(
            argv=argv,
            cwd=str(cwd),
            returncode=124,
            elapsed_s=round(time.monotonic() - started, 3),
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=((exc.stderr or "") if isinstance(exc.stderr, str) else "") + "\nPROCESS TIMEOUT",
        )


def init_repo(work: Path) -> list[CommandEvidence]:
    evidence = [run(["git", "init", "-q"], work)]
    evidence.append(run(["git", "config", "user.name", "baseline"], work))
    evidence.append(run(["git", "config", "user.email", "baseline@example.invalid"], work))
    evidence.append(run(["git", "add", "."], work))
    evidence.append(run(["git", "commit", "-q", "-m", "fixture baseline"], work))
    return evidence


def apply_scenario(work: Path, scenario: str) -> CommandEvidence:
    return run([sys.executable, str(work / "scripts" / "apply_scenario.py"), scenario], work)


def configure(work: Path) -> CommandEvidence:
    return run(["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Debug"], work)


def build(work: Path) -> CommandEvidence:
    return run(["cmake", "--build", "build", "--parallel", "2"], work)


def ctest(work: Path, timeout_s: int = 30) -> CommandEvidence:
    return run(
        ["ctest", "--test-dir", "build", "--output-on-failure", "--timeout", str(timeout_s)],
        work,
        timeout=max(60, timeout_s * 6),
    )


def first_compiler_error(text: str) -> str:
    patterns = (
        r"(?mi)^.*\.(?:c|cc|cpp|cxx|h|hpp):\d+(?::\d+)?:\s*(?:fatal\s+)?error:.*$",
        r"(?mi)^.*\.(?:c|cc|cpp|cxx|h|hpp)\(\d+(?:,\d+)?\)\s*:\s*error\s+[^:]+:.*$",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0).strip()
    for line in text.splitlines():
        if "error" in line.lower():
            return line.strip()
    return "NO COMPILER ERROR PARSED"


def linker_symbols(text: str) -> list[str]:
    found: list[str] = []
    patterns = (
        r"undefined reference to [`'\"]([^`'\"]+)",
        r"unresolved external symbol\s+([^\s]+)",
    )
    for pattern in patterns:
        found.extend(re.findall(pattern, text, flags=re.I))
    out: list[str] = []
    for item in found:
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out[:8]


def search_symbol_files(work: Path, symbols: Iterable[str]) -> list[str]:
    hits: list[str] = []
    for symbol in symbols:
        bare = re.sub(r"\(.*", "", symbol).split("::")[-1]
        if not bare:
            continue
        for path in sorted(work.rglob("*")):
            if not path.is_file() or "build" in path.parts or ".git" in path.parts:
                continue
            if path.suffix.lower() not in {".cpp", ".cc", ".cxx", ".h", ".hpp"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if bare in text:
                rel = path.relative_to(work).as_posix()
                hit = f"{bare}: {rel}"
                if hit not in hits:
                    hits.append(hit)
    return hits[:20]


def failing_test_tokens(text: str) -> list[str]:
    names: list[str] = []
    for line in text.splitlines():
        low = line.lower()
        if "***failed" in low or "***exception" in low or "***timeout" in low or "segfault" in low:
            m = re.search(r"(?:Test\s+#?\d+:\s*)?([A-Za-z0-9_.-]+)", line)
            if m:
                name = m.group(1)
                if name.lower() not in {"test", "tests"} and name not in names:
                    names.append(name)
    return names[:8]


def relevant_snippets(work: Path, tokens: Iterable[str], max_files: int = 8) -> list[dict[str, str]]:
    normalized = [re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_") for t in tokens]
    normalized = [t for t in normalized if t]
    results: list[dict[str, str]] = []
    for path in sorted(work.rglob("*")):
        if len(results) >= max_files:
            break
        if not path.is_file() or "build" in path.parts or ".git" in path.parts or "scenarios" in path.parts:
            continue
        if path.suffix.lower() not in {".cpp", ".cc", ".cxx", ".h", ".hpp"}:
            continue
        rel = path.relative_to(work).as_posix()
        rel_norm = re.sub(r"[^a-z0-9]+", "_", rel.lower())
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text_norm = re.sub(r"[^a-z0-9]+", "_", text.lower())
        if normalized and not any(t in rel_norm or t in text_norm for t in normalized):
            continue
        results.append({"path": rel, "text": "\n".join(text.splitlines()[:140])})
    return results


def navigation_report(work: Path) -> str:
    chunks: list[str] = []
    for path in sorted(work.rglob("*")):
        if not path.is_file() or "build" in path.parts or ".git" in path.parts or "scenarios" in path.parts:
            continue
        if path.suffix.lower() not in {".cpp", ".cc", ".cxx", ".h", ".hpp"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "RingBuffer" in text:
            chunks.append(f"FILE {path.relative_to(work).as_posix()}\n{text}")
    return "\n\n".join(chunks[:6])


def make_report(case: str, work: Path) -> tuple[str, list[CommandEvidence], dict]:
    ev: list[CommandEvidence] = []
    meta: dict = {"scenario": SCENARIO[case]}
    ev.append(apply_scenario(work, SCENARIO[case]))

    if case == "clean-build":
        ev += [configure(work), build(work), ctest(work)]
        report = "\n".join(
            [
                "clean-build deterministic report",
                f"configure_rc={ev[-3].returncode}",
                f"build_rc={ev[-2].returncode}",
                f"ctest_rc={ev[-1].returncode}",
                ev[-1].stdout,
                ev[-1].stderr,
            ]
        )
        return report, ev, meta

    if case == "compile-error-locate":
        ev += [configure(work), build(work)]
        merged = ev[-1].stdout + "\n" + ev[-1].stderr
        first = first_compiler_error(merged)
        meta["first_compiler_error"] = first
        return f"first compiler error:\n{first}\n\nraw build output:\n{merged}", ev, meta

    if case == "link-error":
        ev += [configure(work), build(work)]
        merged = ev[-1].stdout + "\n" + ev[-1].stderr
        symbols = linker_symbols(merged)
        hits = search_symbol_files(work, symbols)
        meta["linker_symbols"] = symbols
        meta["symbol_hits"] = hits
        report = (
            "linker failure:\n"
            + merged
            + "\n\nparsed undefined symbols:\n"
            + "\n".join(symbols or ["NONE PARSED"])
            + "\n\nsource files mentioning parsed symbols:\n"
            + "\n".join(hits or ["NONE"])
        )
        return report, ev, meta

    if case in {"test-failure-diagnose", "segfault", "timeout"}:
        ev += [configure(work), build(work)]
        test = ctest(work, 3 if case == "timeout" else 30)
        ev.append(test)
        merged = test.stdout + "\n" + test.stderr
        names = failing_test_tokens(merged)
        snippets = relevant_snippets(work, names)
        meta["failing_test_tokens"] = names
        meta["snippets"] = snippets
        report = (
            "test evidence:\n"
            + merged
            + "\n\nrelated source/test snippets:\n"
            + "\n\n".join(f"FILE {s['path']}\n{s['text']}" for s in snippets)
        )
        return report, ev, meta

    if case == "navigation":
        report = navigation_report(work)
        meta["files_reported"] = report.count("FILE ")
        return report, ev, meta

    if case == "review-restraint":
        status = run(["git", "status", "--porcelain"], work)
        diff = run(["git", "diff", "--"], work)
        ev += [status, diff]
        clean = status.returncode == 0 and not status.stdout.strip() and not diff.stdout.strip()
        meta["working_tree_clean"] = clean
        report = (
            f"working_tree_clean={str(clean).lower()}\n"
            f"git_status:\n{status.stdout}\n"
            f"git_diff:\n{diff.stdout}\n"
        )
        return report, ev, meta

    raise AssertionError(case)


def score_frozen_report(case: str, report: str, evidence: list[CommandEvidence]) -> tuple[bool, list[str]]:
    """Score only after report generation; never feeds facts back to the procedure."""
    low = report.lower()
    reasons: list[str] = []

    if case == "clean-build":
        ok = all(item.returncode == 0 for item in evidence[-3:])
        if not ok:
            reasons.append("configure/build/test did not all pass")
        return ok, reasons

    expectations = {
        "compile-error-locate": (
            ("ring_buffer.cpp",),
            ("13",),
            ("count_", "count"),
        ),
        "link-error": (("checksum",), ("ring_buffer.cpp", "ring_buffer")),
        "test-failure-diagnose": (("ring_buffer",), ("full", "off by one", "capacity")),
        "segfault": (("text_util",), ("null", "nullptr", "dereference", "trim")),
        "timeout": (("slow",), ("wrap", "unsigned char", "255", "never")),
        "navigation": (("ring_buffer.hpp",), ("false", "rejected", "refuses")),
        "review-restraint": (("working_tree_clean=true",),),
    }
    groups = expectations[case]
    ok = True
    for group in groups:
        if not any(term in low for term in group):
            ok = False
            reasons.append("missing one of: " + ", ".join(group))
    return ok, reasons


def identities() -> dict:
    commit = run(["git", "rev-parse", "HEAD"], ROOT)
    status = run(["git", "status", "--porcelain"], ROOT)
    out = {
        "repository_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "repository_clean": status.returncode == 0 and not status.stdout.strip(),
    }
    try:
        sys.path.insert(0, str(ROOT))
        from local_agent import provenance

        out.update(
            {
                "source_sha256": provenance.source_sha256(),
                "base_prompt_sha256": provenance.base_prompt_sha256(),
                "outcome_contract_sha256": provenance.outcome_contract_sha256(),
            }
        )
    except Exception as exc:  # context only, baseline can still run
        out["identity_error"] = str(exc)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case", choices=CASES)
    args = parser.parse_args(argv)

    if shutil.which("cmake") is None or shutil.which("ctest") is None or shutil.which("git") is None:
        parser.error("cmake, ctest and git must be available in PATH")

    ident = identities()
    if not ident.get("repository_clean"):
        parser.error("source checkout must be clean before the scripted baseline")

    selected = (args.case,) if args.case else CASES
    results = []
    with tempfile.TemporaryDirectory(prefix="gen2-scripted-baseline-") as td:
        scratch = Path(td)
        for case in selected:
            work = scratch / case
            shutil.copytree(FIXTURE, work)
            setup = init_repo(work)
            report, evidence, meta = make_report(case, work)
            frozen_report = str(report)
            passed, score_reasons = score_frozen_report(case, frozen_report, evidence)
            results.append(
                {
                    "case": case,
                    "scenario": SCENARIO[case],
                    "passed": passed,
                    "score_reasons": score_reasons,
                    "report": frozen_report,
                    "meta": meta,
                    "setup_evidence": [asdict(x) for x in setup],
                    "command_evidence": [asdict(x) for x in evidence],
                }
            )

    payload = {
        "kind": "gen2_scripted_baseline",
        "not_a_model_row": True,
        "preregistered_cases": list(CASES),
        "selected_cases": list(selected),
        "identities": ident,
        "passed": sum(r["passed"] for r in results),
        "total": len(results),
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        raise FileExistsError(f"refusing to overwrite {args.out}")
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"scripted baseline: {payload['passed']}/{payload['total']}")
    for row in results:
        print(f"  {row['case']}: {'PASS' if row['passed'] else 'FAIL'}")
        for reason in row["score_reasons"]:
            print(f"    {reason}")
    print(f"evidence: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
