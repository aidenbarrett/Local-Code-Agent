"""Direct tests for the detached serving launcher and its argv/process contract."""
from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


INTERNAL = Path(__file__).resolve().parents[2]
LAUNCHER = INTERNAL / "scripts" / "start-ovms-detached.py"

PROBE_HELPERS = r'''
def _stdin_empty():
    try:
        return sys.stdin.read(1) == ""
    except Exception:
        return True
'''
PROBE = r'''
import json, os, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "argv": sys.argv[2:],
    "cwd": os.getcwd(),
    "env_marker": os.environ.get("LCA_TEST_MARKER"),
    "env_unset": "LCA_TEST_REMOVE_ME" in os.environ,
    "same_group": (os.getpgrp() == os.getppid()) if hasattr(os, "getpgrp") else None,
    "own_group_leader": (os.getpgrp() == os.getpid()) if hasattr(os, "getpgrp") else None,
    "stdin_closed": sys.stdin is None or _stdin_empty(),
}, indent=2), encoding="utf-8")
print("probe stdout")
print("probe stderr", file=sys.stderr)
'''


def _launcher():
    spec = spec_from_file_location("start_ovms_detached", LAUNCHER)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe(tmp_path):
    path = tmp_path / "probe.py"
    path.write_text(PROBE_HELPERS + PROBE, encoding="utf-8")
    return path


def _spec(tmp_path, probe, args, *, env=None, unset_env=None, cwd=None, bom=False):
    record = tmp_path / "record.json"
    payload = {
        "exe": sys.executable,
        "args": [str(probe), str(record), *args],
        "stdout": str(tmp_path / "logs" / "out.log"),
        "stderr": str(tmp_path / "logs" / "err.log"),
    }
    if env is not None:
        payload["env"] = env
    if unset_env is not None:
        payload["unset_env"] = unset_env
    if cwd is not None:
        payload["cwd"] = str(cwd)
    spec_path = tmp_path / "launch.json"
    spec_path.write_text(json.dumps(payload, indent=2), encoding="utf-8-sig" if bom else "utf-8")
    return spec_path, record


def _wait(record: Path, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if record.is_file() and record.stat().st_size:
            try:
                return json.loads(record.read_text(encoding="utf-8"))
            except ValueError:
                pass
        time.sleep(0.05)
    raise AssertionError(f"probe never wrote {record}")


AWKWARD_ARGS = [
    '--plugin_config={"NUM_STREAMS":"1","PERFORMANCE_HINT":"LATENCY"}',
    "--model_path=C:\\Program Files\\models\\qwen3-8b-int4",
    "--rest_port=18000",
    "--log_level INFO with spaces",
    '--quote="double"',
    "--single='quoted'",
    "--percent=%PATH%",
    "--dollar=$HOME",
    "--caret=^notescaped^",
    "--empty=",
]


def test_every_argument_arrives_byte_identical(tmp_path, probe):
    launcher = _launcher()
    spec_path, record = _spec(tmp_path, probe, AWKWARD_ARGS)
    launcher.launch(spec_path).wait(timeout=30)
    assert _wait(record)["argv"] == AWKWARD_ARGS


def test_embedded_json_and_bom_survive(tmp_path, probe):
    launcher = _launcher()
    embedded = '--plugin_config={"NUM_STREAMS":"2","CACHE_DIR":"C:\\\\cache"}'
    spec_path, record = _spec(tmp_path, probe, [embedded], bom=True)
    assert spec_path.read_bytes().startswith(b"\xef\xbb\xbf")
    launcher.launch(spec_path).wait(timeout=30)
    arrived = _wait(record)["argv"][0]
    assert arrived == embedded
    assert json.loads(arrived.split("=", 1)[1])["NUM_STREAMS"] == "2"


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX here")
def test_the_child_leads_its_own_process_group(tmp_path, probe):
    launcher = _launcher()
    spec_path, record = _spec(tmp_path, probe, ["--group"])
    launcher.launch(spec_path).wait(timeout=30)
    seen = _wait(record)
    assert seen["own_group_leader"] is True
    assert seen["same_group"] is False


def test_logs_are_created_appended_and_separated(tmp_path, probe):
    launcher = _launcher()
    spec_path, record = _spec(tmp_path, probe, ["--first"])
    launcher.launch(spec_path).wait(timeout=30)
    _wait(record)
    assert "probe stdout" in (tmp_path / "logs" / "out.log").read_text(encoding="utf-8")
    assert "probe stderr" in (tmp_path / "logs" / "err.log").read_text(encoding="utf-8")
    assert "probe stderr" not in (tmp_path / "logs" / "out.log").read_text(encoding="utf-8")
    record.unlink()
    spec_path, record = _spec(tmp_path, probe, ["--second"])
    launcher.launch(spec_path).wait(timeout=30)
    _wait(record)
    assert (tmp_path / "logs" / "out.log").read_text(encoding="utf-8").count("probe stdout") == 2


def test_child_stdin_is_closed(tmp_path, probe):
    launcher = _launcher()
    spec_path, record = _spec(tmp_path, probe, ["--stdin"])
    launcher.launch(spec_path).wait(timeout=30)
    assert _wait(record)["stdin_closed"] is True


def test_env_unset_order_and_cwd_are_honoured(tmp_path, probe, monkeypatch):
    launcher = _launcher()
    monkeypatch.setenv("LCA_TEST_MARKER", "inherited")
    monkeypatch.setenv("LCA_TEST_REMOVE_ME", "inherited")
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    spec_path, record = _spec(
        tmp_path,
        probe,
        ["--env"],
        unset_env=["LCA_TEST_MARKER", "LCA_TEST_REMOVE_ME"],
        env={"LCA_TEST_MARKER": "replaced"},
        cwd=workdir,
    )
    launcher.launch(spec_path).wait(timeout=30)
    seen = _wait(record)
    assert seen["env_marker"] == "replaced"
    assert seen["env_unset"] is False
    assert Path(seen["cwd"]).resolve() == workdir.resolve()


def test_cli_argument_contract_and_pid_output(tmp_path, probe):
    proc = subprocess.run([sys.executable, str(LAUNCHER)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "usage:" in proc.stderr
    proc = subprocess.run([sys.executable, str(LAUNCHER), "a", "b"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2

    spec_path, record = _spec(tmp_path, probe, ["--pid"])
    proc = subprocess.run([sys.executable, str(LAUNCHER), str(spec_path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().isdigit()
    _wait(record)


def test_missing_spec_prints_no_pid():
    proc = subprocess.run([sys.executable, str(LAUNCHER), "no-such-spec.json"], capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""
