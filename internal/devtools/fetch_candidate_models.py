#!/usr/bin/env python3
"""Download candidate OpenVINO models into the runtime model store for evaluation.

Weights go where the serving code already looks for them
(``<runtime root>/models/<org>/<name>``), never into the Git tree. Listing a model
here does not make it a supported profile: only ``local_agent.config.MODEL_PRESETS``
decides what is served, and a candidate becomes usable only once a preset names it.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INTERNAL = Path(__file__).resolve().parents[1]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from serving.model_store import default_runtime_root, model_repository  # noqa: E402

GROUPS = ("worker", "specialist", "chat")


@dataclass(frozen=True)
class Candidate:
    repo_id: str
    group: str
    role: str
    note: str = ""


CATALOGUE: tuple[Candidate, ...] = (
    Candidate("OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov", "worker",
              "coding worker, GPU (ptl-gpu-30b / ptl-cpu-30b)"),
    Candidate("OpenVINO/Qwen3-30B-A3B-int4-ov", "worker",
              "general reasoning MoE, GPU comparison against the Coder variant"),
    Candidate("OpenVINO/Qwen3.5-35B-A3B-int4-ov", "worker",
              "newer MoE, GPU", "vision-language model; needs OpenVINO 2026.2 or later"),
    Candidate("OpenVINO/Qwen3.8-27B-int4-ov", "worker",
              "dense quality ceiling (ceiling-27b-dense)", "experimental; needs OpenVINO nightly"),
    Candidate("OpenVINO/Qwen3-8B-int4-cw-ov", "specialist",
              "cheap worker on NPU (ptl-npu-8b)"),
    Candidate("OpenVINO/Qwen3-4B-int4-ov", "specialist", "log and test-output summariser"),
    Candidate("OpenVINO/Qwen3-0.6B-int4-ov", "specialist", "tiny classifier or router"),
    Candidate("OpenVINO/Qwen2.5-Coder-3B-Instruct-int4-ov", "specialist",
              "small coder for narrow edits"),
    Candidate("OpenVINO/Qwen3-Embedding-0.6B-int8-ov", "specialist", "code search embeddings"),
    Candidate("circulus/qwen3-reranker-0.6b-int8-ov", "specialist",
              "code search reranker", "community conversion"),
    Candidate("blaj/Qwen3.5-9B-abliterated-int4-ov", "chat",
              "uncensored chat, no tools or repository authority",
              "community conversion; its card reports testing on OVMS 2026.4"),
)


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def selected(groups: list[str], names: list[str]) -> tuple[Candidate, ...]:
    unknown = sorted(set(groups) - set(GROUPS))
    if unknown:
        raise ValueError(f"unknown group(s): {', '.join(unknown)}")
    known = {c.repo_id for c in CATALOGUE}
    missing = sorted(set(names) - known)
    if missing:
        raise ValueError(f"not in the catalogue: {', '.join(missing)}")
    return tuple(c for c in CATALOGUE if c.group in groups or c.repo_id in names)


def target_directory(runtime_root: Path, candidate: Candidate) -> Path:
    return model_repository(runtime_root) / candidate.repo_id


def _hub() -> Any:
    try:
        import huggingface_hub
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub is not installed; run: python -m pip install huggingface_hub"
        ) from exc
    return huggingface_hub


def remote_size(hub: Any, repo_id: str) -> int:
    info = hub.HfApi().model_info(repo_id, files_metadata=True)
    return sum(int(s.size or 0) for s in (info.siblings or []))


def _gib(size: int) -> str:
    return f"{size / 2**30:.1f} GiB"


def download(candidates: tuple[Candidate, ...], runtime_root: Path, *, headroom_gib: float) -> int:
    hub = _hub()
    sizes = {c.repo_id: remote_size(hub, c.repo_id) for c in candidates}
    total = sum(sizes.values())
    store = model_repository(runtime_root)
    store.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(store).free
    _out(f"{len(candidates)} model(s), {_gib(total)} in total; {_gib(free)} free at {store}")
    if total + headroom_gib * 2**30 > free:
        _err(f"REFUSED: not enough disk space (keeping {headroom_gib:g} GiB headroom)")
        return 2
    for c in candidates:
        target = target_directory(runtime_root, c)
        _out(f"-> {c.repo_id} ({_gib(sizes[c.repo_id])}) into {target}")
        # Already-complete files are skipped, so rerunning resumes an interrupted fetch.
        hub.snapshot_download(repo_id=c.repo_id, local_dir=target)
    _out("done")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", action="append", default=[], choices=GROUPS)
    parser.add_argument("--model", action="append", default=[], help="one catalogue repo id")
    parser.add_argument("--all", action="store_true", help="every group")
    parser.add_argument("--runtime-root", type=Path, default=default_runtime_root())
    parser.add_argument("--headroom-gib", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true", help="show targets, no network")
    args = parser.parse_args(argv)
    try:
        chosen = selected(list(GROUPS) if args.all else args.group, args.model)
    except ValueError as exc:
        _err(f"REFUSED: {exc}")
        return 2
    if not chosen:
        for c in CATALOGUE:
            note = f"  [{c.note}]" if c.note else ""
            _out(f"{c.group:<11} {c.repo_id:<48} {c.role}{note}")
        _out("\nChoose with --group, --model or --all.")
        return 0
    if args.dry_run:
        for c in chosen:
            _out(f"{c.repo_id} -> {target_directory(args.runtime_root, c)}")
        return 0
    try:
        return download(chosen, args.runtime_root, headroom_gib=args.headroom_gib)
    except (RuntimeError, OSError) as exc:
        _err(f"REFUSED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
