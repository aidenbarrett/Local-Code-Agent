from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from local_agent.agent.state import AgentState, ToolCallRecord
from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.proof_binding import ProofScope, binding_from_run, repository_tree_sha256
from local_agent.session.results import verdict_block_from_task_result


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "a.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    _git(tmp_path, "add", "a.cpp")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def _run(root, *, verified: bool, proof: str, epoch: int = 0, current_epoch: int = 0):
    state = AgentState(task="Build it.", repo_root=root)
    state.verified = verified
    state.verification_attempted = True
    state.mutation_epoch = current_epoch
    state.history.append(ToolCallRecord(
        name="build_target",
        arguments={},
        verdict="pass" if verified else "fail",
        ok=verified,
        summary="fixture",
        execution="ok",
        domain="pass" if verified else "fail",
        epoch=epoch,
        proof=proof,
    ))
    return SimpleNamespace(state=state)


def test_tree_digest_moves_when_working_tree_changes(tmp_path):
    root = _repo(tmp_path)
    before = repository_tree_sha256(root)
    (root / "a.cpp").write_text("int main() { return 1; }\n", encoding="utf-8")
    after = repository_tree_sha256(root)
    assert before != after


def test_verified_run_binds_request_full_scope_tree_and_evidence(tmp_path):
    root = _repo(tmp_path)
    run = _run(root, verified=True, proof="full_build_pass")
    binding = binding_from_run("Build it.", run, root)
    assert binding.scope is ProofScope.FULL_BUILD
    assert len(binding.request_sha256) == 64
    assert len(binding.tree_sha256) == 64
    assert binding.evidence_ids == ("build_target:0",)


def test_stale_epoch_cannot_back_verified_product_result(tmp_path):
    root = _repo(tmp_path)
    run = _run(root, verified=True, proof="full_build_pass", epoch=0, current_epoch=1)
    with pytest.raises(RuntimeError, match="without a current full proof"):
        binding_from_run("Build it.", run, root)


def test_durable_verdict_uses_typed_scope_request_and_tree(tmp_path):
    root = _repo(tmp_path)
    run = _run(root, verified=True, proof="full_build_pass")
    binding = binding_from_run("Build it.", run, root)
    result = TaskResult(
        "task-proof",
        TaskOutcome.PASS,
        "Build passed.",
        True,
        ("build_target:0",),
        {"proof_binding": binding.as_dict()},
        verification_ran=True,
        reason_code="verification_passed",
    )
    verdict = verdict_block_from_task_result(result)
    assert verdict.scope == f"full_build; request_sha256={binding.request_sha256}"
    assert verdict.tree_sha256 == binding.tree_sha256
    assert "Proof scope: full_build" in "\n".join(verdict.rendered_lines)


def test_verified_verdict_rejects_targeted_scope_even_with_success_shape(tmp_path):
    root = _repo(tmp_path)
    tree = repository_tree_sha256(root)
    result = TaskResult(
        "task-targeted",
        TaskOutcome.PASS,
        "Target passed.",
        True,
        ("build_target:0",),
        {
            "proof_binding": {
                "request_sha256": "a" * 64,
                "scope": "targeted_build",
                "tree_sha256": tree,
                "evidence_ids": ["build_target:0"],
            }
        },
        verification_ran=True,
        reason_code="verification_passed",
    )
    with pytest.raises(ValueError, match="full current-tree proof scope"):
        verdict_block_from_task_result(result)
