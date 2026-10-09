"""One owner decides whether a profile's server is reused, replaced or started (#481).

Reuse used to compare only model and device, while ``serve.start`` compared the full
launch. A profile whose server arguments changed was therefore served by the stale
process. Reuse and start now share ``serve.launch_differences``, and the outcome says
which transition happened, with launch-to-ready only for a launch this call made.
"""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from local_agent.config import MODEL_PRESETS
from serving import managed_runtime, serve
from serving.managed_runtime import (
    RefusedReason,
    ReplacedReason,
    RuntimeEnsureResult,
    RuntimeLifecycle,
    RuntimeStep,
)

PROFILE = "ptl-npu-8b"


def _recorded(plan) -> dict:
    """The plan as ``process.json`` stores and returns it."""
    return json.loads(json.dumps(plan))


def test_an_identical_launch_has_no_differences_after_the_record_round_trip(tmp_path):
    plan = serve.make_plan(PROFILE, MODEL_PRESETS[PROFILE], tmp_path, executable="ovms")
    assert serve.launch_differences(_recorded(plan), plan) == ()


def test_client_only_settings_are_not_server_identity(tmp_path):
    config = MODEL_PRESETS[PROFILE]
    old = serve.make_plan(PROFILE, config, tmp_path, executable="ovms")
    new = serve.make_plan(PROFILE, replace(config, temperature=config.temperature + 0.1),
                          tmp_path, executable="ovms")
    assert old["model_configuration"] != new["model_configuration"]
    assert serve.launch_differences(_recorded(old), new) == ()


@pytest.mark.parametrize(("change", "field"), [
    (lambda c: {"server_max_prompt_length": c.server_max_prompt_length // 2}, "args"),
    (lambda c: {"device": "GPU"}, "args"),
    (lambda c: {"model": c.model + "-other"}, "args"),
])
def test_a_server_side_change_is_a_launch_difference(tmp_path, change, field):
    config = MODEL_PRESETS[PROFILE]
    old = serve.make_plan(PROFILE, config, tmp_path, executable="ovms")
    changed = replace(config, **change(config))
    # A smaller prompt envelope must still hold the context budget.
    changed = replace(changed, context_budget_tokens=min(changed.context_budget_tokens,
                                                          changed.server_max_prompt_length // 2))
    new = serve.make_plan(PROFILE, changed, tmp_path, executable="ovms")
    assert field in serve.launch_differences(_recorded(old), new)


def test_a_different_executable_is_a_launch_difference(tmp_path):
    config = MODEL_PRESETS[PROFILE]
    old = serve.make_plan(PROFILE, config, tmp_path, executable="ovms")
    new = serve.make_plan(PROFILE, config, tmp_path, executable=str(tmp_path / "other" / "ovms"))
    assert serve.launch_differences(_recorded(old), new) == ("exe",)
    assert "exe" in serve.launch_differences({}, new), "a record with no executable is not compatible"


_SAME = object()


def _plan(tmp_path):
    return serve.make_plan(PROFILE, MODEL_PRESETS[PROFILE], tmp_path, executable="ovms")


class _Fake:
    """``serve`` stand-ins around a real plan; records what the owner did.

    ``record_plan=_SAME`` records the plan the owner will request.
    """

    def __init__(self, monkeypatch, tmp_path, *, record_plan=None, alive=True, healthy=True,
                 foreign=False, start_state=None):
        self.calls: list[str] = []
        self.root = tmp_path
        if record_plan is _SAME:
            record_plan = _plan(tmp_path)
        record = None if record_plan is None else {"plan": _recorded(record_plan)}
        monkeypatch.setenv("LCA_OVMS_EXECUTABLE", "ovms")
        monkeypatch.setattr(serve, "read_record", lambda plan: record)
        monkeypatch.setattr(serve, "status",
                            lambda plan: {"healthy": healthy and alive, "process_alive": alive})
        monkeypatch.setattr(serve, "stop", lambda plan: self.calls.append("stop"))
        monkeypatch.setattr(managed_runtime, "endpoint_reachable", lambda config: foreign)
        state = start_state or {"healthy": True, "launch_to_ready_ms": 1234}
        monkeypatch.setattr(serve, "start",
                            lambda plan, config, wait_seconds: self.calls.append("start") or state)

    def ensure(self, steps=None):
        return managed_runtime.ensure_managed_runtime(
            PROFILE, MODEL_PRESETS[PROFILE], self.root,
            progress=None if steps is None else steps.append,
        )


def test_an_identical_healthy_server_is_reused_without_a_launch_time(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, tmp_path, record_plan=_SAME)
    result = fake.ensure()
    assert result.lifecycle is RuntimeLifecycle.REUSED and result.ok
    assert result.launch_to_ready_ms is None and result.replaced_reason is None
    assert fake.calls == []


def test_a_healthy_server_with_stale_arguments_is_replaced_not_reused(monkeypatch, tmp_path):
    """The defect: same model and device, different server arguments, silently reused."""
    config = MODEL_PRESETS[PROFILE]
    stale = serve.make_plan(PROFILE, replace(config, server_max_prompt_length=config.server_max_prompt_length * 2),
                            tmp_path, executable="ovms")
    assert stale["model_configuration"]["model"] == config.model
    assert stale["model_configuration"]["device"] == config.device
    fake = _Fake(monkeypatch, tmp_path, record_plan=stale)
    steps: list[RuntimeStep] = []
    result = fake.ensure(steps)
    assert result.lifecycle is RuntimeLifecycle.REPLACED and result.ok
    assert result.replaced_reason is ReplacedReason.CONFIG_CHANGED
    assert result.changed_fields == ("args",)
    assert result.launch_to_ready_ms == 1234
    assert fake.calls == ["stop", "start"]
    assert steps == [RuntimeStep.STOPPING, RuntimeStep.STARTING]


def test_an_unhealthy_identical_server_is_replaced_as_unhealthy(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, tmp_path, record_plan=_SAME, healthy=False)
    result = fake.ensure()
    assert result.replaced_reason is ReplacedReason.UNHEALTHY and result.changed_fields == ()
    assert fake.calls == ["stop", "start"]


def test_a_dead_recorded_server_is_replaced_without_a_stop(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, tmp_path, record_plan=_SAME, alive=False)
    result = fake.ensure()
    assert result.lifecycle is RuntimeLifecycle.REPLACED
    assert result.replaced_reason is ReplacedReason.DEAD
    assert fake.calls == ["start"]


def test_no_record_is_a_fresh_start_with_the_measured_launch_time(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, tmp_path)
    result = fake.ensure()
    assert result.lifecycle is RuntimeLifecycle.STARTED and result.replaced_reason is None
    assert result.launch_to_ready_ms == 1234


def test_a_start_that_reports_no_launch_time_is_not_given_one(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, tmp_path, start_state={"healthy": True})
    assert fake.ensure().launch_to_ready_ms is None


def test_a_foreign_endpoint_is_refused_with_a_typed_reason(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, tmp_path, foreign=True)
    result = fake.ensure()
    assert result.lifecycle is RuntimeLifecycle.REFUSED and not result.ok
    assert result.refused_reason is RefusedReason.FOREIGN_ENDPOINT
    assert fake.calls == []


def test_a_start_that_never_becomes_healthy_is_refused(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, tmp_path, start_state={"healthy": False})
    assert fake.ensure().refused_reason is RefusedReason.NOT_HEALTHY


@pytest.mark.parametrize("kwargs", [
    {"lifecycle": RuntimeLifecycle.REPLACED},
    {"lifecycle": RuntimeLifecycle.STARTED, "replaced_reason": ReplacedReason.DEAD},
    {"lifecycle": RuntimeLifecycle.REUSED, "launch_to_ready_ms": 5},
    {"lifecycle": RuntimeLifecycle.REFUSED},
    {"lifecycle": RuntimeLifecycle.STARTED, "refused_reason": RefusedReason.START_FAILED},
    {"lifecycle": RuntimeLifecycle.REPLACED, "replaced_reason": ReplacedReason.DEAD,
     "changed_fields": ("args",)},
])
def test_an_inconsistent_outcome_cannot_be_constructed(kwargs):
    with pytest.raises(ValueError):
        RuntimeEnsureResult(message="x", **kwargs)


def test_start_records_launch_to_ready_for_the_process_it_launched(monkeypatch, tmp_path):
    config = MODEL_PRESETS[PROFILE]
    plan = serve.make_plan(PROFILE, config, tmp_path, executable="ovms")

    class Proc:
        pid = 4242

    class Tracked:
        def __init__(self, pid):
            pass

        def create_time(self):
            return 99.5

        def exe(self):
            return "/opt/ovms"

    import psutil
    monkeypatch.setattr(psutil, "Process", Tracked)
    monkeypatch.setattr(serve, "preflight", lambda *a: {"ok": True})
    monkeypatch.setattr(serve, "launch", lambda plan: Proc())
    monkeypatch.setattr(serve, "status", lambda plan: {"healthy": True, "process_alive": True})
    state = serve.start(plan, config, wait_seconds=5)
    record = json.loads(open(plan["state_file"], encoding="utf-8").read())
    assert isinstance(state["launch_to_ready_ms"], int) and state["launch_to_ready_ms"] >= 0
    assert record["launch_to_ready_ms"] == state["launch_to_ready_ms"]
    assert record["ready_utc"] == state["ready_utc"]
    assert (record["pid"], record["create_time"]) == (4242, 99.5)


def test_readiness_is_not_written_over_a_record_for_another_process(monkeypatch, tmp_path):
    plan = serve.make_plan(PROFILE, MODEL_PRESETS[PROFILE], tmp_path, executable="ovms")
    other = {"pid": 7, "create_time": 1.0, "process_exe": "/opt/ovms", "plan": plan}
    serve.write_json(plan["state_file"], other)
    state = serve._record_readiness(plan, {"pid": 4242, "create_time": 99.5}, {"healthy": True}, 0.0)
    assert "launch_to_ready_ms" in state
    assert json.loads(open(plan["state_file"], encoding="utf-8").read()) == json.loads(json.dumps(other))
