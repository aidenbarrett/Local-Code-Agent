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
