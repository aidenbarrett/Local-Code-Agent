from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_one_shot_keeps_ovms_pythonhome_out_of_controller_venv():
    text = (ROOT / "scripts" / "work-laptop-one-shot.ps1").read_text(encoding="utf-8")
    setup = text.index('. $setupVars.FullName')
    capture = text.index('$env:LCA_OVMS_PYTHONHOME = $env:PYTHONHOME')
    restore = text.index('RestoreEnvVariable "PYTHONHOME"')
    controller = text.index('& $VenvPython -c "import psutil"')
    assert setup < capture < restore < controller
    assert '$env:LCA_OVMS_PYTHONPATH = $env:PYTHONPATH' in text
    assert 'RestoreEnvVariable "PYTHONPATH"' in text


def test_controller_forwards_saved_ovms_python_environment_to_runtime_children():
    text = (ROOT / "measurement" / "serve.py").read_text(encoding="utf-8")
    assert '("LCA_OVMS_PYTHONHOME", "PYTHONHOME")' in text
    assert '("LCA_OVMS_PYTHONPATH", "PYTHONPATH")' in text
    assert 'env=runtime_process_env(plan)' in text
