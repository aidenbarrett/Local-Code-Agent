from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "devtools" / "capture_change_evidence.py"
SPEC = importlib.util.spec_from_file_location("lca_capture_change_evidence", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


def test_record_keeps_destination_parent_and_candidate_distinct(monkeypatch):
    commits = {
        "origin/main": "a" * 40,
        "stack-parent": "b" * 40,
        "HEAD": "c" * 40,
    }
    trees = {sha: str(index) * 40 for index, sha in enumerate(commits.values(), 4)}
    monkeypatch.setattr(capture, "resolve_commit", lambda ref: commits[ref])
    monkeypatch.setattr(capture, "tree_for", lambda sha: trees[sha])

    assert capture.build_record("origin/main", "stack-parent", "HEAD") == {
        "schema": "lca.change-readiness-evidence/2",
        "destination_ref": "origin/main",
        "destination_commit_sha": "a" * 40,
        "destination_tree_sha": "4" * 40,
        "parent_ref": "stack-parent",
        "parent_commit_sha": "b" * 40,
        "parent_tree_sha": "5" * 40,
        "candidate_ref": "HEAD",
        "candidate_commit_sha": "c" * 40,
        "candidate_tree_sha": "6" * 40,
    }


def test_main_fails_closed_without_writing_partial_evidence(monkeypatch, tmp_path):
    out = tmp_path / "evidence.json"

    def fail(*_args):
        raise capture.EvidenceError("cannot resolve")

    monkeypatch.setattr(capture, "build_record", fail)
    assert capture.main([
        "--destination", "main", "--parent", "parent", "--out", str(out)
    ]) == 2
    assert not out.exists()


def test_main_writes_canonical_v2_json(monkeypatch, tmp_path):
    out = tmp_path / "evidence.json"
    record = {
        "schema": "lca.change-readiness-evidence/2",
        "destination_ref": "main",
        "destination_commit_sha": "a" * 40,
        "destination_tree_sha": "b" * 40,
        "parent_ref": "parent",
        "parent_commit_sha": "c" * 40,
        "parent_tree_sha": "d" * 40,
        "candidate_ref": "HEAD",
        "candidate_commit_sha": "e" * 40,
        "candidate_tree_sha": "f" * 40,
    }
    monkeypatch.setattr(capture, "build_record", lambda *_args: record)
    assert capture.main([
        "--destination", "main", "--parent", "parent", "--out", str(out)
    ]) == 0
    assert out.read_text(encoding="utf-8").endswith("\n")
    assert " " not in out.read_text(encoding="utf-8")


def test_legacy_ambiguous_base_flag_is_rejected(tmp_path):
    out = tmp_path / "evidence.json"
    try:
        capture.main(["--base", "main", "--out", str(out)])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("legacy --base invocation unexpectedly succeeded")
    assert not out.exists()
