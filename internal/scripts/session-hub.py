#!/usr/bin/env python3
"""User-facing Session Hub composition root.

Conversation turns remain in the canonical conversation store. Durable Session Hub
events and task artifacts remain in SQLite. Model calls share the existing in-process
endpoint queue/lease authority; this does not claim cross-process arbitration.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys
import subprocess
import tomllib
from collections.abc import Callable
from typing import NamedTuple
from uuid import NAMESPACE_URL, uuid5

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from local_agent.config import (  # noqa: E402
    DEFAULT_CONFIG_NAME,
    MODEL_PRESETS,
    ConfigError,
    find_repo_root,
    load_repo_config,
)
from local_agent.repo_setup import init_command  # noqa: E402
from local_agent.llm.client import LLMClient, OpenAICompatibleClient, stop_proof_for  # noqa: E402
from local_agent.provenance import package_identity  # noqa: E402
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor  # noqa: E402
from local_agent.session.conversation_gateway import conversation_budgets  # noqa: E402
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
from local_agent.session.runtime_facts_gateway import RepositoryFacts, RuntimeFactsGateway  # noqa: E402
from local_agent.session.session_event_service import DurableSessionService  # noqa: E402
from local_agent.session.session_store import SQLiteSessionStore  # noqa: E402
from local_agent.session.task_admission import DurableTaskAdmissionRunner, repository_id  # noqa: E402
from local_agent.session.task_controller import TaskController  # noqa: E402
from local_agent.session.workspaces import GitWorkspaceManager  # noqa: E402
from local_agent.session.task_history import DurableTaskHistory  # noqa: E402
from local_agent.session.textual_runtime import build_textual_session_runtime  # noqa: E402
from serving.managed_runtime import ensure_managed_runtime  # noqa: E402
from serving.model_choice import ModelChoiceError, resolve_preset  # noqa: E402
from serving.model_store import default_runtime_root as _runtime_root  # noqa: E402


def _safe_terminal(text: str) -> str:
    """Drop escape and control characters before printing untrusted text to the console."""
    return "".join(c for c in text if c in "\n\t" or (c.isprintable() and c != "\x1b"))


def _durable_ids(conversation_id: str) -> tuple[str, str]:
    stream_id = uuid5(NAMESPACE_URL, f"urn:lca:session-hub:stream:{conversation_id}")
    session_id = uuid5(NAMESPACE_URL, f"urn:lca:session-hub:session:{conversation_id}")
    return str(stream_id), str(session_id)


def _repository_summary(root: Path, *, execution_enabled: bool) -> str:
    """Render observed repository authority for the always-visible Hub header."""
    canonical = root.resolve()
    try:
        branch = subprocess.run(
            ["git", "-C", str(canonical), "branch", "--show-current"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        branch = ""
    branch_label = branch or "detached/unknown"
    execution = "enabled" if execution_enabled else "disabled"
    return (
        f"repo {canonical.name} · root {canonical} · branch {branch_label} (observed) "
        f"· scope repository only · execution {execution}"
    )


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
        runtime = EndpointRuntime(EndpointArbiter(endpoint_id), stop_proof=stop_proof_for(config))
        adapter = EndpointCallAdapter(runtime)
        adapters[endpoint_id] = adapter
        return adapter
    existing = adapter.runtime.stop_proof
    if (existing.kind if existing is not None else "none") != config.stop_proof:
        # One physical endpoint has one proof source; profiles disagreeing about it
        # is a configuration error, not something to pick between.
        raise ValueError(
            f"profiles sharing {endpoint_id} declare different stop_proof values"
        )
    return adapter



class SessionGraph(NamedTuple):
    """The composed Session Hub: what the Textual app, --check and the acceptance
    journey runner all drive. There is one composition, and this is it.

    (A NamedTuple, not a dataclass: this script is also loaded by file path, where
    dataclasses cannot resolve their defining module.)"""

    gateway: RuntimeFactsGateway
    task_executor: CancellableDurableTaskExecutor
    controller: TaskController
    history: DurableTaskHistory
    workspaces: GitWorkspaceManager


def compose_session_graph(
    service: DurableSessionService, repo, chat_config, worker_config, *,
    runtime_facts: RuntimeFacts, opened, runtime_index, runtime_root: Path,
    allow_execution: bool, budgets: dict,
    worker_client: Callable[[], LLMClient] | None = None,
) -> SessionGraph:
    """Compose the Session Hub. ``worker_client`` replaces only the raw worker model
    client (the acceptance runner's scripted fix); the endpoint authority, controller
    and every durable boundary are the same objects the product uses."""
    events = EventBuffer(service.stream_id)
    adapters: dict[str, EndpointCallAdapter] = {}
    chat_adapter = _endpoint_adapter(chat_config, adapters)
    worker_adapter = _endpoint_adapter(worker_config, adapters)
    worker_factory = ManagedWorkerClientFactory(
        worker_client if worker_client is not None else (lambda: OpenAICompatibleClient(worker_config)),
        worker_adapter,
        session_id=service.session_id,
    )
    # Short on purpose: MSVC build trees nest deep under a candidate
    # worktree and Windows MAX_PATH is counted from the drive root.
    workspaces = GitWorkspaceManager(
        runtime_root / "ws", controller_commit=_controller_commit(),
    )
    # Worktrees left by a controller that died mid-task, with nothing
    # retained to apply. Live owners (including another Hub) are untouched.
    workspaces.reap_orphans()
    if workspaces.unreconciled_leases:
        # Bounded: the count and where to look, never a deletion on a guess.
        print(
            f"Note: {len(workspaces.unreconciled_leases)} candidate workspace(s) under "
            f"{workspaces.workspaces_root} could not be checked for a live owner and were "
            "kept. Remove them by hand once no Session Hub is using them.",
            file=sys.stderr,
        )
    controller = TaskController(
        repo, worker_factory, events,
        allow_execution=allow_execution,
        context_budget_tokens=worker_config.context_budget_tokens,
        workspaces=workspaces,
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
    history = DurableTaskHistory(service.store, stream_id=service.stream_id)
    branch = ""
    try:
        branch = subprocess.run(
            ["git", "-C", str(repo.root), "branch", "--show-current"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    repository_facts = RepositoryFacts(
        name=repo.name,
        root=repo.root.resolve(),
        branch=branch or "detached/unknown",
        execution_enabled=allow_execution,
    )
    gateway = RuntimeFactsGateway(
        chat_client, controller, events,
        runtime_facts=runtime_facts,
        repository_facts=repository_facts,
        conversation=opened,
        runtime_index=runtime_index,
        task_runner=task_runner,
        task_history=history,
        route_events=DurableRouteEvents(service),
        **budgets,
    )
    return SessionGraph(gateway, task_executor, controller, history, workspaces)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-code-agent session")
    parser.add_argument("--repo", default=".", help="repository root or a path inside it")
    parser.add_argument("--profile", default=None, choices=sorted(MODEL_PRESETS),
                        help="model preset for this session (default: your `models use` choice)")
    parser.add_argument("--worker-profile", default=None, choices=sorted(MODEL_PRESETS))
    parser.add_argument("--base-url", help="override conversation and worker endpoint unless --worker-base-url is set")
    parser.add_argument("--worker-base-url", help="explicitly split the worker onto a different endpoint")
    parser.add_argument("--allow-execution", action="store_true", help="allow configured build/test execution")
    parser.add_argument("--conversation", metavar="ID", help="resume a persisted Session Hub conversation")
    parser.add_argument("--check", action="store_true", help="run deterministic self-check once and exit")
    args = parser.parse_args(argv)
    try:
        args.profile = resolve_preset(args.profile, _runtime_root()).name
    except ModelChoiceError as exc:
        parser.error(str(exc))

    root = find_repo_root(Path(args.repo))
    try:
        repo = load_repo_config(root)
    except (ConfigError, tomllib.TOMLDecodeError, OSError) as exc:
        # Refuse before any effect, with the one supported next step, never a traceback.
        declaration = root / DEFAULT_CONFIG_NAME
        next_step = (
            f"correct {declaration}, then check it with .\\local-code-agent.ps1 doctor"
            if declaration.is_file() else init_command(root)
        )
        sys.stderr.write(f"Repository not ready: {_safe_terminal(str(exc))}\nNext: {next_step}\n")
        return 2
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
                graph = compose_session_graph(
                    service, repo, chat_config, worker_config,
                    runtime_facts=runtime_facts, opened=opened, runtime_index=runtime_index,
                    runtime_root=runtime_root, allow_execution=args.allow_execution,
                    budgets=budgets,
                )
                gateway, task_executor = graph.gateway, graph.task_executor

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
                    runtime_summary=f"{runtime_facts.header()} · {_repository_summary(root, execution_enabled=args.allow_execution)}",
                ) as textual:
                    textual.app.run()
                return 0
            finally:
                service.close()
    except ContextRefusal as exc:
        print(f"Conversation refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Session startup failed: {_safe_terminal(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
