from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "devtools" / "check_change.py"
SPEC = importlib.util.spec_from_file_location("lca_check_change", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
check_change = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_change)


def _valid(monkeypatch):
    shas = {
        "destination": "a" * 40,
        "parent": "b" * 40,
        "trusted": "a" * 40,
        "HEAD": "c" * 40,
    }
    monkeypatch.setattr(check_change, "require_clean", lambda: None)
    monkeypatch.setattr(check_change, "resolve", lambda ref: shas[ref])
    monkeypatch.setattr(check_change, "tree_for", lambda _sha: "d" * 40)
    monkeypatch.setattr(check_change, "is_ancestor", lambda *_args: True)
    monkeypatch.setattr(check_change, "changed_paths", lambda *_args: [])
    monkeypatch.setattr(check_change, "files_at", lambda _sha: [])
    monkeypatch.setattr(check_change, "stale_imports", lambda *_args: [])
    declaration = {
        "source_sha256": "1" * 64,
        "base_prompt_sha256": "2" * 64,
        "outcome_contract_sha256": "3" * 64,
    }
    monkeypatch.setattr(check_change, "read_json_at", lambda *_args: dict(declaration))
    monkeypatch.setattr(check_change, "verify_generated_output", lambda _sha: None)
    monkeypatch.setattr(check_change, "load_acceptance_evidence", lambda *_args: None)
    return shas


def _check(tmp_path):
    return check_change.check_candidate(
        parent_ref="parent",
        destination_ref="destination",
        trusted_baseline_ref="trusted",
        head_ref="HEAD",
        evidence_path=tmp_path / "outside.json",
        scope="local",
    )


def test_valid_fixture_passes_only_local_scope(monkeypatch, tmp_path):
    _valid(monkeypatch)
    result = _check(tmp_path)
    assert result["scope"] == "local"
    assert result["head"] == "c" * 40


def test_wrong_parent_is_not_ready(monkeypatch, tmp_path):
    shas = _valid(monkeypatch)
    monkeypatch.setattr(
        check_change,
        "is_ancestor",
        lambda base, head: not (base == shas["parent"] and head == shas["HEAD"]),
    )
    with pytest.raises(check_change.ReadinessError, match="declared stack parent"):
        _check(tmp_path)


def test_feature_only_closure_is_not_destination_ready(monkeypatch, tmp_path):
    shas = _valid(monkeypatch)
    monkeypatch.setattr(
        check_change,
        "is_ancestor",
        lambda base, head: not (
            base == shas["destination"] and head == shas["parent"]
        ),
    )
    with pytest.raises(check_change.ReadinessError, match="eventual destination"):
        _check(tmp_path)


def test_dirty_candidate_is_not_ready(monkeypatch, tmp_path):
    _valid(monkeypatch)
    monkeypatch.setattr(
        check_change,
        "require_clean",
        lambda: (_ for _ in ()).throw(check_change.ReadinessError("dirty")),
    )
    with pytest.raises(check_change.ReadinessError, match="dirty"):
        _check(tmp_path)


def test_candidate_changing_mid_check_is_not_ready(monkeypatch, tmp_path):
    shas = _valid(monkeypatch)
    head_values = iter([shas["HEAD"], "e" * 40])

    def resolve(ref):
        if ref == "HEAD":
            return next(head_values)
        return shas[ref]

    monkeypatch.setattr(check_change, "resolve", resolve)
    with pytest.raises(check_change.ReadinessError, match="changed during"):
        _check(tmp_path)


def test_frozen_artifact_edit_is_not_ready(monkeypatch, tmp_path):
    _valid(monkeypatch)
    monkeypatch.setattr(
        check_change,
        "changed_paths",
        lambda *_args: ["internal/experiments/generation-1/raw.json"],
    )
    with pytest.raises(check_change.ReadinessError, match="frozen artifacts"):
        _check(tmp_path)


def test_stale_import_is_not_ready(monkeypatch, tmp_path):
    _valid(monkeypatch)
    monkeypatch.setattr(
        check_change,
        "stale_imports",
        lambda *_args: ["internal/scripts/example.py:4: local_agent.tools.testing"],
    )
    with pytest.raises(check_change.ReadinessError, match="retired imports"):
        _check(tmp_path)


def test_literal_dynamic_retired_import_is_detected(monkeypatch):
    monkeypatch.setattr(
        check_change,
        "files_at",
        lambda _sha: ["internal/local_agent/example.py"],
    )
    monkeypatch.setattr(
        check_change,
        "read_text_at",
        lambda *_args: "importlib.import_module('local_agent.tools.testing')\n",
    )
    assert check_change.stale_imports("a" * 40) == [
        "internal/local_agent/example.py:1: local_agent.tools.testing"
    ]


def test_missing_acceptance_evidence_is_not_ready(monkeypatch, tmp_path):
    _valid(monkeypatch)
    monkeypatch.setattr(
        check_change,
        "load_acceptance_evidence",
        lambda *_args: (_ for _ in ()).throw(
            check_change.ReadinessError("acceptance evidence unavailable")
        ),
    )
    with pytest.raises(check_change.ReadinessError, match="evidence unavailable"):
        _check(tmp_path)


def test_candidate_cannot_self_edit_trusted_baseline(monkeypatch, tmp_path):
    shas = _valid(monkeypatch)

    def declaration(commit, _path):
        value = {
            "source_sha256": "1" * 64,
            "base_prompt_sha256": "2" * 64,
            "outcome_contract_sha256": "3" * 64,
        }
        if commit == shas["HEAD"]:
            value["base_prompt_sha256"] = "9" * 64
        return value

    monkeypatch.setattr(check_change, "read_json_at", declaration)
    with pytest.raises(check_change.ReadinessError, match="trusted baseline"):
        _check(tmp_path)


def test_unavailable_generated_output_check_is_not_ready(monkeypatch, tmp_path):
    _valid(monkeypatch)
    monkeypatch.setattr(
        check_change,
        "verify_generated_output",
        lambda _sha: (_ for _ in ()).throw(
            check_change.ReadinessError("fixture generation unavailable")
        ),
    )
    with pytest.raises(check_change.ReadinessError, match="generation unavailable"):
        _check(tmp_path)


def test_pr_scope_never_claims_ready_without_ci_consumer(monkeypatch, tmp_path):
    _valid(monkeypatch)
    with pytest.raises(check_change.ReadinessError, match="trusted CI evidence"):
        check_change.check_candidate(
            parent_ref="parent",
            destination_ref="destination",
            trusted_baseline_ref="trusted",
            head_ref="HEAD",
            evidence_path=tmp_path / "evidence.json",
            scope="pr",
        )


def test_acceptance_evidence_requires_every_local_gate(tmp_path, monkeypatch):
    outside = tmp_path / "evidence.json"
    monkeypatch.setattr(check_change, "ROOT", tmp_path / "candidate")
    outside.write_text(
        """{
  "schema": "lca.change-acceptance/1",
  "destination_commit_sha": "a",
  "parent_commit_sha": "b",
  "candidate_commit_sha": "c",
  "candidate_tree_sha": "d",
  "checks": [{"id": "native-pytest", "status": "passed"}]
}
""",
        encoding="utf-8",
    )
    expected = {
        "destination_commit_sha": "a",
        "parent_commit_sha": "b",
        "candidate_commit_sha": "c",
        "candidate_tree_sha": "d",
    }
    with pytest.raises(check_change.ReadinessError, match="compatibility-runner"):
        check_change.load_acceptance_evidence(outside, expected)
