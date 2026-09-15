import json
import sys

import pytest
from measurement import contention


def _python_sleep(seconds):
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_command_json_is_explicit_and_fail_closed():
    assert contention.parse_command_json('["python", "-V"]') == ["python", "-V"]
    for bad in ("not-json", "{}", "[]", '["python", ""]'):
        with pytest.raises(ValueError):
            contention.parse_command_json(bad)


def test_compile_wall_time_is_measured_while_agent_overlaps():
    result = contention.run_contention(
        _python_sleep(0.2),
        _python_sleep(0.05),
        settle_seconds=0.01,
        agent_timeout_seconds=2,
    )
    assert result["measurement_quality"] == "observed"
    assert result["agent_exit_code"] == 0
    assert result["compile_exit_code"] == 0
    assert result["compile_wall_seconds"] > 0
    assert result["overlap_seconds"] > 0


def test_cli_writes_immutable_privacy_safe_sidecar(tmp_path):
    out = tmp_path / "contention.json"
    agent = json.dumps(_python_sleep(0.1))
    compile_command = json.dumps(_python_sleep(0.02))
    assert contention.main([
        "--out",
        str(out),
        "--agent-command-json",
        agent,
        "--compile-command-json",
        compile_command,
        "--agent-timeout-seconds",
        "2",
    ]) == 0
    data = json.loads(out.read_text())
    assert data["kind"] == "compile_contention_completion"
    assert data["measurement_quality"] == "observed"
    with pytest.raises(FileExistsError):
        contention.main([
            "--out",
            str(out),
            "--agent-command-json",
            agent,
            "--compile-command-json",
            compile_command,
        ])
