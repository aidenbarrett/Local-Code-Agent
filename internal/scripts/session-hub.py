#!/usr/bin/env python3
"""User-facing Session Hub composition root.

This module owns product composition only. Conversation turns remain in the
canonical conversation store. Durable Session Hub events remain in the SQLite
session store. Task policy and admission identity remain in hashed product source.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys
from uuid import NAMESPACE_URL, uuid5

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from local_agent.config import MODEL_PRESETS, find_repo_root, load_repo_config  # noqa: E402
from local_agent.llm.client import OpenAICompatibleClient  # noqa: E402
from local_agent.provenance import package_identity  # noqa: E402
from local_agent.session.conversation_store import (  # noqa: E402
    ContextRefusal,
    conversation,
    create_session,
    ensure_runtime,
    new_session,
)
from local_agent.session.durable_history import (  # noqa: E402
    DurableConversationGateway,
    DurableTaskHistory,
    ResultSummaryRecordingController,
    TurnTaskSessionStore,
)
from local_agent.session.event_buffer import EventBuffer  # noqa: E402
from local_agent.session.session_event_service import (  # noqa: E402
    DurableSessionService,
    DurableTaskExecutor,
)
from local_agent.session.task_admission import (  # noqa: E402
    DurableTaskAdmissionRunner,
    repository_id,
)
from local_agent.session.task_controller import TaskController  # noqa: E402
from local_agent.session.cli import conversation_budgets, safe_terminal  # noqa: E402


def _runtime_root() -> Path:
    explicit = os.environ.get("LCA_RUNTIME_ROOT")
    if explicit:
        return Path(explicit)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home()
    return base / "LocalCodeAgent"


def _durable_ids(conversation_id: str) -> tuple[str, str]:
    stream_id = uuid5(NAMESPACE_URL, f"urn:lca:session-hub:stream:{conversation_id}")
    session_id = uuid5(NAMESPACE_URL, f"urn:lca:session-hub:session:{conversation_id}")
    return str(stream_id), str(session_id)


def _controller_commit() -> str:
    identity = package_identity()
    value = identity.get("package_commit") or identity.get("source_sha256") or "unknown"
    return str(value)[:64]


def _open_durable_service(runtime_root: Path, conversation_id: str):
    stream_id, session_id = _durable_ids(conversation_id)
    store = TurnTaskSessionStore(runtime_root / "session-hub" / "session.db")
    service = DurableSessionService(store, stream_id=stream_id, session_id=session_id)
    try:
        recovered = service.recover_unknown_tasks()
    except BaseException:
        service.close()
        raise
    return service, recovered


def _emit_session_opened(service: DurableSessionService, *, conversation_id: str, repo, recovered: bool) -> None:
    receipt = service.append(
        "session.opened",
        {
            "conversation_id": conversation_id,
            "repository_id": repository_id(repo),
            "controller_commit": _controller_commit(),
            "capabilities": [
                "conversation",
                "repository_read",
                "durable_session_events",
                "durable_task_execution",
                "durable_task_history",
            ],
            "recovered": recovered,
        },
    )
    receipt.wait(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-code-agent session")
    parser.add_argument("--repo", default=".", help="repository root or a path inside it")
    parser.add_argument("--profile", default="ptl-npu-8b", choices=sorted(MODEL_PRESETS))
    parser.add_argument("--worker-profile", default=None, choices=sorted(MODEL_PRESETS))
    parser.add_argument("--base-url", help="override conversation endpoint")
    parser.add_argument("--worker-base-url", help="override worker endpoint")
    parser.add_argument("--allow-execution", action="store_true", help="allow configured build/test execution")
    parser.add_argument("--conversation", metavar="ID", help="resume a persisted Session Hub conversation")
    parser.add_argument("--check", action="store_true", help="run deterministic self-check once and exit")
    args = parser.parse_args(argv)

    root = find_repo_root(Path(args.repo))
    repo = load_repo_config(root)

    chat_config = MODEL_PRESETS[args.profile]
    if args.base_url:
        chat_config = replace(chat_config, base_url=args.base_url)
    worker_profile = args.worker_profile or args.profile
    worker_config = MODEL_PRESETS[worker_profile]
    if args.worker_base_url:
        worker_config = replace(worker_config, base_url=args.worker_base_url)

    request_chars, disk_chars = conversation_budgets(chat_config)
    runtime_root = _runtime_root()
    try:
        if args.conversation:
            conversation_id = args.conversation
        else:
            session = new_session(args.profile, chat_config.model, chat_config.device, budget_chars=disk_chars)
            create_session(runtime_root, session)
            conversation_id = session.conversation_id

        with conversation(runtime_root, conversation_id) as opened:
            runtime_index = ensure_runtime(opened.session, args.profile, chat_config.model, chat_config.device)
            service, recovered = _open_durable_service(runtime_root, conversation_id)
            try:
                _emit_session_opened(service, conversation_id=conversation_id, repo=repo, recovered=bool(recovered))

                def show(event) -> None:
                    detail = event.payload.get("summary") or event.payload.get("message") or ""
                    print(f"[{event.sequence:04d}] {event.kind}" + (f"  {detail}" if detail else ""))

                events = EventBuffer(service.stream_id, show)

                def worker_factory():
                    return OpenAICompatibleClient(worker_config)

                controller = ResultSummaryRecordingController(
                    TaskController(
                        repo,
                        worker_factory,
                        events,
                        allow_execution=args.allow_execution,
                        context_budget_tokens=worker_config.context_budget_tokens,
                    ),
                    service.store,
                )
                task_runner = DurableTaskAdmissionRunner(DurableTaskExecutor(service, controller))
                task_history = DurableTaskHistory(
                    service.store,
                    stream_id=service.stream_id,
                )
                gateway = DurableConversationGateway(
                    OpenAICompatibleClient(chat_config),
                    controller,
                    events,
                    conversation=opened,
                    runtime_index=runtime_index,
                    request_bytes=request_chars,
                    task_runner=task_runner,
                    task_history=task_history,
                )

                if args.check:
                    answer = gateway.turn("/check")
                    print(answer)
                    return 0 if gateway.last_result and gateway.last_result.outcome.succeeded else 2

                print("Local Code Agent Session Hub")
                print(f"Repository: {root}")
                print(f"Conversation: {conversation_id}")
                print("Conversation history: persisted")
                print("Durable session events: enabled")
                if recovered:
                    print(f"Recovered {len(recovered)} unfinished task(s) as unknown / NO_VERDICT.")
                print("Task execution: durable admission enabled before controller effects")
                print("Task follow-up history: durable and restart-safe")
                print("Commands: /check, /quit")
                print()

                while True:
                    try:
                        line = input("you> ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        return 0
                    if line in {"/quit", "/exit"}:
                        return 0
                    if not line:
                        continue
                    try:
                        print(gateway.turn(line))
                    except ContextRefusal as exc:
                        print(f"Conversation refused: {exc}")
                    except RuntimeError as exc:
                        print(f"Session error: {exc}")
            finally:
                service.close()
    except ContextRefusal as exc:
        print(f"Conversation refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Session startup failed: {safe_terminal(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
