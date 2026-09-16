from local_agent.config import MODEL_PRESETS


def test_panther_lake_npu_profiles_disable_thinking_explicitly():
    """Cheap-tier NPU runs must not inherit the server's reasoning default."""
    for profile in ("ptl-npu-8b", "ptl-npu-8b-16k"):
        cfg = MODEL_PRESETS[profile]
        assert cfg.tier == "cheap"
        assert cfg.thinking is False
