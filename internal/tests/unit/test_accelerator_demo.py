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


def test_human_facing_output_explains_the_hardware_demo_to_a_new_user():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "LOCAL AI HARDWARE DEMO" in source
    assert "Same local Qwen3-8B model. Only the hardware target changes." in source
    assert "Qwen3-8B (INT4)" in source
    assert "OpenVINO Model Server" in source
    assert "MODEL & HARDWARE" in source
    assert "STARTUP" in source
    assert "LIVE INFERENCE" in source
    assert "MODEL SERVER" in source
    assert "RESULT" in source

    assert 'term.request_header(calls, "COLD START")' in source
    assert 'term.request_header(calls, "WARM")' in source
    assert "First token" in source
    assert "Generation speed" in source
    assert "Output length" in source
    assert "One-time runtime warm-up + prompt processing" in source
    assert "Runtime already initialised; prompt processing still happens" in source

    assert "Starting the local model server" in source
    assert "Hardware target confirmed" in source
    assert "[3/3] DEMO COMPLETE" in source
    assert "Average speed" in source
    assert "Best first token" in source
    assert "Live demo observations · not benchmark results." in source

    # Internal plumbing belongs in verbose/developer paths, not the stranger-facing report.
    assert "Ready at" not in source
    assert "Process ID" not in source
    assert "Accelerator Verification Demo" not in source


def test_banner_is_aligned_and_uses_the_shared_product_width():
    assert len(DEMO.LOGO) == 6
    assert len(set(map(len, DEMO.LOGO))) == 1
    assert len(DEMO.LOGO[0]) == 26
    assert DEMO.WIDTH == 72
