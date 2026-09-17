from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from measurement import serve


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "demo-accelerator.py"
SPEC = spec_from_file_location("accelerator_demo", SCRIPT)
assert SPEC and SPEC.loader
DEMO = module_from_spec(SPEC)
SPEC.loader.exec_module(DEMO)


def test_demo_uses_same_model_and_changes_only_explicit_device(tmp_path):
    configs = {device: DEMO.demo_config(device) for device in DEMO.DEVICES}
    models = {cfg.model for cfg in configs.values()}
    assert len(models) == 1
    assert models == {"OpenVINO/Qwen3-8B-int4-cw-ov"}
    assert {cfg.device for cfg in configs.values()} == {"CPU", "GPU", "NPU"}
    assert all(cfg.runtime == "ovms" for cfg in configs.values())
    assert all(cfg.thinking is False for cfg in configs.values())

    for device, cfg in configs.items():
        plan = serve.make_plan(DEMO.DEMO_PROFILE, cfg, tmp_path, windows=True)
        target = plan["args"][plan["args"].index("--target_device") + 1]
        assert target == device


def test_demo_rejects_unknown_device():
    try:
        DEMO.demo_config("TPU")
    except ValueError as exc:
        assert "CPU, GPU, NPU" in str(exc)
    else:
        raise AssertionError("unknown accelerator must fail closed")


def test_wrappers_offer_same_demo_entrypoint():
    ps1 = (ROOT / "scripts" / "demo-accelerator.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "scripts" / "demo-accelerator.sh").read_text(encoding="utf-8")
    assert "demo-accelerator.py" in ps1
    assert '"CPU", "GPU", "NPU", "ALL"' in ps1
    assert "demo-accelerator.py" in sh


def test_windows_wrapper_uses_managed_ovms_runtime():
    ps1 = (ROOT / "scripts" / "demo-accelerator.ps1").read_text(encoding="utf-8")
    assert "ovms-2026.3.0" in ps1
    assert "setupvars.ps1" in ps1
    assert "LCA_OVMS_PYTHONHOME" in ps1
    assert "LCA_OVMS_PYTHONPATH" in ps1
    assert "--runtime-root" in ps1
    assert "--executable" in ps1
    assert "$OvmsExe.FullName" in ps1


def test_windows_wrapper_can_leave_server_running_for_chat():
    ps1 = (ROOT / "scripts" / "demo-accelerator.ps1").read_text(encoding="utf-8")
    assert "[switch]$KeepServer" in ps1
    assert 'if ($KeepServer) { $DemoArgs += "--keep-server" }' in ps1


def test_human_facing_output_is_a_readable_terminal_report():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "LOCAL CODE AGENT" in source
    assert "Accelerator Verification Demo" not in source
    assert "Qwen3-8B (INT4)" in source
    assert "OpenVINO Model Server" in source
    assert "MODEL SETUP" in source
    assert "STARTUP" in source
    assert "INFERENCE RUN" in source
    assert "SERVER" in source
    assert "SUMMARY" in source

    assert "Request 1 | COLD START" in source
    assert "| WARM" in source
    assert "First token" in source
    assert "Generation speed" in source
    assert "Output length" in source
    assert "One-time runtime warm-up + prompt processing" in source
    assert "Runtime already initialised; prompt processing still happens" in source

    assert "Starting validated model server..." in source
    assert "RESULT          PASS" in source
    assert "Average speed" in source
    assert "Best first token" in source
    assert "live demo observations, not benchmark results" in source

    assert "Previous server   stopped (PID" not in source
    assert "decode={rate}" not in source
    assert "completion={stats.completion_tokens}" not in source
    assert "visible={useful}" not in source
    assert "call {calls:02d}" not in source


def test_banner_is_compact_and_balanced():
    assert len(DEMO.LOGO) == 4
    assert max(len(line) for line in DEMO.LOGO) <= 24
    assert DEMO.WIDTH == 60
