from __future__ import annotations

from types import SimpleNamespace

import pytest

from local_agent.session import task_admission


def _controller(repo, *, startup_hash: str):
    return SimpleNamespace(
        repo=repo,
        allow_execution=False,
        context_budget_tokens=12_000,
        source_sha256_at_start=startup_hash,
    )


def test_execution_contract_uses_controller_startup_source_identity(monkeypatch, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    startup = "a" * 64
    controller = _controller(repo, startup_hash=startup)
    monkeypatch.setattr(task_admission, "source_sha256", lambda: startup)

    digest = task_admission.execution_contract_sha256(controller)

    assert len(digest) == 64


def test_checkout_drift_refuses_before_new_execution_contract(monkeypatch, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    controller = _controller(repo, startup_hash="a" * 64)
    monkeypatch.setattr(task_admission, "source_sha256", lambda: "b" * 64)

    with pytest.raises(RuntimeError, match="restart the Session Hub"):
        task_admission.execution_contract_sha256(controller)


def test_unbound_one_shot_controller_keeps_current_hash_semantics(monkeypatch, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    controller = SimpleNamespace(
        repo=repo,
        allow_execution=False,
        context_budget_tokens=12_000,
    )
    monkeypatch.setattr(task_admission, "source_sha256", lambda: "c" * 64)

    digest = task_admission.execution_contract_sha256(controller)

    assert len(digest) == 64
