#!/usr/bin/env python3
"""User-facing Session Hub composition root.

Conversation turns remain in the canonical conversation store. Durable Session Hub
events and task artifacts remain in SQLite. Model calls share the existing in-process
endpoint queue/lease authority; this does not claim cross-process arbitration.
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
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor  # noqa: E402
from local_agent.session.cli import conversation_budgets, safe_terminal  # noqa: E402
from local_agent.session.conversation_store import (  # noqa: E402
    ContextRefusal, conversation, create_session, ensure_runtime, new_session,
)
from local_agent.session.durable_routes import DurableRouteEvents  # noqa: E402
from local_agent.session.durable_task_controller import AdmittedDurableTaskController  # noqa: E402
from local_agent.session.endpoint_call import EndpointCallAdapter  # noqa: E402
from local_agent.session.endpoint_client import ManagedLLMClient, ManagedWorkerClientFactory  # noqa: E402
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRole  # noqa: E402
from local_agent.session.endpoint_runtime import EndpointRuntime  # noqa: E402
from local_agent.session.event_buffer import EventBuffer  # noqa: E402
from local_agent.session.runtime_facts import RuntimeFacts  # noqa: E402
from local_agent.session.runtime_facts_gateway import RuntimeFactsGateway  # noqa: E402
from local_agent.session.session_event_service import DurableSessionService  # noqa: E402
from local_agent.session.session_store import SQLiteSessionStore  # noqa: E402
from local_agent.session.task_admission import DurableTaskAdmissionRunner, repository_id  # noqa: E402
from local_agent.session.task_controller import TaskController  # noqa: E402
from local_agent.session.task_history import DurableTaskHistory  # noqa: E402
from local_agent.session.textual_runtime import build_textual_session_runtime  # noqa: E402
from measurement.managed_runtime import ensure_managed_runtime  # noqa: E402


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
    store = SQLiteSessionStore(runtime_root / "session-hub" / "session.db")
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
                "conversation", "repository_read", "durable_session_events",
                "durable_task_execution", "durable_task_history", "durable_tool_activity",
                "deterministic_routing", "textual_session_hub", "non_blocking_turn_dispatch",
                "explicit_model_route_acceptance", "task_execution_epoch_fencing",
                "in_process_endpoint_arbitration", "user_stop_request",
            ],
            "recovered": recovered,
        },
    )
    receipt.wait(5)


def _resolve_model_configs(args: argparse.Namespace):
    chat_config = MODEL_PRESETS[args.profile]
    if args.base_url:
        chat_config = replace(chat_config, base_url=args.base_url)
    worker_profile = args.worker_profile or args.profile
    worker_config = MODEL_PRESETS[worker_profile]
    worker_base_url = args.worker_base_url or args.base_url
    if worker_base_url:
        worker_config = replace(worker_config, base_url=worker_base_url)
    return chat_config, worker_profile, worker_config


def _endpoint_adapter(config, adapters: dict[str, EndpointCallAdapter]) -> EndpointCallAdapter:
    endpoint_id = config.base_url.rstrip("/")
    adapter = adapters.get(endpoint_id)
    if adapter is None:
        adapter = EndpointCallAdapter(EndpointRuntime(EndpointArbiter(endpoint_id)))
        adapters[endpoint_id] = adapter
    return adapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-code-agent session")
    parser.add_argument("--repo", default=".", help="repository root or a path inside it")
    parser.add_argument("--profile", default="ptl-npu-8b", choices=sorted(MODEL_PRESETS))
    parser.add_argument("--worker-profile", default=None, choices=sorted(MODEL_PRESETS))
    parser.add_argument("--base-url", help="override conversation and worker endpoint unless --worker-base-url is set")
    parser.add_argument("--worker-base-url", help="explicitly split the worker onto a different endpoint")
    parser.add_argument("--allow-execution", action="store_true", help="allow configured build/test execution")
    parser.add_argument("--conversation", metavar="ID", help="resume a persisted Session Hub conversation")
    parser.add_argument("--check", action="store_true", help="run deterministic self-check once and exit")
    args = parser.parse_args(argv)

    root = find_repo_root(Path(args.repo))
    repo = load_repo_config(root)
    chat_config, worker_profile, worker_config = _resolve_model_configs(args)
    budgets = conversation_budgets(chat_config.context_budget_tokens)
    runtime_root = _runtime_root()

    try:
        if not args.check:
            ensured = ensure_managed_runtime(args.profile, chat_config, runtime_root)
            if not ensured.ok:
                raise RuntimeError(ensured.message)

        runtime_facts = RuntimeFacts.observe(
            args.profile, chat_config, execution_enabled=args.allow_execution,
        )

        if args.conversation:
            conversation_id = args.conversation
        else:
            session = new_session(
                args.profile, chat_config.model, chat_config.device,
                budget_chars=budgets["request_chars"],
            )
            create_session(runtime_root, session)
            conversation_id = session.conversation_id

        with conversation(runtime_root, conversation_id) as opened:
            runtime_index = ensure_runtime(opened.session, args.profile, chat_config.model, chat_config.device)
            service, recovered = _open_durable_service(runtime_root, conversation_id)
            try:
                _emit_session_opened(service, conversation_id=conversation_id, repo=repo, recovered=bool(recovered))
                events = EventBuffer(service.stream_id)
                adapters: dict[str, EndpointCallAdapter] = {}
                chat_adapter = _endpoint_adapter(chat_config, adapters)
                worker_adapter = _endpoint_adapter(worker_config, adapters)
                worker_factory = ManagedWorkerClientFactory(
                    lambda: OpenAICompatibleClient(worker_config),
                    worker_adapter,
                    session_id=service.session_id,
                )
                controller = TaskController(
                    repo, worker_factory, events,
                    allow_execution=args.allow_execution,
                    context_budget_tokens=worker_config.context_budget_tokens,
                )
                admitted_controller = AdmittedDurableTaskController(service, controller)
                task_executor = CancellableDurableTaskExecutor(service, admitted_controller)
                task_runner = DurableTaskAdmissionRunner(task_executor)
                chat_client = ManagedLLMClient(
                    OpenAICompatibleClient(chat_config),
                    chat_adapter,
                    role=EndpointRole.CONVERSATION,
                    session_id=service.session_id,
                )
                gateway = RuntimeFactsGateway(
                    chat_client, controller, events,
                    runtime_facts=runtime_facts,
                    conversation=opened,
                    runtime_index=runtime_index,
                    task_runner=task_runner,
                    task_history=DurableTaskHistory(service.store, stream_id=service.stream_id),
                    route_events=DurableRouteEvents(service),
                    **budgets,
                )

                if args.check:
                    answer = gateway.turn("/check")
                    print(answer)
                    return 0 if gateway.last_result and gateway.last_result.outcome.succeeded else 2

                with build_textual_session_runtime(
                    service,
                    opened,
                    gateway,
                    stop_task=lambda task_id, execution_epoch: task_executor.request_cancel(
                        task_id,
                        execution_epoch=execution_epoch,
                    ),
                    runtime_summary=runtime_facts.header(),
                ) as textual:
                    textual.app.run()
                return 0
            finally:
                service.close()
    except ContextRefusal as exc:
        print(f"Conversation refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Session startup failed: {safe_terminal(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
