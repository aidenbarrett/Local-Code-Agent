"""#481: the managed-runtime outcome is a durable, session-scoped fact.

The serving owner decides reuse/start/replace/refuse; the Session Hub only records
that typed outcome in the session's durable stream and shows it in the header.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from local_agent.session.conversation_store import session_path
from local_agent.session.event_contract import EventContractError, build_event
from local_agent.session.session_event_service import DurableSessionService
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
    assert f"\n{summary}\n" in str(observed["runtime_summary"])


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


_REFUSED = RuntimeEnsureResult(
    RuntimeLifecycle.REFUSED, "configured model endpoint is reachable but is not owned",
    refused_reason=RefusedReason.FOREIGN_ENDPOINT,
)

_HOLD_CONVERSATION = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from local_agent.session.conversation_store import conversation
with conversation(Path(sys.argv[2]), sys.argv[3]):
    print("held", flush=True)
    sys.stdin.read()
"""


def _admit_unfinished_task(hub, runtime_root: Path, conversation_id: str) -> str:
    """A live Hub's task: admitted and running, with no verdict or close yet."""
    stream_id, session_id = hub._durable_ids(conversation_id)
    service = DurableSessionService(
        SQLiteSessionStore(runtime_root / "session-hub" / "session.db"),
        stream_id=stream_id, session_id=session_id,
    )
    try:
        request = b"request"
        receipt = service.submit_task(
            request_id=str(uuid4()), payload_sha256="c" * 64,
            admission_payload={
                "origin": {"kind": "user_direct", "turn_ref": {
                    "conversation_id": conversation_id, "turn_index": 0,
                    "turn_sha256": "a" * 64}},
                "request_ref": {"artifact_id": str(uuid4()),
                                "sha256": hashlib.sha256(request).hexdigest(),
                                "media_type": "application/json",
                                "size_bytes": len(request), "availability": "unavailable"},
                "contract_sha256": "b" * 64, "repository_id": "repo-1", "skill": "inspect",
                "execution_epoch": 0, "deadline_utc": "2030-01-01T00:00:00Z",
            },
        )
        receipt.wait(5)
        assert receipt.task_id is not None
        service.append("task.state_changed", {
            "previous": "admitted", "current": "running", "reason_code": "requested",
            "execution_epoch": 0,
        }, task_id=receipt.task_id).wait(5)
        return receipt.task_id
    finally:
        service.close()


def test_a_refused_reopen_of_a_conversation_open_elsewhere_never_touches_its_stream(
        tmp_path, monkeypatch, capsys):
    """Recording must not recover or finalize a live Hub's task without its conversation lock."""
    hub = _load_hub()
    observed = _start_hub(hub, tmp_path, monkeypatch, RuntimeEnsureResult(
        RuntimeLifecycle.REUSED, "owned model endpoint already ready"))
    assert hub.main(["--repo", str(REPO), "--profile", PROFILE]) == 0
    conversation_id = str(observed["conversation_id"])
    live_task = _admit_unfinished_task(hub, tmp_path, conversation_id)
    before = _events(hub, tmp_path, conversation_id)
    capsys.readouterr()

    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_CONVERSATION, str(REPO / "internal"), str(tmp_path),
         conversation_id],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        _start_hub(hub, tmp_path, monkeypatch, _REFUSED)
        assert hub.main(["--repo", str(REPO), "--profile", PROFILE,
                         "--conversation", conversation_id]) == 2
    finally:
        holder.communicate("", timeout=30)

    after = _events(hub, tmp_path, conversation_id)
    assert not [event for event in after if event["task_id"] == live_task
                and event["kind"] in {"task.verdict", "task.closed"}], "the live task was finalized"
    assert after == before, "a refused reopen wrote to a stream another process owns"
    error = capsys.readouterr().err
    assert "is not owned" in error, "the runtime refusal must stay the reported cause"
    assert "the refusal was not recorded" in error and "already open in another process" in error


def test_a_refused_reopen_of_a_malformed_conversation_records_nothing(
        tmp_path, monkeypatch, capsys):
    hub = _load_hub()
    conversation_id = "2026-10-14T00-00-00-000000Z-beef"
    path = session_path(tmp_path, conversation_id)
    path.parent.mkdir(parents=True)
    path.write_text("{not a conversation", encoding="utf-8")
    _start_hub(hub, tmp_path, monkeypatch, _REFUSED)

    assert hub.main(["--repo", str(REPO), "--profile", PROFILE,
                     "--conversation", conversation_id]) == 2

    assert not (tmp_path / "session-hub").exists(), "a malformed conversation got a stream"
    error = capsys.readouterr().err
    assert "is not owned" in error and "the refusal was not recorded" in error


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
