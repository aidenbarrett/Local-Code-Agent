#!/usr/bin/env python3
"""Capture observations that cannot be reconstructed after an experiment.

This file is deliberately boring. It runs before a model evaluation and writes
an immutable sidecar describing the execution host, toolchain, requested target,
model/runtime configuration, and the exact OpenAI tool schemas offered by every
selected case/condition. Transcripts already archive rendered messages; schemas
were the remaining model-facing bytes that were only hashed.

A hash is an identifier, not an archive. This manifest stores both.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import locale
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
EVALUATION = REPO / "evaluation"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(EVALUATION))

from task_contracts import CASES  # noqa: E402
from local_agent.agent import Orchestrator, SkillLibrary  # noqa: E402
from local_agent.config import MODEL_PRESETS, load_repo_config  # noqa: E402
from local_agent.provenance import package_identity  # noqa: E402
from local_agent.tools import build_registry  # noqa: E402

FIXTURE = REPO / "benchmark_fixture" / "cpp_project"


# Project-declared architecture metadata. These fields describe the model
# configuration; they are not performance measurements and do not imply compute
# cost. In particular, active parameter count does not settle runtime cost.
_MODEL_ARCHITECTURES: dict[str, dict[str, Any]] = {
    "OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov": {
        "family": "Qwen3-Coder-30B-A3B-Instruct",
        "architecture": "moe",
        "total_parameters_b": 30.5,
        "active_parameters_b": 3.3,
        "metadata_quality": "project_declared",
        "interpretation": (
            "active parameters are architecture metadata, not a measurement of "
            "runtime compute or memory traffic"
        ),
    },
    "qwen3-coder-30b": {
        "family": "Qwen3-Coder-30B-A3B-Instruct",
        "architecture": "moe",
        "total_parameters_b": 30.5,
        "active_parameters_b": 3.3,
        "metadata_quality": "project_declared",
        "interpretation": (
            "active parameters are architecture metadata, not a measurement of "
            "runtime compute or memory traffic"
        ),
    },
    "OpenVINO/Qwen3-8B-int4-cw-ov": {
        "family": "Qwen3-8B",
        "architecture": "dense",
        "parameter_label": "8B",
        "metadata_quality": "project_declared",
    },
    "qwen3-8b": {
        "family": "Qwen3-8B",
        "architecture": "dense",
        "parameter_label": "8B",
        "metadata_quality": "project_declared",
    },
}


class _NoClient:
    def chat(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - should never run
        raise RuntimeError("manifest capture must never call a model")


def _decode(raw: bytes) -> tuple[str, str]:
    """Decode probe output deterministically, recording what was required."""
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        enc = locale.getpreferredencoding(False) or "utf-8"
        return raw.decode(enc, "replace"), f"{enc}-replace"


def _probe(argv: list[str]) -> dict[str, Any]:
    exe = shutil.which(argv[0])
    if exe is None:
        return {
            "argv": argv,
            "available": False,
            "exit_code": None,
            "first_line": None,
            "raw_sha256": None,
            "decoded_with": None,
        }
    try:
        proc = subprocess.run(
            [exe, *argv[1:]], capture_output=True, timeout=20, check=False
        )
    except OSError as exc:
        return {
            "argv": argv,
            "available": True,
            "exit_code": None,
            "error": str(exc),
            "first_line": None,
            "raw_sha256": None,
            "decoded_with": None,
        }
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    text, encoding = _decode(raw)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return {
        "argv": argv,
        "available": True,
        "resolved_executable": exe,
        "exit_code": proc.returncode,
        "first_line": lines[0] if lines else "",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "decoded_with": encoding,
    }


def _cmake_cache_identity() -> dict[str, Any]:
    """Configure a disposable fixture and read the generator/compiler CMake chose."""
    keys = {
        "CMAKE_GENERATOR": "generator",
        "CMAKE_CXX_COMPILER": "cxx_compiler",
        "CMAKE_CXX_COMPILER_ID": "cxx_compiler_id",
        "CMAKE_CXX_COMPILER_VERSION": "cxx_compiler_version",
        "CMAKE_C_COMPILER": "c_compiler",
        "CMAKE_C_COMPILER_ID": "c_compiler_id",
        "CMAKE_C_COMPILER_VERSION": "c_compiler_version",
    }
    out: dict[str, Any] = {name: None for name in keys.values()}
    cmake = shutil.which("cmake")
    if cmake is None:
        out["quality"] = "unobserved_cmake_missing"
        return out

    with tempfile.TemporaryDirectory(prefix="lca-id-") as tmp:
        build = Path(tmp) / "b"
        proc = subprocess.run(
            [cmake, "-S", str(FIXTURE), "-B", str(build), "-DCMAKE_BUILD_TYPE=Debug"],
            capture_output=True,
            timeout=90,
            check=False,
        )
        out["configure_exit_code"] = proc.returncode
        raw = (proc.stdout or b"") + (proc.stderr or b"")
        out["configure_raw_sha256"] = hashlib.sha256(raw).hexdigest()
        if proc.returncode != 0:
            text, enc = _decode(raw)
            out["quality"] = "configure_failed"
            out["configure_decoded_with"] = enc
            out["configure_tail"] = text.splitlines()[-10:]
            return out
        cache = build / "CMakeCache.txt"
        if not cache.is_file():
            out["quality"] = "cache_missing"
            return out
        for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line or "=" not in line:
                continue
            lhs, value = line.split("=", 1)
            key = lhs.split(":", 1)[0]
            if key in keys:
                out[keys[key]] = value
        out["quality"] = "observed_from_disposable_cmake_configure"
    return out


def host_identity() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable": sys.executable,
    }


def toolchain_identity() -> dict[str, Any]:
    compiler = os.environ.get("CXX")
    if compiler:
        compiler_probe = _probe([compiler, "--version"])
        compiler_probe["selection"] = "CXX_environment"
    else:
        candidates = ["c++", "g++", "clang++", "cl"]
        chosen = next((name for name in candidates if shutil.which(name)), None)
        compiler_probe = _probe([chosen, "--version"] if chosen else ["c++", "--version"])
        compiler_probe["selection"] = "first_available_common_compiler"

    return {
        "git": _probe(["git", "--version"]),
        "cmake": _probe(["cmake", "--version"]),
        "ctest": _probe(["ctest", "--version"]),
        "compiler_probe": compiler_probe,
        "cmake_configure": _cmake_cache_identity(),
    }


def _schema_hash(schemas: list[dict[str, Any]]) -> str:
    # Must match evaluation.run_evaluation._schema_hash byte-for-byte.
    blob = json.dumps(schemas, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def schema_archive(
    conditions: list[str], selected_case_names: set[str] | None = None
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    repo = load_repo_config(FIXTURE)
    registry, _, _ = build_registry(repo)
    skills = SkillLibrary.discover(REPO / "skills")
    # ContractOrchestrator.__init__ registers read_skill_reference, so the
    # registry here is the exact registry a real run sees before narrowing.
    orch = Orchestrator(repo, registry, _NoClient(), skills)

    archive: dict[str, list[dict[str, Any]]] = {}
    rows: list[dict[str, Any]] = []
    for case in CASES:
        if selected_case_names and case.name not in selected_case_names:
            continue
        skill = skills.get(case.skill) if case.skill else None
        for condition in conditions:
            if condition == "control":
                toolset = orch._toolset_for(None, None, no_skill=True)
            else:
                if skill is None:
                    raise RuntimeError(
                        f"case {case.name!r} has no skill but condition {condition!r} "
                        "requires a pinned skill/tool boundary"
                    )
                toolset = orch._toolset_for(skill.name, skill, no_skill=False)
            schemas = registry.schemas(sorted(toolset))
            digest = _schema_hash(schemas)
            previous = archive.setdefault(digest, schemas)
            if previous != schemas:  # pragma: no cover - cryptographic collision guard
                raise RuntimeError(f"tool schema hash collision at {digest}")
            rows.append(
                {
                    "case": case.name,
                    "condition": condition,
                    "offered_tools": sorted(toolset),
                    "tool_schema_hash": digest,
                }
            )
    return archive, rows


def model_architecture(model_id: str) -> dict[str, Any]:
    return dict(
        _MODEL_ARCHITECTURES.get(
            model_id,
            {
                "family": None,
                "architecture": "unknown",
                "metadata_quality": "unrecorded",
            },
        )
    )


def capture_manifest(
    profile: str,
    conditions: list[str],
    selected_case_names: set[str] | None = None,
) -> dict[str, Any]:
    if profile not in MODEL_PRESETS:
        raise ValueError(f"unknown profile {profile!r}; choose one of {sorted(MODEL_PRESETS)}")
    model = MODEL_PRESETS[profile]
    schemas, case_schemas = schema_archive(conditions, selected_case_names)

    actual_device = os.environ.get("LOCAL_AGENT_ACTUAL_DEVICE") or None
    actual_device_source = "operator_declared" if actual_device else "unobserved"
    instrument_declared = json.loads((REPO / "INSTRUMENT.json").read_text(encoding="utf-8"))

    return {
        "schema_version": 1,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "conditions": conditions,
        "cases": sorted(selected_case_names) if selected_case_names else [c.name for c in CASES],
        "host": host_identity(),
        "toolchain": toolchain_identity(),
        "target": {
            "requested_device": model.device_note,
            "actual_device": actual_device,
            "actual_device_source": actual_device_source,
        },
        "model_configuration": {
            **model.identity(),
            "endpoint": model.base_url,
            "architecture": model_architecture(model.model),
        },
        "measurement_quality": {
            "host": "observed_from_python_runtime",
            "toolchain": "observed_from_command_probes_and_disposable_cmake_configure",
            "requested_device": "declared_by_profile",
            "actual_device": actual_device_source,
            "model_architecture": model_architecture(model.model).get("metadata_quality"),
            "tool_schemas": "exact_serialized_schemas_archived_once_per_hash",
            "transcripts": "archived_by_evaluation_runner",
        },
        "instrument": {
            "declared": {
                "generation": instrument_declared.get("generation"),
                "source_sha256": instrument_declared.get("source_sha256"),
                "base_prompt_sha256": instrument_declared.get("base_prompt_sha256"),
            },
            "observed": package_identity(),
        },
        "tool_schema_archive": schemas,
        "case_tool_schemas": case_schemas,
    }


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, choices=sorted(MODEL_PRESETS))
    parser.add_argument(
        "--condition",
        action="append",
        choices=["control", "narrow", "skill"],
        help="repeat to capture a subset; default is all three conditions",
    )
    parser.add_argument("--case", action="append", help="repeat to capture selected cases")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    conditions = args.condition or ["control", "narrow", "skill"]
    payload = capture_manifest(
        args.profile,
        conditions,
        set(args.case) if args.case else None,
    )
    out = Path(args.out)
    write_atomic(out, payload)
    print(f"wrote immutable run manifest: {out}")
    print(f"profile: {args.profile}")
    print(f"host: {payload['host']['system']} {payload['host']['machine']}")
    print(f"requested device: {payload['target']['requested_device']}")
    print(f"actual device: {payload['target']['actual_device'] or 'UNOBSERVED'}")
    print(f"distinct tool schemas: {len(payload['tool_schema_archive'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
