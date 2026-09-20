from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "devtools" / "capture_ci_checkout.py"
SPEC = importlib.util.spec_from_file_location("lca_capture_ci_checkout", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


def test_record_separates_checkout_from_pr_head_and_base(monkeypatch):
    values = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(capture, "git", lambda *_args: next(values))
    value = capture.record({
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_SHA": "a" * 40,
        "LCA_PR_HEAD_SHA": "c" * 40,
        "LCA_PR_BASE_SHA": "d" * 40,
    })
    assert value["checkout_commit_sha"] == "a" * 40
    assert value["checkout_tree_sha"] == "b" * 40
    assert value["pr_head_sha"] == "c" * 40
    assert value["pr_base_sha"] == "d" * 40


def test_record_refuses_github_sha_mismatch(monkeypatch):
    values = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(capture, "git", lambda *_args: next(values))
    with pytest.raises(RuntimeError, match="does not match"):
        capture.record({"GITHUB_SHA": "c" * 40})
