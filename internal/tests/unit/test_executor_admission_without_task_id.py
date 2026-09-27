"""A durable admission without a task id is refused explicitly, even under `python -O`."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.session_event_service import DurableTaskExecutor

REPO = Path(__file__).resolve().parents[3]


class _Admission:
    task_id = None
    created = True


class _Service:
    def submit_task(self, **_kwargs: object) -> _Admission:
        return _Admission()


class _Controller:
    def run(self, task: str, **_kwargs: object) -> None:
        raise AssertionError("no admitted task id, so nothing may run")


def _submit(executor_type: type[DurableTaskExecutor]) -> None:
    executor = executor_type(_Service(), _Controller())  # type: ignore[arg-type]
    executor.submit(
        task="x", request_id="r", payload_sha256="0" * 64,
        admission_payload={"execution_epoch": 0},
    )


EXECUTORS = pytest.mark.parametrize(
    "executor_type", [DurableTaskExecutor, CancellableDurableTaskExecutor],
)


@EXECUTORS
def test_admission_without_a_task_id_is_refused(executor_type: type[DurableTaskExecutor]) -> None:
    with pytest.raises(RuntimeError, match="no task id"):
        _submit(executor_type)


@EXECUTORS
def test_the_refusal_survives_optimised_python(executor_type: type[DurableTaskExecutor]) -> None:
    # `assert` is stripped by -O; the check must not be.
    code = """
import sys
sys.path.insert(0, "internal")
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.session_event_service import DurableTaskExecutor

class Admission:
    task_id = None
    created = True

class Service:
    def submit_task(self, **kwargs):
        return Admission()

class Controller:
    def run(self, task, **kwargs):
        raise SystemExit("ran without a task id")

try:
    {executor}(Service(), Controller()).submit(
        task="x", request_id="r", payload_sha256="0" * 64,
        admission_payload={"execution_epoch": 0},
    )
except RuntimeError as exc:
    print("refused", exc)
""".replace("{executor}", executor_type.__name__)
    done = subprocess.run([sys.executable, "-O", "-c", code], cwd=REPO,
                          capture_output=True, text=True, check=False, timeout=120)
    assert "refused durable admission returned no task id" in done.stdout, done.stderr
