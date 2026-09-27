"""A real model drives the code-changing journey through the public object graph.

Every other code-journey test scripts the model. This one points the product at a
real OpenAI-compatible endpoint (CI: the native endpoint running a small Qwen3 on
CPU) and runs what a user would type on the broken C++ fixture:

    build it  ->  fix it  ->  /apply <task>

It does not assert that the model succeeds. A small CPU model may not fix the
build, and that is a result worth recording. It asserts that the product tells the
truth about whatever happened:

* the user's checkout is never written before `/apply`;
* a PASS is real: after `/apply`, an independent CMake build of the checkout,
  run here and not by the agent, succeeds;
* anything that is not a PASS carries no success claim and a declared reason.

The outcome is written to ``LCA_REAL_MODEL_REPORT`` (JSON) when set, and printed
as a GitHub notice, so every run leaves a measured answer to "can it code yet".

Configuration: ``LCA_REAL_MODEL_BASE_URL`` (OpenAI-compatible, ending in /v1) and
``LCA_REAL_MODEL`` (served model id). Without them the module is skipped, except
when ``LCA_REQUIRE_REAL_MODEL=1``, where missing configuration is a failure.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from local_agent.config import MODEL_PRESETS, load_repo_config
from local_agent.llm.client import build_client
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.contracts import TaskOutcome
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.workspaces import GitWorkspaceManager

RING = "src/ring_buffer.cpp"
# The product profile whose tool-call format (hermes) and thinking setting match
# what the native endpoint renders; only base_url and model are overridden.
PROFILE = "ptl-npu-8b"
# Reasons a non-passing result may carry; anything else is an undeclared outcome.
DECLARED_FAILURE_REASONS = frozenset({
    "verification_failed", "missing_evidence", "invalid_input", "policy_denied",
    "unavailable_capability", "missing_dependency", "endpoint_unavailable",
    "inference_timeout", "tool_timeout", "cleanup_unknown", "controller_fault",
})


def _model_config():
    base_url = os.environ.get("LCA_REAL_MODEL_BASE_URL", "")
    model = os.environ.get("LCA_REAL_MODEL", "")
    if not (base_url and model):
        if os.environ.get("LCA_REQUIRE_REAL_MODEL") == "1":
            pytest.fail("LCA_REQUIRE_REAL_MODEL=1 but LCA_REAL_MODEL_BASE_URL/LCA_REAL_MODEL are unset")
        pytest.skip("no real model endpoint configured")
    return replace(MODEL_PRESETS[PROFILE], base_url=base_url, model=model)


def _independent_build(root: Path) -> subprocess.CompletedProcess[str]:
    """Build the checkout with CMake directly: the agent's own verdict is not used."""
    build = root / "build-independent"
    shutil.rmtree(build, ignore_errors=True)
    configure = subprocess.run(
        ["cmake", "-S", ".", "-B", str(build), "-DCMAKE_BUILD_TYPE=Debug"],
        cwd=root, capture_output=True, text=True, check=False, timeout=600,
    )
    if configure.returncode != 0:
        return configure
    return subprocess.run(
        ["cmake", "--build", str(build), "--parallel", "4"],
        cwd=root, capture_output=True, text=True, check=False, timeout=600,
    )


def _step(result, started: float) -> dict[str, object]:
    return {
        "outcome": result.outcome.value,
        "reason_code": result.reason_code,
        "verified": result.verified_at_completion,
        "tool_calls": len(result.evidence_ids),
        "seconds": round(time.monotonic() - started, 1),
    }


def _assert_truthful(result) -> None:
    if result.outcome.succeeded:
        assert result.verified_at_completion is True, result.answer
    else:
        assert result.verified_at_completion is False, result.answer
        assert result.reason_code in DECLARED_FAILURE_REASONS, (result.reason_code, result.answer)


def test_a_real_model_fixes_the_build_or_says_truthfully_that_it_did_not(sandbox, tmp_path):
    config = _model_config()
    sandbox.scenario("compile_error")
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qam", "broken"],
                   cwd=sandbox.root, check=True)
    broken = (sandbox.root / RING).read_bytes()
    assert _independent_build(sandbox.root).returncode != 0, "the fixture must start broken"

    report: dict[str, object] = {"model": config.model, "profile": PROFILE, "scenario": "compile_error"}
    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    try:
        events = EventBuffer(service.stream_id)
        controller = TaskController(
            load_repo_config(sandbox.root), lambda: build_client(config), events,
            allow_execution=True, context_budget_tokens=config.context_budget_tokens,
            workspaces=GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40),
        )
        executor = CancellableDurableTaskExecutor(service, AdmittedDurableTaskController(service, controller))
        history = DurableTaskHistory(service.store, stream_id=service.stream_id)
        gateway = ConversationGateway(
            build_client(config), controller, events,
            task_runner=DurableTaskAdmissionRunner(executor),
            task_history=history,
            route_events=DurableRouteEvents(service),
        )

        started = time.monotonic()
        gateway.turn("build it")
        built = gateway.last_result
        report["build"] = _step(built, started)
        _assert_truthful(built)
        # The fixture does not compile, so a PASS here would be a false claim.
        assert not built.outcome.succeeded, built.answer

        report["applied"] = False
        report["independent_build_after_apply"] = None
        if built.reason_code != "verification_failed":
            # The model never ran the build, so there is no observed failure for
            # "fix it" to name. That is the measured result of this run.
            report["fix"] = {"outcome": None, "reason_code": "build_failure_not_observed"}
            return

        started = time.monotonic()
        gateway.turn("fix it")
        fixed = gateway.last_result
        if fixed is built:
            # "fix it" did not admit a task (for example it asked which failure).
            report["fix"] = {"outcome": None, "reason_code": "fix_not_admitted"}
            return
        report["fix"] = _step(fixed, started)
        _assert_truthful(fixed)
        # Preparing a candidate never writes the user's checkout.
        assert (sandbox.root / RING).read_bytes() == broken

        if fixed.outcome is TaskOutcome.PASS:
            gateway.turn(f"/apply {fixed.task_id}")
            applied = gateway.last_result
            report["apply"] = _step(applied, time.monotonic())
            assert applied.outcome is TaskOutcome.PASS, applied.answer
            report["applied"] = True
            independent = _independent_build(sandbox.root)
            report["independent_build_after_apply"] = independent.returncode == 0
            # The product said the fix builds; a build it did not run must agree.
            assert independent.returncode == 0, independent.stdout[-2000:] + independent.stderr[-2000:]
    finally:
        service.close()
        path = os.environ.get("LCA_REAL_MODEL_REPORT")
        if path:
            Path(path).write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
        fix = report.get("fix", {})
        print(f"::notice title=real-model code journey::{config.model}: "
              f"build={report.get('build', {}).get('outcome')} "
              f"fix={fix.get('outcome')}/{fix.get('reason_code')} "
              f"applied_and_builds={report.get('independent_build_after_apply')}")
