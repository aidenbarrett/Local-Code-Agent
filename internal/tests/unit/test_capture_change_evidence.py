from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "devtools" / "capture_change_evidence.py"
SPEC = importlib.util.spec_from_file_location("lca_capture_change_evidence", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


def test_record_keeps_refs_commits_and_trees_distinct(monkeypatch):
    commits = {"main": "a" * 40, "HEAD": "b" * 40}
    trees = {"a" * 40: "c" * 40, "b" * 40: "d" * 40}
    monkeypatch.setattr(capture, "resolve_commit", lambda ref: commits[ref])
    monkeypatch.setattr(capture, "tree_for", lambda sha: trees[sha])

    assert capture.build_record("main", "HEAD") == {
        "schema": "lca.change-readiness-evidence/1",
        "base_ref": "main",
        "base_commit_sha": "a" * 40,
        "base_tree_sha": "c" * 40,
        "head_ref": "HEAD",
        "head_commit_sha": "b" * 40,
        "head_tree_sha": "d" * 40,
    }


def test_main_fails_closed_without_writing_partial_evidence(monkeypatch, tmp_path):
    out = tmp_path / "evidence.json"

    def fail(*_args):
        raise capture.EvidenceError("cannot resolve")

    monkeypatch.setattr(capture, "build_record", fail)
    assert capture.main(["--base", "main", "--out", str(out)]) == 2
    assert not out.exists()


def test_main_writes_canonical_json(monkeypatch, tmp_path):
    out = tmp_path / "evidence.json"
    record = {
        "schema": "lca.change-readiness-evidence/1",
        "base_ref": "main",
        "base_commit_sha": "a" * 40,
        "base_tree_sha": "b" * 40,
        "head_ref": "HEAD",
        "head_commit_sha": "c" * 40,
        "head_tree_sha": "d" * 40,
    }
    monkeypatch.setattr(capture, "build_record", lambda *_args: record)
    assert capture.main(["--base", "main", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").endswith("\n")
    assert " " not in out.read_text(encoding="utf-8")
