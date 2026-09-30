#!/usr/bin/env python3
"""Show which presets have their model weights downloaded, and download one preset's.

`.\\local-code-agent.ps1 models` lists; `models pull <profile>` downloads. Downloads go
through `serving.serve.pull`, the same owner the setup script uses, into the runtime
model store. Only presets in `MODEL_PRESETS` can be pulled.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

INTERNAL = Path(__file__).resolve().parents[1]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from local_agent.config import MODEL_PRESETS  # noqa: E402
from serving import serve  # noqa: E402
from serving.model_store import default_runtime_root  # noqa: E402

LOCAL_RUNTIMES = ("ovms", "llamacpp")


def _plan(profile: str, runtime_root: Path) -> dict:
    config = MODEL_PRESETS[profile]
    executable = os.environ.get("LCA_OVMS_EXECUTABLE") if config.runtime == "ovms" else None
    return serve.make_plan(profile, config, runtime_root, executable=executable)


def weight_state(profile: str, runtime_root: Path) -> str:
    config = MODEL_PRESETS[profile]
    plan = _plan(profile, runtime_root)
    if config.runtime == "llamacpp":
        return "downloaded" if Path(plan["model_dir"]).is_file() else "missing (GGUF, fetched by hand)"
    return "missing" if serve.missing_payload_files(plan["model_dir"]) else "downloaded"


def list_weights(runtime_root: Path) -> int:
    print(f"Model store: {serve.model_repository(runtime_root)}")
    for name, config in MODEL_PRESETS.items():
        if config.runtime not in LOCAL_RUNTIMES:
            continue
        flag = "  (experimental)" if config.serving_experimental else ""
        print(f"  {name:<20} {config.device:<4} {config.model:<48} "
              f"{weight_state(name, runtime_root)}{flag}")
    print(f"\nDownload one with: {serve.pull_command('<profile>')}")
    return 0


def pull_weights(profile: str, runtime_root: Path, *, allow_experimental: bool) -> int:
    if profile not in MODEL_PRESETS:
        raise serve.Refusal(f"unknown profile {profile}; run .\\local-code-agent.ps1 models")
    config = MODEL_PRESETS[profile]
    if config.runtime == "ovms" and not os.environ.get("LCA_OVMS_EXECUTABLE"):
        raise serve.Refusal("OVMS is not installed under the runtime root; run .\\install.ps1")
    result = serve.pull(_plan(profile, runtime_root), config,
                        allow_experimental=allow_experimental)
    state = weight_state(profile, runtime_root)
    print(f"{profile}: {state} in {result['model_dir']}")
    return 0 if state == "downloaded" else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", default="list", choices=("list", "pull"))
    parser.add_argument("profile", nargs="?")
    parser.add_argument("--allow-experimental", action="store_true")
    parser.add_argument("--runtime-root", type=Path, default=None)
    args = parser.parse_args(argv)
    runtime_root = args.runtime_root or default_runtime_root()
    try:
        if args.action == "list":
            if args.profile:
                raise serve.Refusal("list takes no profile")
            return list_weights(runtime_root)
        if not args.profile:
            raise serve.Refusal("pull needs a profile; run .\\local-code-agent.ps1 models")
        return pull_weights(args.profile, runtime_root,
                            allow_experimental=args.allow_experimental)
    except (serve.Refusal, OSError, subprocess.SubprocessError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
