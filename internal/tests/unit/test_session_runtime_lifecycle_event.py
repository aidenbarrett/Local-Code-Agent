"""#481: the managed-runtime outcome is a durable, session-scoped fact.

The serving owner decides reuse/start/replace/refuse; the Session Hub only records
that typed outcome in the session's durable stream and shows it in the header.
"""
from __future__ import annotations

from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from local_agent.session.event_contract import EventContractError, build_event
from local_agent.session.session_store import SQLiteSessionStore
from serving import serve
from serving.managed_runtime import (
    RefusedReason,
    ReplacedReason,
    RuntimeEnsureResult,
    RuntimeLifecycle,
)

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "internal" / "scripts" / "session-hub.py"
PROFILE = "ptl-npu-8b"


def _load_hub():
    spec = spec_from_file_location("session_hub_runtime_lifecycle_event", SCRIPT)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _start_hub(hub, tmp_path, monkeypatch, outcome: RuntimeEnsureResult) -> dict[str, object]:
    observed: dict[str, object] = {}
    facts = hub.RuntimeFacts(
        preset=PROFILE, endpoint="http://127.0.0.1:9999/v1", declared_model="m",
        declared_device="NPU", observed_model="m", execution_enabled=False,
    )

    @contextmanager
    def fake_textual(service, opened, gateway, *, runtime_summary=None, stop_task=None):
        observed["runtime_summary"] = runtime_summary
        observed["conversation_id"] = opened.session.conversation_id
        yield type("TextualRuntime", (), {"app": type("App", (), {"run": lambda self: None})()})()

    monkeypatch.setattr(hub, "_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(hub, "ensure_managed_runtime", lambda *_args: outcome)
    monkeypatch.setattr(hub.RuntimeFacts, "observe", staticmethod(lambda *_a, **_k: facts))
    monkeypatch.setattr(hub, "build_textual_session_runtime", fake_textual)
    return observed


def _events(hub, runtime_root: Path, conversation_id: str) -> list[dict]:
    stream_id, _session_id = hub._durable_ids(conversation_id)
    store = SQLiteSessionStore(runtime_root / "session-hub" / "session.db")
    return store.replay(stream_id)


@pytest.mark.parametrize(("outcome", "payload", "summary"), [
    (RuntimeEnsureResult(RuntimeLifecycle.REUSED, "owned model endpoint already ready"),
     {"lifecycle": "reused", "replaced_reason": None, "refused_reason": None,
      "changed_fields": [], "launch_to_ready_ms": None},
     "server reused"),
    (RuntimeEnsureResult(RuntimeLifecycle.STARTED, "managed model endpoint ready",
                         launch_to_ready_ms=12345),
     {"lifecycle": "started", "replaced_reason": None, "refused_reason": None,
      "changed_fields": [], "launch_to_ready_ms": 12345},
     "server started, ready 12.3s after launch"),
    (RuntimeEnsureResult(RuntimeLifecycle.REPLACED, "managed model endpoint ready",
                         replaced_reason=ReplacedReason.CONFIG_CHANGED, changed_fields=("args",),
                         launch_to_ready_ms=41250),
     {"lifecycle": "replaced", "replaced_reason": "config_changed", "refused_reason": None,
      "changed_fields": ["args"], "launch_to_ready_ms": 41250},
     "server replaced (config_changed: args), ready 41.2s after launch"),
    (RuntimeEnsureResult(RuntimeLifecycle.REPLACED, "managed model endpoint ready",
                         replaced_reason=ReplacedReason.DEAD),
     {"lifecycle": "replaced", "replaced_reason": "dead", "refused_reason": None,
      "changed_fields": [], "launch_to_ready_ms": None},
     "server replaced (dead), launch time not recorded"),
])
def test_an_opened_session_records_what_it_did_to_the_server(
        tmp_path, monkeypatch, outcome, payload, summary):
    hub = _load_hub()
    observed = _start_hub(hub, tmp_path, monkeypatch, outcome)

    assert hub.main(["--repo", str(REPO), "--profile", PROFILE]) == 0

    events = _events(hub, tmp_path, str(observed["conversation_id"]))
    kinds = [event["kind"] for event in events]
    assert kinds[:2] == ["session.opened", "runtime.ensured"]
    assert kinds.count("runtime.ensured") == 1
    ensured = events[1]
    assert ensured["task_id"] is None and ensured["session_id"] == events[0]["session_id"]
    assert ensured["payload"] == {
        "profile": PROFILE, "runtime": hub.MODEL_PRESETS[PROFILE].runtime, **payload,
    }
    assert f" · {summary} · " in str(observed["runtime_summary"])


def test_a_reopened_conversation_gets_its_own_outcome_without_rewriting_the_first(
        tmp_path, monkeypatch):
    hub = _load_hub()
    observed = _start_hub(hub, tmp_path, monkeypatch, RuntimeEnsureResult(
        RuntimeLifecycle.STARTED, "managed model endpoint ready", launch_to_ready_ms=900))
    assert hub.main(["--repo", str(REPO), "--profile", PROFILE]) == 0
    conversation_id = str(observed["conversation_id"])

    _start_hub(hub, tmp_path, monkeypatch, RuntimeEnsureResult(
        RuntimeLifecycle.REUSED, "owned model endpoint already ready"))
    assert hub.main(["--repo", str(REPO), "--profile", PROFILE,
                     "--conversation", conversation_id]) == 0

    lifecycle = [event["payload"]["lifecycle"] for event in _events(hub, tmp_path, conversation_id)
                 if event["kind"] == "runtime.ensured"]
    assert lifecycle == ["started", "reused"]


def test_a_refused_reopen_is_recorded_in_that_conversation_and_stops_startup(
        tmp_path, monkeypatch, capsys):
    hub = _load_hub()
    observed = _start_hub(hub, tmp_path, monkeypatch, RuntimeEnsureResult(
        RuntimeLifecycle.REUSED, "owned model endpoint already ready"))
    assert hub.main(["--repo", str(REPO), "--profile", PROFILE]) == 0
    conversation_id = str(observed["conversation_id"])
    capsys.readouterr()

    refused = RuntimeEnsureResult(
        RuntimeLifecycle.REFUSED, "configured model endpoint is reachable but is not owned",
        refused_reason=RefusedReason.FOREIGN_ENDPOINT,
    )
    after = _start_hub(hub, tmp_path, monkeypatch, refused)
    assert hub.main(["--repo", str(REPO), "--profile", PROFILE,
                     "--conversation", conversation_id]) == 2

    assert "runtime_summary" not in after, "the Hub opened on a refused runtime"
    assert "is not owned" in capsys.readouterr().err
    events = _events(hub, tmp_path, conversation_id)
    assert [event["kind"] for event in events].count("session.opened") == 1
    assert events[-1]["kind"] == "runtime.ensured"
    assert events[-1]["payload"]["lifecycle"] == "refused"
    assert events[-1]["payload"]["refused_reason"] == "foreign_endpoint"


def test_a_refused_new_session_mints_no_conversation_or_stream(tmp_path, monkeypatch, capsys):
    hub = _load_hub()
    observed = _start_hub(hub, tmp_path, monkeypatch, RuntimeEnsureResult(
        RuntimeLifecycle.REFUSED, "managed model endpoint could not be started: boom",
        refused_reason=RefusedReason.START_FAILED,
    ))
    assert hub.main(["--repo", str(REPO), "--profile", PROFILE]) == 2
    assert "could not be started: boom" in capsys.readouterr().err
    assert observed == {}
    assert not (tmp_path / "session-hub").exists()


def test_the_self_check_never_claims_a_lifecycle_it_did_not_ensure(tmp_path, monkeypatch):
    hub = _load_hub()
    _start_hub(hub, tmp_path, monkeypatch, RuntimeEnsureResult(
        RuntimeLifecycle.REUSED, "unused"))
    monkeypatch.setattr(hub, "ensure_managed_runtime",
                        lambda *_a: (_ for _ in ()).throw(AssertionError("--check ensured")))
    hub.main(["--repo", str(REPO), "--profile", PROFILE, "--check"])
    db = tmp_path / "session-hub" / "session.db"
    store = SQLiteSessionStore(db)
    with store._connect() as conn:
        kinds = {row[0] for row in conn.execute("SELECT kind FROM events")}
    assert "session.opened" in kinds and "runtime.ensured" not in kinds


# --- contract: the schema accepts exactly what the owner can produce -------------------


def _event(payload: dict) -> dict:
    return build_event(
        stream_id="00000000-0000-4000-8000-000000000001", sequence=1,
        producer_epoch="00000000-0000-4000-8000-000000000002", kind="runtime.ensured",
        session_id="00000000-0000-4000-8000-000000000003",
        payload={"profile": PROFILE, "runtime": "ovms", **payload},
    )


@pytest.mark.parametrize("outcome", [
    RuntimeEnsureResult(RuntimeLifecycle.REUSED, "m"),
    RuntimeEnsureResult(RuntimeLifecycle.STARTED, "m", launch_to_ready_ms=0),
    RuntimeEnsureResult(RuntimeLifecycle.REPLACED, "m", replaced_reason=ReplacedReason.UNHEALTHY),
    RuntimeEnsureResult(RuntimeLifecycle.REPLACED, "m", replaced_reason=ReplacedReason.CONFIG_CHANGED,
                        changed_fields=tuple(serve.LAUNCH_FIELDS), launch_to_ready_ms=5),
    *(RuntimeEnsureResult(RuntimeLifecycle.REFUSED, "m", refused_reason=reason)
      for reason in RefusedReason),
])
def test_every_owner_outcome_is_a_valid_durable_payload(outcome):
    assert _event(outcome.fact())["payload"]["lifecycle"] == outcome.lifecycle.value


_VALID = {"lifecycle": "reused", "replaced_reason": None, "refused_reason": None,
          "changed_fields": [], "launch_to_ready_ms": None}


@pytest.mark.parametrize("change", [
    {"lifecycle": "warm"},
    {"replaced_reason": "dead"},
    {"refused_reason": "start_failed"},
    {"launch_to_ready_ms": 10},
    {"changed_fields": ["args"]},
    {"lifecycle": "replaced"},
    {"lifecycle": "refused"},
    {"lifecycle": "refused", "refused_reason": "not_healthy", "launch_to_ready_ms": 1},
    {"lifecycle": "replaced", "replaced_reason": "dead", "changed_fields": ["args"]},
    {"lifecycle": "replaced", "replaced_reason": "config_changed", "changed_fields": ["args", "args"]},
    {"lifecycle": "replaced", "replaced_reason": "config_changed", "changed_fields": ["prompt"]},
    {"lifecycle": "started", "launch_to_ready_ms": -1},
])
def test_contradictory_lifecycle_payloads_are_refused_at_the_boundary(change):
    with pytest.raises(EventContractError):
        _event({**_VALID, **change})


def test_a_runtime_fact_is_session_scoped_not_task_scoped():
    with pytest.raises(EventContractError):
        build_event(
            stream_id="00000000-0000-4000-8000-000000000001", sequence=1,
            producer_epoch="00000000-0000-4000-8000-000000000002", kind="runtime.ensured",
            session_id="00000000-0000-4000-8000-000000000003",
            task_id="00000000-0000-4000-8000-000000000004",
            payload={"profile": PROFILE, "runtime": "ovms", **_VALID},
        )
