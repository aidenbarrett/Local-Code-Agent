import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[3]
INTERNAL = REPO / "internal"
DEMO = INTERNAL / "scripts" / "demo-trust-boundary.py"


@pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("ctest") is None,
    reason="verification demo requires the CMake/CTest toolchain",
)
def test_verification_demo_proves_a_real_stale_pass(tmp_path):
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(INTERNAL) if not existing else os.pathsep.join((str(INTERNAL), existing))

    result = subprocess.run(
        [sys.executable, str(DEMO), "--workdir", str(tmp_path), "--keep"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert output.count("100% tests passed") >= 2, output
    assert "Independent verification accepted the passing result" in output
    assert "The tests passed, but they executed the OLD compiled program" in output
    assert "Independent verification REFUSED the passing test result" in output
    assert "STALE TEST RESULT REJECTED" in output
