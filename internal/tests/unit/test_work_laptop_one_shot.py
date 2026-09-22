from pathlib import Path


INTERNAL = Path(__file__).resolve().parents[2]


def test_one_shot_keeps_ovms_pythonhome_out_of_controller_venv():
    text = (INTERNAL / "work-laptop-one-shot.ps1").read_text(encoding="utf-8")
    setup = text.index('. $setupVars.FullName')
    capture = text.index('$env:LCA_OVMS_PYTHONHOME = $env:PYTHONHOME')
    restore = text.index('RestoreEnvVariable "PYTHONHOME"')
    controller = text.index('& $VenvPython -c "import psutil"')
    assert setup < capture < restore < controller
    assert '$env:LCA_OVMS_PYTHONPATH = $env:PYTHONPATH' in text
    assert 'RestoreEnvVariable "PYTHONPATH"' in text


def test_controller_forwards_saved_ovms_python_environment_to_runtime_children():
    text = (INTERNAL / "measurement" / "serve.py").read_text(encoding="utf-8")
    assert '("LCA_OVMS_PYTHONHOME", "PYTHONHOME")' in text
    assert '("LCA_OVMS_PYTHONPATH", "PYTHONPATH")' in text
    assert 'env=runtime_process_env(plan)' in text


def test_one_shot_uses_relocated_controller_qualification_and_fixture():
    text = (INTERNAL / "work-laptop-one-shot.ps1").read_text(encoding="utf-8")
    assert '$Controller = Join-Path $PSScriptRoot "measurement\\serve.py"' in text
    assert '$QualificationScript = Join-Path $PSScriptRoot "measurement\\qualify_server.py"' in text
    assert '$fixtureSource = Join-Path $PSScriptRoot "benchmark_fixture\\cpp_project"' in text


def test_windows_fixture_smoke_uses_explicit_debug_configuration():
    text = (INTERNAL / "work-laptop-one-shot.ps1").read_text(encoding="utf-8")
    assert "cmake --build build --config Debug --parallel 4" in text
    assert "ctest --test-dir build -C Debug --output-on-failure" in text


def test_workstation_summary_explains_what_passed_without_experiment_jargon():
    text = (INTERNAL / "work-laptop-one-shot.ps1").read_text(encoding="utf-8")
    assert "WORKSTATION VALIDATION" in text
    assert "READY TO USE" in text
    assert "Agent connection" in text
    assert "C++ build/test" in text
    assert "4/4 tests passed" in text
    assert "No experimental evaluation was started automatically." in text
    assert "Example single-arm smoke only" not in text
    assert "Generation 2 run" not in text


def test_one_shot_reconciles_only_a_verified_controller_owned_live_profile():
    text = (INTERNAL / "work-laptop-one-shot.ps1").read_text(encoding="utf-8")

    status = text.index('& $VenvPython $Controller status @serveArgs')
    ownership = text.index('$hadOwnedProcess = [bool]$status.process_alive')
    first_start = text.index('& $VenvPython $Controller start @serveArgs --wait-seconds 900')
    conditional = text.index('if ($startExit -ne 0 -and $hadOwnedProcess)')
    stop = text.index('& $VenvPython $Controller stop @serveArgs')
    retry = text.index(
        '& $VenvPython $Controller start @serveArgs --wait-seconds 900',
        first_start + 1,
    )

    assert status < ownership < first_start < conditional < stop < retry
    assert "Stop-Process" not in text
    assert "taskkill" not in text.lower()


def test_one_shot_does_not_reconcile_when_controller_cannot_establish_ownership():
    text = (INTERNAL / "work-laptop-one-shot.ps1").read_text(encoding="utf-8")

    assert 'if ($statusExit -ne 0)' in text
    assert "could not establish managed server ownership before startup" in text
    assert "could not be proven/stopped as controller-owned" in text
    assert "retrying once" in text
