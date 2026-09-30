from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from local_agent.config import MODEL_PRESETS
from serving import serve

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "model_weights.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("model_weights_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _complete_payload(directory: Path) -> None:
    directory.mkdir(parents=True)
    for name in ("openvino_model.xml", "openvino_model.bin", "openvino_tokenizer.xml",
                 "openvino_tokenizer.bin", "openvino_detokenizer.xml",
                 "openvino_detokenizer.bin", "config.json"):
        (directory / name).write_text("x", encoding="utf-8")


def _payload_dir(root: Path, profile: str) -> Path:
    return serve.model_repository(root) / MODEL_PRESETS[profile].model


def test_missing_payload_names_the_language_model_and_every_absent_file(tmp_path: Path) -> None:
    assert serve.missing_payload_files(tmp_path / "absent")[0] == "openvino_model.xml"
    _complete_payload(tmp_path / "ok")
    assert serve.missing_payload_files(tmp_path / "ok") == []
    (tmp_path / "ok" / "openvino_model.bin").write_text("", encoding="utf-8")
    assert serve.missing_payload_files(tmp_path / "ok") == ["openvino_model.bin"]


def test_start_refusal_for_absent_weights_names_the_pull_command(tmp_path: Path) -> None:
    config = MODEL_PRESETS["ptl-gpu-30b"]
    plan = serve.make_plan("ptl-gpu-30b", config, tmp_path)
    with pytest.raises(serve.Refusal) as refused:
        serve.preflight(plan, config)
    message = str(refused.value)
    assert "not downloaded" in message
    assert r".\local-code-agent.ps1 models pull ptl-gpu-30b" in message


def test_list_marks_each_local_preset_missing_or_downloaded(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    weights = _load()
    _complete_payload(_payload_dir(tmp_path, "ptl-npu-8b"))
    assert weights.main(["--runtime-root", str(tmp_path)]) == 0
    # Each preset row starts with a two-character selection marker ("* " or "  ").
    rows = {line[2:].split()[0]: line for line in capsys.readouterr().out.splitlines()
            if line[:2] in ("* ", "  ") and line[2:].strip()}
    assert rows["ptl-npu-8b"].startswith("* ")  # the default, with no stored choice
    assert rows["ptl-npu-8b"].rstrip().endswith("downloaded")
    assert "missing" in rows["ptl-gpu-30b"]
    assert "cloud" not in rows
    local = {n for n, c in MODEL_PRESETS.items() if c.runtime in ("ovms", "llamacpp")}
    assert set(rows) == local


def test_pull_refuses_without_ovms_and_never_downloads(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    weights = _load()
    monkeypatch.delenv("LCA_OVMS_EXECUTABLE", raising=False)
    monkeypatch.setattr(serve, "pull", lambda *a, **k: pytest.fail("downloaded"))
    assert weights.main(["pull", "ptl-gpu-30b", "--runtime-root", str(tmp_path)]) == 2
    assert "install.ps1" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["pull"], ["pull", "no-such-profile"], ["list", "ptl-npu-8b"]])
def test_bad_arguments_are_refused(argv: list[str], tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    weights = _load()
    monkeypatch.setenv("LCA_OVMS_EXECUTABLE", "ovms")
    monkeypatch.setattr(serve, "pull", lambda *a, **k: pytest.fail("downloaded"))
    assert weights.main([*argv, "--runtime-root", str(tmp_path)]) == 2


def test_pull_goes_through_the_serving_owner_for_that_preset(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    weights = _load()
    monkeypatch.setenv("LCA_OVMS_EXECUTABLE", "ovms")
    seen: list[tuple[str, str, bool]] = []

    def fake_pull(plan: dict, config: object, *, allow_experimental: bool) -> dict:
        seen.append((plan["profile"], plan["model_dir"], allow_experimental))
        _complete_payload(Path(plan["model_dir"]))
        return {"pulled": MODEL_PRESETS["ptl-gpu-30b"].model, "model_dir": plan["model_dir"]}

    monkeypatch.setattr(serve, "pull", fake_pull)
    assert weights.main(["pull", "ptl-gpu-30b", "--runtime-root", str(tmp_path)]) == 0
    assert seen == [("ptl-gpu-30b", str(_payload_dir(tmp_path, "ptl-gpu-30b")), False)]


def test_pull_that_leaves_weights_incomplete_is_not_success(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    weights = _load()
    monkeypatch.setenv("LCA_OVMS_EXECUTABLE", "ovms")
    monkeypatch.setattr(serve, "pull", lambda plan, config, *, allow_experimental: {
        "pulled": "x", "model_dir": plan["model_dir"]})
    assert weights.main(["pull", "ptl-gpu-30b", "--runtime-root", str(tmp_path)]) == 2
