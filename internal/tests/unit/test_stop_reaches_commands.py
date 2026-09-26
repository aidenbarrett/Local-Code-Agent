"""Stop must reach configured commands, not only revoke controller authority.

Before this join the cancellation token lived in the executor's runtime and nothing
handed it to ``run_command``: ``/stop`` fenced the epoch while a build kept running to
its own timeout.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from local_agent.config import load_repo_config
from local_agent.session.cancellation import CancellationToken, StaleExecutionEpoch
from local_agent.session.cancellation_runtime import CancellationRuntime
from local_agent.session.durable_activity import DurableToolActivity, durable_tool_reason
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.tools import build_registry
from local_agent.tools.tool_primitives import BlockedError, Locus, Reason, ToolError


def _toml_list(argv: list[str]) -> str:
    # JSON string escapes are valid TOML basic-string escapes, including Windows paths.
    return "[" + ", ".join(json.dumps(a) for a in argv) + "]"


def _repo(tmp_path: Path, build: list[str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".local-agent.toml").write_text(
        "[repo]\n"
        'name = "stop-fixture"\n'
        'default_profile = "debug"\n'
        "[profiles.debug]\n"
        f"build = {_toml_list(build)}\n"
        "[policy]\n"
        "allow_build = true\n"
        "command_timeout_seconds = 60\n",
        encoding="utf-8",
    )
    return root


def _sleeper(marker: Path) -> list[str]:
    code = (
        "from pathlib import Path; import time; "
        f"Path({str(marker)!r}).write_text('started'); time.sleep(60)"
    )
    return [sys.executable, "-c", code]


def test_command_cancelled_is_a_user_locus_reason_recorded_as_cancelled():
    assert Reason.COMMAND_CANCELLED.locus is Locus.USER
    assert durable_tool_reason(execution="error", reason="command_cancelled") == "cancelled"
    assert durable_tool_reason(execution="blocked", reason="command_cancelled") == "cancelled"


def test_runtime_hands_out_only_the_current_epoch_token():
    runtime = CancellationRuntime()
    task_id = str(uuid4())
    registered = runtime.register_task(task_id, execution_epoch=3)
    assert runtime.token(task_id, 3) is registered
    with pytest.raises(StaleExecutionEpoch):
        runtime.token(task_id, 2)


def test_stop_before_spawn_blocks_the_command_without_running_it(tmp_path):
    marker = tmp_path / "ran.txt"
    root = _repo(tmp_path, _sleeper(marker))
    token = CancellationToken(str(uuid4()), 0)
    token.request()
    registry, _ctx, _store = build_registry(load_repo_config(root), cancellation_probe=token)

    with pytest.raises(BlockedError) as caught:
        registry.get("build_target").handler(profile="debug")

    assert caught.value.reason is Reason.COMMAND_CANCELLED
    assert not marker.exists()


def test_stop_during_build_ends_the_command_promptly(tmp_path):
    marker = tmp_path / "started.txt"
    root = _repo(tmp_path, _sleeper(marker))
    token = CancellationToken(str(uuid4()), 0)
    registry, _ctx, _store = build_registry(load_repo_config(root), cancellation_probe=token)
    errors: list[BaseException] = []

    def build() -> None:
        try:
            registry.get("build_target").handler(profile="debug")
        except BaseException as exc:  # captured for assertion
            errors.append(exc)

    thread = threading.Thread(target=build)
    thread.start()
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists(), "the build command never started"

    stopped_at = time.monotonic()
    token.request()
    thread.join(15)

    assert not thread.is_alive(), "Stop did not reach the running build"
    assert time.monotonic() - stopped_at < 10, "build ran on after Stop"
    assert len(errors) == 1
    assert isinstance(errors[0], ToolError) and not isinstance(errors[0], BlockedError)
    assert errors[0].reason is Reason.COMMAND_CANCELLED
    assert "Stop interrupted this command" in str(errors[0])


def test_no_probe_keeps_existing_command_behaviour(tmp_path):
    root = _repo(tmp_path, [sys.executable, "-c", "print('built')"])
    registry, ctx, _store = build_registry(load_repo_config(root))
    assert ctx.cancellation_probe is None
    result = registry.get("build_target").handler(profile="debug")
    assert result.ok is True


class _Service:
    """Only what DurableToolActivity.from_task needs is exercised through a stub."""


def test_admitted_controller_passes_the_exact_epoch_token_to_the_controller(monkeypatch):
    task_id = str(uuid4())
    runtime = CancellationRuntime()
    token = runtime.register_task(task_id, execution_epoch=5)
    captured: dict = {}

    class Activity:
        execution_epoch = 5

    monkeypatch.setattr(
        DurableToolActivity, "from_task", staticmethod(lambda service, tid: Activity())
    )

    class Controller:
        repo = object()
        allow_execution = True
        context_budget_tokens = 4096

        def resolve_skill(self, name):
            return name

        def run(self, task, **kwargs):
            captured.update(kwargs)
            return "result"

    bridge = AdmittedDurableTaskController(_Service(), Controller())
    bridge.bind_cancellation_tokens(runtime.token)
    assert bridge.run("build it", task_id=task_id) == "result"
    assert captured["cancellation_probe"] is token

    captured.clear()
    bridge.run("check", task_id=task_id, self_check=True)
    assert "cancellation_probe" not in captured


def test_unbound_admitted_controller_does_not_invent_a_probe(monkeypatch):
    class Activity:
        execution_epoch = 0

    monkeypatch.setattr(
        DurableToolActivity, "from_task", staticmethod(lambda service, tid: Activity())
    )
    captured: dict = {}

    class Controller:
        repo = object()
        allow_execution = True
        context_budget_tokens = 4096

        def resolve_skill(self, name):
            return name

        def run(self, task, **kwargs):
            captured.update(kwargs)

    AdmittedDurableTaskController(_Service(), Controller()).run("x", task_id=str(uuid4()))
    assert "cancellation_probe" not in captured


def test_admitted_controller_refuses_a_second_cancellation_runtime():
    class Controller:
        repo = object()
        allow_execution = True
        context_budget_tokens = 4096

        def resolve_skill(self, name):
            return name

        def run(self, task, **kwargs):
            return None

    bridge = AdmittedDurableTaskController(_Service(), Controller())
    first = CancellationRuntime()
    bridge.bind_cancellation_tokens(first.token)
    bridge.bind_cancellation_tokens(first.token)
    with pytest.raises(RuntimeError):
        bridge.bind_cancellation_tokens(CancellationRuntime().token)


def test_public_stop_path_interrupts_a_running_build_end_to_end(tmp_path):
    """Executor Stop -> runtime token -> admitted controller -> ToolContext -> runner."""
    import hashlib

    from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
    from local_agent.session.contracts import TaskOutcome, TaskResult
    from local_agent.session.session_event_service import DurableSessionService
    from local_agent.session.session_store import SQLiteSessionStore

    marker = tmp_path / "started.txt"
    repo = load_repo_config(_repo(tmp_path, _sleeper(marker)))
    tool_errors: list[BaseException] = []
    finished = threading.Event()

    class Controller:
        allow_execution = True
        context_budget_tokens = 4096

        def __init__(self) -> None:
            self.repo = repo

        def resolve_skill(self, name):
            return name

        def run(self, task, *, task_id=None, cancellation_probe=None, **_kwargs):
            registry, _ctx, _store = build_registry(self.repo, cancellation_probe=cancellation_probe)
            try:
                registry.get("build_target").handler(profile="debug")
            except BaseException as exc:
                tool_errors.append(exc)
            finally:
                finished.set()
            return TaskResult(task_id, TaskOutcome.NO_VERDICT, "stopped", False)

    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )
    request = b"request"
    payload = {
        "origin": {
            "kind": "user_direct",
            "turn_ref": {"conversation_id": "conv-stop", "turn_index": 0, "turn_sha256": "a" * 64},
        },
        "request_ref": {
            "artifact_id": str(uuid4()),
            "sha256": hashlib.sha256(request).hexdigest(),
            "media_type": "application/json",
            "size_bytes": len(request),
            "availability": "unavailable",
        },
        "contract_sha256": "b" * 64,
        "repository_id": "repo-1",
        "skill": "inspect",
        "execution_epoch": 0,
        "deadline_utc": "2030-01-01T00:00:00Z",
    }
    try:
        executor = CancellableDurableTaskExecutor(
            service, AdmittedDurableTaskController(service, Controller())
        )
        handle = executor.submit(
            task="build it",
            request_id="stop-e2e",
            payload_sha256="c" * 64,
            admission_payload=payload,
        )
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "build never started under the executor"

        stopped_at = time.monotonic()
        executor.request_cancel(handle.task_id, execution_epoch=0)
        assert finished.wait(15), "Stop never reached the running build"
        assert time.monotonic() - stopped_at < 10
        assert len(tool_errors) == 1
        assert getattr(tool_errors[0], "reason", None) is Reason.COMMAND_CANCELLED
    finally:
        # Let the executor thread finish its fenced terminal reconciliation before the
        # store it writes to is closed.
        for thread in threading.enumerate():
            if thread.name.startswith("lca-task-"):
                thread.join(15)
        service.close()
