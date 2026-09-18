"""Synchronous terminal client for the gateway; no screen scraping or shell tools."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from ..config import MODEL_PRESETS, find_repo_root, load_repo_config
from ..llm.client import OpenAICompatibleClient
from .controller import TaskController
from .events import EventBuffer
from .gateway import ConversationGateway


CHARS_PER_TOKEN_ESTIMATE = 4


def conversation_budgets(context_budget_tokens: int) -> dict[str, int]:
    """Explicit approximate character policy, plus a separate UTF-8 byte cap.

    Four chars/token is a sizing heuristic, not tokenizer output or a lower bound.
    Code and multilingual text can differ substantially. No occupancy claim follows.
    """
    if context_budget_tokens < 1:
        raise ValueError("context token budget must be positive")
    request_chars = min(24_000, context_budget_tokens * CHARS_PER_TOKEN_ESTIMATE)
    return {"history_chars": min(16_000, request_chars),
            "request_chars": request_chars, "request_bytes": request_chars * 4 + 1024}


def safe_terminal(text: str) -> str:
    # Strip escape/control characters from model text and repository output.
    return "".join(c for c in text if c in "\n\t" or (c.isprintable() and c != "\x1b"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="local-agent session")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--profile", choices=sorted(MODEL_PRESETS), default="ptl-npu-8b")
    parser.add_argument("--worker-profile", choices=sorted(MODEL_PRESETS))
    parser.add_argument("--base-url", help="Explicit existing conversation endpoint")
    parser.add_argument("--worker-base-url", help="Explicit existing worker endpoint")
    parser.add_argument("--allow-execution", action="store_true", help="Trust configured build/test code in this checkout")
    parser.add_argument("--check", action="store_true", help="Run LCA self-check once, without a model")
    args = parser.parse_args(argv)
    repo = load_repo_config(find_repo_root(Path(args.repo)))
    chat_config = MODEL_PRESETS[args.profile]
    if args.base_url:
        chat_config = replace(chat_config, base_url=args.base_url)
    worker_config = MODEL_PRESETS[args.worker_profile] if args.worker_profile else chat_config
    if args.worker_base_url:
        worker_config = replace(worker_config, base_url=args.worker_base_url)

    def show(event):
        print(safe_terminal(f"  [{event.sequence}] {event.kind} {event.payload}"), flush=True)

    events = EventBuffer(uuid4().hex, show)
    controller = TaskController(repo, lambda: OpenAICompatibleClient(worker_config), events,
                                allow_execution=args.allow_execution,
                                context_budget_tokens=worker_config.context_budget_tokens)
    if args.check:
        result = controller.run("Check Local Code Agent", self_check=True)
        print(safe_terminal(result.render()))
        return 0 if result.outcome == "pass" else 1
    gateway = ConversationGateway(OpenAICompatibleClient(chat_config), controller, events,
                                  **conversation_budgets(chat_config.context_budget_tokens))
    print("LOCAL CODE AGENT | Conversation session (prototype)")
    print(f"Repository: {repo.root}")
    print("Execution: " + ("configured commands enabled" if args.allow_execution else "disabled"))
    print("Edits/commits disabled. /check runs LCA tests. /quit exits. History is in memory only.")
    print("Endpoints must already be running. Turns are sequential; Ctrl-C ends the session.")
    while True:
        try:
            said = input("YOU > ").strip()
            if said == "/quit":
                return 0
            if said:
                print(safe_terminal(gateway.turn(said)))
        except EOFError:
            return 0
        except KeyboardInterrupt:
            print("Session interrupted. Inspect any running build/test processes before restarting.")
            return 130
        except (ValueError, RuntimeError) as exc:
            print(safe_terminal(str(exc)))


if __name__ == "__main__":
    raise SystemExit(main())
