#!/usr/bin/env python3
"""Executable acceptance gates for the Session Hub milestone.

Each implementation slice extends this file with assertions for behaviour it
actually ships. CI and local bring-up run the same gates against product code.
"""
from __future__ import annotations

import ast
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4


REPO = Path(__file__).resolve().parents[2]
INTERNAL = REPO / "internal"
SCHEMA = INTERNAL / "docs" / "session-contract" / "v1" / "events.schema.json"
CHAT_CONTEXT = INTERNAL / "scripts" / "chat_context.py"
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

V1_REQUIRED_EVENT_KINDS = {
    "session.opened", "turn.recorded", "route.proposed", "route.resolved",
    "task.admitted", "task.state_changed", "endpoint.state_changed",
    "tool.started", "tool.finished", "artifact.recorded",
    "task.cancel_requested", "task.verdict", "task.closed",
    "watch.state_changed", "watch.run_recorded", "telemetry.policy",
    "fault.reported", "telemetry.sample", "conversation.delta",
}


class AcceptanceFailure(RuntimeError):
    pass


def require(condition: bool, name: str) -> None:
    if not condition:
        raise AcceptanceFailure(name)
    print(f"ok  {name}")


def _collect_dotted_kind_consts(value) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        props = value.get("properties")
        if isinstance(props, dict):
            kind = props.get("kind")
            if isinstance(kind, dict):
                candidate = kind.get("const")
                if isinstance(candidate, str) and "." in candidate:
                    found.append(candidate)
        for child in value.values():
            found.extend(_collect_dotted_kind_consts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_collect_dotted_kind_consts(child))
    return found


def _load_chat_context():
    spec = spec_from_file_location("accept_session_hub_chat_context", CHAT_CONTEXT)
    if not spec or not spec.loader:
        raise AcceptanceFailure("cannot import persisted conversation context")
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _application_reaches_private_storage() -> list[str]:
    offenders: list[str] = []
    for root in (INTERNAL / "scripts", INTERNAL / "local_agent"):
        for path in root.rglob("*.py"):
            if path == CHAT_CONTEXT:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError) as exc:
                raise AcceptanceFailure(f"cannot inspect {path.relative_to(REPO)}: {exc}") from exc
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in {"_load_session", "_save_session"}:
                    offenders.append(str(path.relative_to(REPO)))
                elif isinstance(node, ast.Attribute) and node.attr in {"_load_session", "_save_session"}:
                    offenders.append(str(path.relative_to(REPO)))
    return sorted(set(offenders))


def _schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def schema_gates() -> None:
    require(SCHEMA.is_file(), "session event schema exists")
    schema = _schema()
    require(schema.get("$id") == "urn:lca:session:events:1", "event schema id is v1")
    kinds = _collect_dotted_kind_consts(schema)
    require(len(kinds) == len(set(kinds)), "event schema has no duplicate typed event kinds")
    require(V1_REQUIRED_EVENT_KINDS.issubset(set(kinds)), "event schema retains every reviewed v1 event kind")


def conversation_ownership_gates() -> None:
    require(CHAT_CONTEXT.is_file(), "persisted conversation context exists")
    ctx = _load_chat_context()
    require(not hasattr(ctx, "load_session"), "no public unlocked persisted-session loader remains")
    require(not hasattr(ctx, "save_session"), "no public persisted-session replacement API remains")
    require(hasattr(ctx, "conversation"), "existing conversations open through owned context")
    require(hasattr(ctx, "create_session"), "new conversation creation is explicit")
    require(not _application_reaches_private_storage(), "application code cannot reach private session storage")

    with tempfile.TemporaryDirectory(prefix="lca-hub-accept-") as temp:
        root = Path(temp)
        session = ctx.new_session("accept", "model", "NPU")
        ctx.create_session(root, session)
        with ctx.conversation(root, session.conversation_id) as opened:
            ctx.append_turn(opened.session, "user", "first", 0)
            opened.save()
        with ctx.conversation(root, session.conversation_id) as opened:
            ctx.append_turn(opened.session, "assistant", "second", 0)
            opened.save()
        with ctx.conversation(root, session.conversation_id) as opened:
            require([turn.content for turn in opened.session.turns] == ["first", "second"], "sequential owners preserve every committed turn")
            ctx.append_turn(opened.session, "user", "abandoned", 0)
        with ctx.conversation(root, session.conversation_id) as opened:
            require([turn.content for turn in opened.session.turns] == ["first", "second"], "context exit does not implicitly persist abandoned edits")


def outcome_gates() -> None:
    from local_agent.session.contracts import (
        TASK_OUTCOME_PROJECTIONS, UNREACHABLE_TERMINAL_STATES, TaskOutcome,
        TaskResult, TerminalState, task_exit_code,
    )

    schema_states = set(_schema()["$defs"]["TaskCompletion"]["properties"]["status"]["enum"])
    require({state.value for state in TerminalState} == schema_states, "product terminal-state vocabulary exactly matches the v1 schema")
    require(set(TASK_OUTCOME_PROJECTIONS) == set(TaskOutcome), "every task outcome has one lifecycle projection")
    produced = {projection.terminal_state for projection in TASK_OUTCOME_PROJECTIONS.values()}
    unreachable = set(UNREACHABLE_TERMINAL_STATES)
    require(produced | unreachable == set(TerminalState), "every v1 terminal state is produced or explicitly unreachable")
    require(produced.isdisjoint(unreachable), "unreachable terminal states have no producer mapping")
    require(all(all(p.terminal_state != state for p in TASK_OUTCOME_PROJECTIONS.values()) for state in unreachable), "unreachable-for-now markers cannot coexist with producers")
    require(set(task_exit_code(outcome) for outcome in TaskOutcome).issubset({0, 1, 2}), "every task outcome has a bounded CLI exit class")

    try:
        TaskResult("accept", TaskOutcome.PASS, "unproved", False)
    except ValueError:
        pass
    else:
        raise AcceptanceFailure("unproved success cannot construct a task result")

    unknown = TaskResult("accept", TaskOutcome.NO_VERDICT, "unknown", False)
    require(unknown.projection.terminal_state == TerminalState.UNKNOWN, "NO_VERDICT projects to explicit unknown lifecycle state")
    require(TerminalState.INTERRUPTED in unreachable, "interrupted is not used as the generic unknown-result bucket")


def _admission_payload(request_ref: dict) -> dict:
    return {
        "origin": {
            "kind": "user_direct",
            "turn_ref": {"conversation_id": "accept", "turn_index": 0, "turn_sha256": "a" * 64},
        },
        "request_ref": request_ref,
        "contract_sha256": "b" * 64,
        "repository_id": "accept-repo",
        "skill": "inspect",
        "execution_epoch": 0,
        "deadline_utc": "2030-01-01T00:00:00Z",
    }


def durable_event_gates() -> None:
    from local_agent.session.contracts import TaskOutcome, TaskResult
    from local_agent.session.event_contract import EventContractError, build_event
    from local_agent.session.session_event_service import DurableSessionService, DurableTaskExecutor
    from local_agent.session.session_store import SQLiteSessionStore

    stream_id = str(uuid4())
    session_id = str(uuid4())
    with tempfile.TemporaryDirectory(prefix="lca-hub-events-") as temp:
        store = SQLiteSessionStore(Path(temp) / "session.db")
        service = DurableSessionService(store, stream_id=stream_id, session_id=session_id)
        subscriber = service.subscribe(capacity=16)
        try:
            try:
                build_event(
                    stream_id=stream_id, sequence=1, producer_epoch=service.producer_epoch,
                    session_id=session_id, kind="session.opened", payload={"not": "the contract"},
                )
            except EventContractError:
                pass
            else:
                raise AcceptanceFailure("invalid durable event payload was accepted")
            require(True, "durable envelopes are validated against the v1 schema")

            request_bytes = b"accept-session-hub"
            request_ref = {
                "artifact_id": str(uuid4()),
                "sha256": hashlib.sha256(request_bytes).hexdigest(),
                "media_type": "application/json",
                "size_bytes": len(request_bytes),
                "availability": "retained",
            }
            admission_payload = _admission_payload(request_ref)
            receipt = service.submit_task(
                request_id="accept-request",
                payload_sha256=hashlib.sha256(request_bytes).hexdigest(),
                admission_payload=admission_payload,
                request_bytes=request_bytes,
            )
            require(receipt.task_id is not None, "non-blocking admission returns a task id immediately")
            receipt.wait(5)
            live = subscriber.drain()
            replayed = service.replay()
            require(live == replayed, "committed live delivery and durable replay are identical")
            require([row["task_id"] for row in store.unterminated_tasks(stream_id)] == [receipt.task_id], "durable admission is visible before execution")
            require(store.artifact_bytes(request_ref) == request_bytes, "retained admitted request bytes are hash-checked and readable")
            turn_ref = admission_payload["origin"]["turn_ref"]
            require(store.task_ids_for_turn(turn_ref) == [receipt.task_id], "task admission atomically records the exact originating TurnRef")

            recovered = service.recover_unknown_tasks()
            require(recovered == [receipt.task_id], "restart recovery finds admitted tasks without terminal state")
            events = service.replay()
            require([event["kind"] for event in events] == ["task.admitted", "task.verdict", "task.closed"], "recovery records verdict and closure without replaying effects")
            completion = events[1]["payload"]["completion"]
            require(completion["status"] == "unknown" and completion["verdict_block"]["verdict"] == "NO_VERDICT", "crash recovery resolves to explicit unknown/NO_VERDICT")
            require(events[2]["payload"]["status"] == "unknown" and events[2]["payload"]["cleanup"] == "unknown", "unknown recovery closes with cleanup unknown")
            require(not store.unterminated_tasks(stream_id), "recovered unknown task becomes durably terminal")
            record = store.task_record(receipt.task_id)
            require(record is not None and record["result_ref"] == completion["result_ref"], "terminal task row indexes the same durable result as the verdict and closure")
            require(store.artifact_bytes(record["result_ref"]), "recovered unknown task retains an inspectable bounded result artifact")
        finally:
            service.close()

    # Separate store proves the execution fence rather than relying on event order
    # from a manually submitted task above.
    with tempfile.TemporaryDirectory(prefix="lca-hub-exec-") as temp:
        store = SQLiteSessionStore(Path(temp) / "session.db")
        service = DurableSessionService(store, stream_id=str(uuid4()), session_id=str(uuid4()))
        seen: list[str] = []

        class Controller:
            def run(self, task, *, self_check=False, route_source=None, task_id=None):
                assert task_id is not None
                assert [row["task_id"] for row in store.unterminated_tasks(service.stream_id)] == [task_id]
                seen.append(task_id)
                return TaskResult(task_id, TaskOutcome.FAIL, "observed failure", False, verification_ran=True)

        try:
            request = b"executor"
            request_ref = {
                "artifact_id": str(uuid4()),
                "sha256": hashlib.sha256(request).hexdigest(),
                "media_type": "application/json",
                "size_bytes": len(request),
                "availability": "retained",
            }
            executor = DurableTaskExecutor(service, Controller())
            handle = executor.submit(
                task="inspect", request_id="executor-request",
                payload_sha256=hashlib.sha256(request).hexdigest(),
                admission_payload=_admission_payload(request_ref),
                request_bytes=request,
                route_source="user_direct",
            )
            handle.wait(10)
            require(seen == [handle.task_id], "controller execution begins only after durable admission")
            events = service.replay()
            require([event["kind"] for event in events] == ["task.admitted", "task.state_changed", "task.verdict", "task.closed"], "controller result follows admitted-running-verdict-close lifecycle")
            record = store.task_record(handle.task_id)
            require(record["terminal"] is True, "verdict, closure and terminal result index commit before task completion returns")
            require(store.artifact_bytes(record["result_ref"]), "completed task retains its bounded result artifact")
            retry = executor.submit(
                task="inspect", request_id="executor-request",
                payload_sha256=hashlib.sha256(request).hexdigest(),
                admission_payload=_admission_payload(request_ref),
                request_bytes=request,
                route_source="user_direct",
            )
            retry.wait(10)
            require(seen == [handle.task_id], "idempotent admission never replays controller effects")
        finally:
            service.close()


def durable_subscription_gates() -> None:
    from local_agent.session.session_event_service import DurableSessionService
    from local_agent.session.session_store import SQLiteSessionStore

    with tempfile.TemporaryDirectory(prefix="lca-hub-handoff-") as temp:
        service = DurableSessionService(
            SQLiteSessionStore(Path(temp) / "session.db"),
            stream_id=str(uuid4()),
            session_id=str(uuid4()),
        )
        try:
            first = service.append("session.opened", {
                "conversation_id": "accept",
                "repository_id": "accept-repo",
                "controller_commit": "deadbeef",
                "capabilities": ["inspect"],
                "recovered": False,
            })
            first.wait(5)
            handoff = service.subscribe_from(after=0, capacity=8)
            second = service.append("session.opened", {
                "conversation_id": "accept",
                "repository_id": "accept-repo",
                "controller_commit": "deadbeef",
                "capabilities": ["inspect"],
                "recovered": True,
            })
            second.wait(5)

            replayed = service.replay_subscription(handoff)
            live = handoff.live.drain()
            require([event["sequence"] for event in replayed] == [1], "subscription handoff exposes a bounded durable replay window")
            require([event["sequence"] for event in live] == [2], "events after the replay boundary are delivered live exactly once")
        finally:
            service.close()


def main() -> int:
    try:
        schema_gates()
        conversation_ownership_gates()
        outcome_gates()
        durable_event_gates()
        durable_subscription_gates()
    except (AcceptanceFailure, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    print("Session Hub acceptance gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
