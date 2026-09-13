#!/usr/bin/env python3
"""Preset-driven serving only. No evaluation imports or scored runs."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_agent.config import MODEL_PRESETS, ModelConfig


class Refusal(RuntimeError):
    pass


def validate(config):
    if not all((config.runtime, config.quant, config.device, config.tier)):
        raise Refusal("preset is missing runtime, quant, device or tier")
    if not 0 < config.context_budget_tokens < config.server_max_prompt_length:
        raise Refusal("context budget must be positive and below server_max_prompt_length")
    if config.runtime not in ("ovms", "llamacpp"):
        raise Refusal(f"{config.runtime} is not a local serving runtime")
    if not re.fullmatch(r"CPU|GPU(?:\.\d+)?|NPU", config.device):
        raise Refusal(f"unsupported explicit device: {config.device}")


def endpoint(config):
    u = urlsplit(config.base_url)
    if u.scheme != "http" or u.hostname not in ("localhost", "127.0.0.1") or not u.port:
        raise Refusal("local serving requires an explicit localhost HTTP port in base_url")
    return "127.0.0.1", u.port


def model_directory(repository, model):
    root = repository / model
    candidates = list(root.rglob("openvino_model.xml")) if root.exists() else []
    if len(candidates) > 1:
        raise Refusal(f"multiple IR versions under {root}; select one with --model-dir")
    return candidates[0].parent if candidates else root


def make_plan(profile, config, runtime_root, *, executable=None, model_dir=None,
              gguf=None, windows=None):
    validate(config)
    host, port = endpoint(config)
    windows = os.name == "nt" if windows is None else windows
    runtime_root = Path(runtime_root).absolute()
    state = runtime_root / "runtime" / profile
    repository = runtime_root / "models"
    payload = Path(model_dir).absolute() if model_dir else model_directory(repository, config.model)
    cache = runtime_root / "cache" / profile
    env = {}
    if config.runtime == "ovms":
        exe = executable or ("ovms.exe" if windows else "ovms")
        # Explicit task selects an in-memory graph from the verified local IR.
        # No implicit HF pull or shared graph.pbtxt edits at start time.
        args = ["--model_path", str(payload), "--model_name", config.model,
                "--task", "text_generation", "--target_device", config.device,
                "--tool_parser", config.tool_parser, "--rest_port", str(port),
                "--rest_bind_address", host, "--port", "0",
                "--cache_dir", str(cache), "--enable_prefix_caching", "true"]
        if config.device == "NPU":
            args += ["--max_prompt_len", str(config.server_max_prompt_length),
                     "--plugin_config", '{"NPUW_LLM_PREFILL_ATTENTION_HINT":"PYRAMID"}']
        else:
            args += ["--cache_size", "2"]
    else:
        exe = executable or ("llama-server.exe" if windows else "llama-server")
        payload = Path(gguf).absolute() if gguf else repository / (config.model + ".gguf")
        args = ["-m", str(payload), "--alias", config.model, "--host", host,
                "--port", str(port), "-c", str(config.server_max_prompt_length),
                "-np", "1", "--jinja"]
        if config.llama_backend == "openvino":
            env = {"GGML_OPENVINO_DEVICE": config.device,
                   "GGML_OPENVINO_STATEFUL_EXECUTION": "0",
                   "GGML_OPENVINO_PREFILL_CHUNK_SIZE": "256"}
            if config.device == "NPU" and config.quant != "Q4_0":
                raise Refusal(f"experimental llama NPU route requires Q4_0, found {config.quant}")
        elif config.device != "CPU":
            raise Refusal("accelerator llama serving requires llama_backend=openvino")
        else:
            args += ["-ngl", "0"]
    return {"profile": profile, "model_configuration": config.identity(),
            "exe": str(exe), "args": args, "argv": [str(exe), *args], "env": env,
            "unset_env": ["GGML_OPENVINO_DEVICE", "GGML_OPENVINO_STATEFUL_EXECUTION",
                          "GGML_OPENVINO_PREFILL_CHUNK_SIZE"],
            "cwd": str(ROOT), "host": host, "port": port,
            "base_url": config.base_url, "model_dir": str(payload),
            "model_repository": str(repository), "cache_dir": str(cache),
            "state_file": str(state / "process.json"), "spec_file": str(state / "launch.json"),
            "stdout": str(state / "stdout.log"), "stderr": str(state / "stderr.log"),
            "experimental": config.serving_experimental,
            "max_prompt_length_basis": ("launch_argument" if config.device == "NPU" or
                config.runtime == "llamacpp" else "declared_model_envelope"),
            "server_observed_device": None, "server_observed_max_prompt_length": None}


def precision(payload):
    """The official artifacts omit mode in JSON; read NNCF metadata in the IR."""
    path = Path(payload) / "openvino_model.xml"
    try:
        root = ET.parse(path).getroot()
        wc = root.find("rt_info/nncf/weight_compression")
        if wc is None:
            return {"mode": "unknown", "source": str(path)}
        values = {node.tag: node.get("value") for node in wc}
        return {"mode": (values.get("mode") or "unknown").upper(),
                "group_size": values.get("group_size"), "ratio": values.get("ratio"),
                "source": str(path)}
    except (OSError, ET.ParseError) as exc:
        raise Refusal(f"cannot read model precision from {path}: {exc}") from exc


def check_precision(payload, device):
    observed = precision(payload)
    if device == "NPU" and (observed["mode"] != "INT4_SYM" or
            observed.get("group_size") not in ("-1", "128") or
            observed.get("ratio") not in ("1", "1.0")):
        raise Refusal(f"NPU requires INT4_SYM, ratio 1, group -1 or 128; found {observed}")
    return observed


def available_devices():
    try:
        import openvino as ov
        core = ov.Core()
        return list(core.available_devices), ov.__version__
    except Exception as exc:
        raise Refusal(f"OpenVINO device discovery failed in this Python environment: {exc}") from exc


def check_device(device, available):
    # GPU is an alias for the first GPU. GPU.1 must match that exact instance.
    if device not in available and not (device == "GPU" and "GPU.0" in available):
        raise Refusal(f"requested {device}; OpenVINO available_devices={available}")


def driver_version():
    if os.name != "nt":
        return {"version": None, "quality": "unobserved", "reason": "Windows inventory only"}
    script = ("Get-CimInstance Win32_PnPSignedDriver | Where-Object {"
              "$_.DeviceName -match 'NPU|AI Boost|Neural'} | "
              "Select-Object DeviceName,DriverVersion | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, timeout=20, check=True)
        data = json.loads(result.stdout)
        if data:
            return {"drivers": data, "quality": "observed"}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"version": None, "quality": "unobserved", "reason": "NPU driver query unavailable"}


def check_disk(path, minimum_gib):
    path = Path(path)
    while not path.exists():
        path = path.parent
    free = shutil.disk_usage(path).free
    if free < minimum_gib * 1024**3:
        raise Refusal(f"{path}: {free / 1024**3:.1f} GiB free; requires {minimum_gib} GiB")
    return free


def check_port(plan):
    with socket.socket() as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((plan["host"], plan["port"]))
        except OSError as exc:
            raise Refusal(f"port {plan['port']} is occupied; no owned healthy server") from exc


def preflight(plan, config, allow_experimental=False):
    if plan["experimental"] and not allow_experimental:
        raise Refusal("unproven preset; inspect dry-run, then explicitly use --allow-experimental")
    payload = Path(plan["model_dir"])
    record = {"warnings": []}
    if config.runtime == "ovms":
        # Do this before even querying the runtime, so a bad NPU artifact is named.
        record["precision"] = check_precision(payload, config.device)
        for name in ("openvino_model.bin", "openvino_tokenizer.xml", "openvino_tokenizer.bin",
                     "openvino_detokenizer.xml", "openvino_detokenizer.bin", "config.json"):
            if not (payload / name).is_file() or (payload / name).stat().st_size == 0:
                raise Refusal(f"incomplete model payload: {payload / name}; run pull first")
        if config.device != "NPU":
            model_config = json.loads((payload / "config.json").read_text(encoding="utf-8"))
            maximum = model_config.get("max_position_embeddings")
            if not isinstance(maximum, int) or maximum < config.server_max_prompt_length:
                raise Refusal(f"model max_position_embeddings={maximum} does not cover declared envelope")
    elif not payload.is_file():
        raise Refusal(f"GGUF missing: {payload}; pass --gguf with the downloaded file")
    if config.runtime == "ovms" or config.llama_backend == "openvino":
        devices, version = available_devices()
        check_device(config.device, devices)
        record.update(available_devices=devices, openvino_python_version=version)
    if config.device == "NPU":
        record["npu_driver"] = driver_version()
        if record["npu_driver"]["quality"] == "unobserved":
            record["warnings"].append("NPU driver version unobserved; driver was not changed")
    record["free_cache_bytes"] = check_disk(plan["cache_dir"], config.minimum_free_disk_gib)
    check_port(plan)
    resolved = shutil.which(plan["exe"])
    if not resolved:
        raise Refusal(f"server executable not found: {plan['exe']}; import setupvars or use --executable")
    plan["exe"] = str(Path(resolved).resolve())
    plan["argv"][0] = plan["exe"]
    try:
        version = subprocess.run([plan["exe"], "--version"], capture_output=True,
                                 text=True, timeout=15, check=True)
        record["server_version_output"] = (version.stdout + version.stderr).strip()[:4000]
    except (OSError, subprocess.SubprocessError):
        record["server_version_output"] = None
        record["warnings"].append("server version unobserved; preset runtime_version is a declaration")
    return record


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


@contextmanager
def profile_lock(plan):
    # OS locks release on a controller crash; no stale lock-file deletion races.
    path = Path(plan["state_file"]).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                fh.write(b"0"); fh.flush(); fh.seek(0)
            lock = lambda: msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: (fh.seek(0), msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            lock = lambda: fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(fh, fcntl.LOCK_UN)
        try:
            lock()
        except OSError as exc:
            raise Refusal("another controller operation owns this profile") from exc
        try:
            yield
        finally:
            unlock()


def read_record(plan):
    path = Path(plan["state_file"])
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refusal(f"unreadable process record {path}; inspect before restarting") from exc


def owned_process(record):
    import psutil
    try:
        process = psutil.Process(record["pid"])
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        if process.create_time() != record["create_time"] or process.exe() != record["process_exe"]:
            raise Refusal("PID now belongs to another process; refusing to adopt or stop it")
        return process
    except psutil.NoSuchProcess:
        return None
    except (KeyError, psutil.AccessDenied) as exc:
        raise Refusal("process ownership could not be established") from exc


def get_json(url):
    # Loopback must not pass through a corporate HTTP proxy.
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
            body = response.read(1024 * 1024)
            try:
                data = json.loads(body) if body else {}
            except ValueError:
                data = {"body": body.decode("utf-8", errors="replace")}
            return response.status, data
    except Exception as exc:
        return None, {"error": str(exc)}


def status(plan):
    record = read_record(plan)
    active = record["plan"] if record else plan
    process = owned_process(record) if record else None
    origin = f"http://{active['host']}:{active['port']}"
    models_code, models = get_json(origin + "/v1/models")
    runtime = active["model_configuration"]["runtime"]
    health_path = "/v2/health/ready" if runtime == "ovms" else "/health"
    health_code, health = get_json(origin + health_path)
    expected = active["model_configuration"]["model"]
    ids = [m.get("id") for m in models.get("data", []) if isinstance(m, dict)] if isinstance(models, dict) else []
    ready = bool(process and health_code == 200 and models_code == 200 and expected in ids)
    return {"profile": plan["profile"], "process_alive": process is not None,
            "healthy": ready, "pid": record["pid"] if record else None,
            "health_http_status": health_code, "health_response": health,
            "models_http_status": models_code, "models": models,
            "resolved_device": active["model_configuration"]["device"] if record else None,
            "resolved_max_prompt_length": active["model_configuration"]["server_max_prompt_length"] if record else None,
            "resolution_basis": active["max_prompt_length_basis"] if record else "not launched",
            "server_observed_device": None, "server_observed_max_prompt_length": None,
            "uptime_seconds": max(0, time.time() - record["create_time"]) if process else None,
            "stdout": active["stdout"], "stderr": active["stderr"],
            "preflight": record.get("preflight") if record else None}


def launch(plan):
    helper = ROOT / "scripts" / "start-ovms-detached.py"
    spec = importlib.util.spec_from_file_location("detached_server", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.launch(Path(plan["spec_file"]))


def start(plan, config, *, allow_experimental=False, wait_seconds=900):
    import psutil
    with profile_lock(plan):
        old = read_record(plan)
        if old and owned_process(old):
            same = all(old["plan"].get(k) == plan.get(k) for k in
                       ("model_configuration", "args", "env", "model_dir"))
            same = same and os.path.normcase(str(Path(shutil.which(plan["exe"]) or plan["exe"]).resolve())) == os.path.normcase(str(Path(old["plan"]["exe"]).resolve()))
            if not same:
                raise Refusal("live profile has different configuration; stop it explicitly first")
            state = status(plan)
            if state["healthy"]:
                return state
            raise Refusal(f"owned PID is live but unhealthy; inspect {plan['stdout']} and {plan['stderr']}")
        evidence = preflight(plan, config, allow_experimental)
        write_json(plan["spec_file"], plan)
        proc = launch(plan)
        try:
            tracked = psutil.Process(proc.pid)
            record = {"pid": proc.pid, "create_time": tracked.create_time(),
                      "process_exe": tracked.exe(), "plan": plan, "preflight": evidence}
            write_json(plan["state_file"], record)
        except Exception as exc:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                proc.kill()
            raise Refusal(f"could not record child process ownership: {exc}") from exc
    deadline = time.monotonic() + wait_seconds
    while True:
        state = status(plan)
        if state["healthy"]:
            return state
        if not state["process_alive"] or time.monotonic() >= deadline:
            raise Refusal(f"server not ready; inspect {plan['stdout']} and {plan['stderr']}; live process retained")
        time.sleep(0.25)


def stop(plan, grace_seconds=10):
    import psutil
    with profile_lock(plan):
        record = read_record(plan)
        process = owned_process(record) if record else None
        forced = False
        if process:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
            except (OSError, psutil.Error):
                pass
            try:
                process.wait(timeout=grace_seconds)
            except psutil.TimeoutExpired:
                # psutil checks creation time again when sending a kill.
                process.kill(); process.wait(timeout=10); forced = True
        Path(plan["state_file"]).unlink(missing_ok=True)
        return {"profile": plan["profile"], "stopped": True, "forced": forced}


def pull(plan, config, *, allow_experimental=False):
    if config.runtime != "ovms":
        raise Refusal("download your chosen GGUF explicitly, then pass --gguf")
    if plan["experimental"] and not allow_experimental:
        raise Refusal("unproven profile; --allow-experimental required for pull")
    check_disk(plan["model_repository"], config.minimum_pull_disk_gib)
    # Pull is download/configuration only, never a serving/compile operation.
    argv = [plan["exe"], "--pull", "--source_model", config.model,
            "--model_repository_path", plan["model_repository"], "--target_device", config.device,
            "--task", "text_generation", "--tool_parser", config.tool_parser]
    with profile_lock(plan):
        subprocess.run(argv, check=True)
    return {"pulled": config.model, "model_dir": str(model_directory(Path(plan["model_repository"]), config.model))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "stop", "logs", "pull"))
    select = parser.add_mutually_exclusive_group(required=True)
    select.add_argument("--profile", choices=MODEL_PRESETS)
    select.add_argument("--all", action="store_true")
    parser.add_argument("--runtime-root", type=Path, default=Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LocalCodeAgent")
    parser.add_argument("--device")
    parser.add_argument("--executable")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--gguf", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-experimental", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=900)
    parser.add_argument("--tail", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        if args.dry_run and args.command != "start":
            raise Refusal("--dry-run previews start only")
        if args.all and args.command != "status":
            raise Refusal("--all is supported only for status")
        if args.tail < 0 or args.wait_seconds < 0:
            raise Refusal("tail and wait-seconds must be nonnegative")
        profiles = [n for n, c in MODEL_PRESETS.items() if c.runtime in ("ovms", "llamacpp")] if args.all else [args.profile]
        output = []
        for name in profiles:
            config = MODEL_PRESETS[name]
            if args.device:
                config = replace(config, device=args.device, device_note=f"override: {args.device}")
            plan = make_plan(name, config, args.runtime_root, executable=args.executable,
                             model_dir=args.model_dir, gguf=args.gguf)
            if args.dry_run:
                output.append({**plan, "preflight": "deferred; no hardware queried or process started"})
            elif args.command == "start":
                output.append(start(plan, config, allow_experimental=args.allow_experimental, wait_seconds=args.wait_seconds))
            elif args.command == "status":
                output.append(status(plan))
            elif args.command == "stop":
                output.append(stop(plan))
            elif args.command == "pull":
                output.append(pull(plan, config, allow_experimental=args.allow_experimental))
            else:
                for stream in ("stdout", "stderr"):
                    path = Path(plan[stream])
                    print(f"{stream}: {path}")
                    if path.exists():
                        with path.open(encoding="utf-8", errors="replace") as fh:
                            print("".join(deque(fh, maxlen=args.tail)), end="")
                return 0
        print(json.dumps(output if args.all else output[0], indent=2))
        return 0
    except (Refusal, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
