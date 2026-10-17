"""The walkthrough preparation refuses cleanly before doing anything it cannot finish."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo-walkthrough.py"


def _module():
    name = "lca_demo_walkthrough"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def test_without_a_fetched_corpus_it_refuses_and_names_the_fetch(tmp_path, capsys):
    code = _module().main(["--output", str(tmp_path / "walk"),
                           "--corpus-cache", str(tmp_path / "empty-cache")])
    captured = capsys.readouterr()
    assert code == 2
    assert "acceptance --corpus cxxopts --fetch" in captured.err
    assert captured.out == ""
    assert not (tmp_path / "walk").exists()


def test_the_seeded_fault_it_uses_is_a_source_fault_with_repair_paths():
    module = _module()
    entry = module.corpus.load_manifest()[module.CORPUS_NAME]
    fault = entry.fault(module.FAULT_ID)
    assert fault.kind == "source" and fault.repair_paths
