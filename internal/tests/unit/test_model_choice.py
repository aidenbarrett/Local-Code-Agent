from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from local_agent.config import DEFAULT_MODEL_PRESET, MODEL_PRESETS
from serving import model_choice
from serving.model_choice import (
    ModelChoiceError,
    choice_path,
    local_presets,
    resolve_preset,
    store_preset,
)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_default_is_a_real_local_preset():
    assert DEFAULT_MODEL_PRESET in MODEL_PRESETS
    assert DEFAULT_MODEL_PRESET in local_presets()


def test_resolution_order_is_explicit_then_stored_then_default(tmp_path):
    assert resolve_preset(None, tmp_path) == model_choice.ResolvedPreset(DEFAULT_MODEL_PRESET, "default")
    store_preset(tmp_path, "ptl-gpu-30b")
    assert resolve_preset(None, tmp_path) == model_choice.ResolvedPreset("ptl-gpu-30b", "stored")
    assert resolve_preset("ptl-npu-8b", tmp_path) == model_choice.ResolvedPreset("ptl-npu-8b", "explicit")


def test_storing_an_unknown_or_non_local_preset_is_refused_and_writes_nothing(tmp_path):
    for bad in ("no-such-model", "cloud"):
        with pytest.raises(ModelChoiceError):
            store_preset(tmp_path, bad)
    assert not choice_path(tmp_path).exists()


@pytest.mark.parametrize("content", ['{"preset": "gone-model"}', "not json", '["ptl-npu-8b"]'])
def test_a_broken_stored_choice_is_refused_not_silently_replaced(tmp_path, content):
    choice_path(tmp_path).write_text(content, encoding="utf-8")
    with pytest.raises(ModelChoiceError, match="models use"):
        resolve_preset(None, tmp_path)
    # An explicit choice for this run still works while the stored one is broken.
    assert resolve_preset("ptl-gpu-30b", tmp_path).name == "ptl-gpu-30b"


def test_an_unknown_explicit_preset_is_refused(tmp_path):
    with pytest.raises(ModelChoiceError):
        resolve_preset("no-such-model", tmp_path)


def test_models_use_stores_the_choice_and_list_marks_it(tmp_path, capsys):
    weights = _script("model_weights")
    assert weights.main(["use", "ptl-gpu-30b", "--runtime-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "ptl-gpu-30b is now used when no --profile is given" in out
    assert "models pull ptl-gpu-30b" in out  # weights are not downloaded here
    assert json.loads(choice_path(tmp_path).read_text(encoding="utf-8")) == {"preset": "ptl-gpu-30b"}

    assert weights.main(["--runtime-root", str(tmp_path)]) == 0
    listing = capsys.readouterr().out
    rows = [line for line in listing.splitlines() if line[:2] in ("* ", "  ")]
    marked = [line for line in rows if line.startswith("* ")]
    assert len(marked) == 1 and marked[0].split()[1] == "ptl-gpu-30b"
    assert "ptl-gpu-30b is used when no --profile is given (your choice)" in listing


def test_models_use_refuses_unknown_presets(tmp_path, capsys):
    weights = _script("model_weights")
    assert weights.main(["use", "no-such-model", "--runtime-root", str(tmp_path)]) == 2
    assert "unknown model preset" in capsys.readouterr().err
    assert weights.main(["use", "--runtime-root", str(tmp_path)]) == 2


def test_product_surfaces_take_their_default_from_the_one_owner():
    """No product entry point may carry its own default model preset."""
    for name in ("session-hub", "acceptance-journeys"):
        source = (SCRIPTS / f"{name}.py").read_text(encoding="utf-8")
        assert 'default="ptl-' not in source, name
        assert "resolve_preset(" in source, name
