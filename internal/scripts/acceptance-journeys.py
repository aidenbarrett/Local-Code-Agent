#!/usr/bin/env python3
"""Run the Session Hub acceptance journeys in one go and keep every log.

This drives the real Session Hub composition (``session-hub.py``'s
``compose_session_graph``) with the configured model endpoint, against fresh
copies of the bundled broken C++ repository. Each journey gets its own repository,
conversation and durable store, and a time limit.

Two kinds of journey, reported differently:

* product journeys check what must hold whatever the model is: deterministic
  build and test results, candidate isolation, /apply refusals, preserved user
  work, Stop, authority and ambiguity. They are PASS, FAIL or UNKNOWN.
* model journeys measure what the model achieved (fix a build, fix a test, make
  a change, answer questions). They are MEASURED with the outcome, and still FAIL
  if the product's claim about them is untrue.

UNKNOWN always carries its reason. Nothing is inferred from configuration.

Each run needs a new --output directory. Everything stays there: ``journeys.json``
(lca.acceptance-journeys/1), a
text ``summary.txt``, and per journey the conversation transcript and the full
durable event log. Nothing is sent anywhere.

    python internal/scripts/acceptance-journeys.py --output C:\\lca-acc\\run-001 --allow-model
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import psutil  # noqa: E402

from local_agent.config import MODEL_PRESETS, ModelConfig, load_repo_config  # noqa: E402
from local_agent.llm.client import build_client, tool_call  # noqa: E402
from local_agent.llm.protocol import ChatResponse, LLMTransportError  # noqa: E402
from local_agent.provenance import package_identity  # noqa: E402
from local_agent.session.cli import conversation_budgets  # noqa: E402
from local_agent.session.contracts import TaskOutcome, TaskResult  # noqa: E402
from local_agent.session.conversation_store import (  # noqa: E402
    conversation, create_session, ensure_runtime, new_session,
)
from local_agent.session.runtime_facts import RuntimeFacts  # noqa: E402
from serving.managed_runtime import endpoint_reachable  # noqa: E402
from serving.model_choice import ModelChoiceError, resolve_preset  # noqa: E402
from serving.model_store import default_runtime_root  # noqa: E402

SCHEMA = "lca.acceptance-journeys/1"
FIXTURE = SOURCE_ROOT / "benchmark_fixture" / "cpp_project"
RING = "src/ring_buffer.cpp"


def _load_session_hub() -> Any:
    """The one Session Hub composition, loaded from its script."""
    spec = importlib.util.spec_from_file_location("lca_session_hub", SOURCE_ROOT / "scripts" / "session-hub.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load session-hub.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["lca_session_hub"] = module
    spec.loader.exec_module(module)
    return module


HUB = _load_session_hub()


# ------------------------------------------------------------------ results


@dataclass
class Journey:
    id: str
    title: str
    kind: str  # "product" | "model"
    status: str = "UNKNOWN"
    reason: str = "not run"
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    measured: dict[str, Any] = field(default_factory=dict)
    tool_failures: list[dict[str, Any]] = field(default_factory=list)

    def passed(self, reason: str = "") -> None:
        self.status, self.reason = "PASS", reason

    def failed(self, reason: str) -> None:
        self.status, self.reason = "FAIL", reason

    def unknown(self, reason: str) -> None:
        self.status, self.reason = "UNKNOWN", reason

    def measured_as(self, outcome: str, reason: str = "") -> None:
        self.status, self.reason = f"MEASURED:{outcome}", reason


class JourneyFailed(Exception):
    """A product claim did not hold."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise JourneyFailed(message)


def _task_facts(result: TaskResult | None) -> dict[str, Any]:
    if result is None:
        return {}
    binding = result.metrics.get("proof_binding") if isinstance(result.metrics, dict) else None
    return {
        "task_id": result.task_id,
        "outcome": result.outcome.value,
        "reason_code": result.reason_code,
        "verified": result.verified_at_completion,
        "evidence_ids": list(result.evidence_ids),
        "proof_scope": binding.get("scope") if isinstance(binding, dict) else None,
    }


# ------------------------------------------------------------------ repositories


def _git(root: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def make_repo(dest: Path, scenario: str, *, allow_commit: bool = False, slow_build: bool = False) -> Path:
    """A private git repository of the fixture with one scenario committed as HEAD."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(FIXTURE, dest, ignore=shutil.ignore_patterns("build", ".local-agent"))
    subprocess.run([sys.executable, str(dest / "scripts" / "apply_scenario.py"), scenario],
                   cwd=dest, check=True, capture_output=True)
    config = dest / ".local-agent.toml"
    text = config.read_text(encoding="utf-8")
    if allow_commit:
        text = text.replace("allow_commit = false", "allow_commit = true")
    if slow_build:
        # A configured build that takes minutes, so Stop can be exercised mid-command.
        text = text.replace('default_profile = "debug"', 'default_profile = "slow"')
        text += ('\n[profiles.slow]\nconfigure = ["cmake", "-E", "echo", "configured"]\n'
                 'build = ["cmake", "-E", "sleep", "600"]\n'
                 'test = ["cmake", "-E", "sleep", "600"]\n')
    config.write_text(text, encoding="utf-8")
    _git(dest, "init", "-q")
    _git(dest, "config", "user.email", "acceptance@example.invalid")
    _git(dest, "config", "user.name", "Acceptance")
    _git(dest, "config", "core.autocrlf", "false")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-qm", f"acceptance baseline: {scenario}")
    return dest


def tree_digest(root: Path) -> str:
    """Hash of every tracked and untracked non-ignored file, plus HEAD and the index."""
    digest = hashlib.sha256()
    digest.update(_git(root, "rev-parse", "HEAD").encode())
    digest.update(_git(root, "status", "--porcelain=v1", "-uall").encode())
    for rel in sorted(_git(root, "ls-files", "-co", "--exclude-standard").splitlines()):
        path = root / rel
        if path.is_file():
            digest.update(rel.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def independent_build(root: Path, build_dir: str = "build-independent") -> tuple[bool, str]:
    """Configure and build with CMake directly; the agent's verdict is not consulted."""
    build = root / build_dir
    shutil.rmtree(build, ignore_errors=True)
    for command in (["cmake", "-S", ".", "-B", build_dir], ["cmake", "--build", build_dir, "--parallel", "4"]):
        done = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False, timeout=900)
        if done.returncode != 0:
            return False, (done.stdout + done.stderr)[-4000:]
    return True, ""


def processes_under(root: Path) -> list[str]:
    """Live processes whose command line or working directory is inside root."""
    marker = str(root.resolve()).lower()
    found = []
    for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            cwd = (proc.info.get("cwd") or "").lower()
        except (psutil.Error, OSError):
            continue
        if proc.pid != os.getpid() and (marker in cmdline or cwd.startswith(marker)):
            found.append(f"{proc.pid}: {cmdline[:160]}")
    return found


# ------------------------------------------------------------------ one session


class Session:
    """One Session Hub over one repository: the composed graph plus logging."""

    def __init__(self, runner: Runner, journey: Journey, repo: Path, *, allow_execution: bool = True) -> None:
        self.runner, self.journey, self.repo = runner, journey, repo
        self.allow_execution = allow_execution
        self.log_dir = runner.output / "journeys" / journey.id
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.transcript = (self.log_dir / "transcript.txt").open("a", encoding="utf-8")

    @contextmanager
    def open(self) -> Iterator[Session]:
        runner = self.runner
        hub_root = runner.output / "hub" / self.journey.id
        chat, worker = runner.chat_config, runner.worker_config
        budgets = conversation_budgets(chat.context_budget_tokens)
        session = new_session(runner.profile, chat.model, chat.device, budget_chars=budgets["request_chars"])
        create_session(hub_root, session)
        with conversation(hub_root, session.conversation_id) as opened:
            runtime_index = ensure_runtime(opened.session, runner.profile, chat.model, chat.device)
            service, _recovered = HUB._open_durable_service(hub_root, session.conversation_id)
            try:
                repo_config = load_repo_config(self.repo)
                HUB._emit_session_opened(service, conversation_id=session.conversation_id,
                                         repo=repo_config, recovered=False)
                self.service = service
                self.graph = HUB.compose_session_graph(
                    service, repo_config, chat, worker,
                    runtime_facts=runner.runtime_facts, opened=opened, runtime_index=runtime_index,
                    runtime_root=hub_root, allow_execution=self.allow_execution, budgets=budgets,
                    worker_client=ScriptedCompileFix if self.journey.id in SCRIPTED_WORKER else None,
                )
                yield self
            finally:
                self._dump_events()
                service.close()
                self.transcript.close()

    # -- turns

    def turn(self, text: str, *, timeout: float | None = None) -> tuple[str, TaskResult | None]:
        """One user turn. Returns the rendered answer and the result if a task ran."""
        before = self.graph.gateway.last_result
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["answer"] = self.graph.gateway.turn(text)
            except BaseException as exc:  # noqa: BLE001 - reported below, never swallowed
                box["error"] = exc

        started = time.monotonic()
        thread = threading.Thread(target=run, name=f"journey-{self.journey.id}", daemon=True)
        thread.start()
        thread.join(timeout or self.runner.timeout)
        seconds = time.monotonic() - started
        if thread.is_alive():
            self._write(text, f"(no answer after {seconds:.0f}s; timed out)")
            self.stop_latest()
            raise TimeoutError(f"{text!r} did not finish within {seconds:.0f}s")
        if "error" in box:
            self._write(text, "ERROR: " + "".join(traceback.format_exception(box["error"])))
            raise box["error"]
        result = self.graph.gateway.last_result
        result = None if result is before else result
        self._write(text, str(box.get("answer", "")), seconds, result)
        if result is not None:
            self.journey.tasks.append({"said": text, "seconds": round(seconds, 1), **_task_facts(result)})
        return str(box.get("answer", "")), result

    def turn_async(self, text: str) -> AsyncTurn:
        turn = AsyncTurn()

        def run() -> None:
            try:
                answer = self.graph.gateway.turn(text)
                self._write(text, answer)
            except BaseException as exc:  # noqa: BLE001 - recorded in the transcript
                turn.error = exc
                self._write(text, "ERROR: " + repr(exc))
        turn.thread = threading.Thread(target=run, name=f"journey-{self.journey.id}-async", daemon=True)
        turn.thread.start()
        return turn

    # -- durable facts

    def events(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after = 0
        while True:
            batch = self.service.store.replay(self.service.stream_id, after=after, limit=1000)
            if not batch:
                return out
            out.extend(batch)
            after = int(batch[-1]["sequence"])

    def admitted(self) -> list[dict[str, Any]]:
        return [e for e in self.events() if e.get("kind") == "task.admitted"]

    def wait_for(self, predicate: Callable[[dict[str, Any]], bool], timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events():
                if predicate(event):
                    return event
            time.sleep(0.5)
        return None

    def terminal(self, task_id: str) -> dict[str, Any] | None:
        record = self.service.store.task_record(task_id)
        return record if record and record.get("terminal") else None

    def stop(self, task_id: str) -> float:
        """Request Stop for one task exactly as the Hub does. Returns seconds to terminal."""
        admission = next(e for e in self.admitted() if e.get("task_id") == task_id)
        epoch = int(admission["payload"].get("execution_epoch", 0))
        started = time.monotonic()
        self.graph.task_executor.request_cancel(task_id, execution_epoch=epoch)
        while time.monotonic() - started < self.runner.stop_budget:
            if self.terminal(task_id):
                return time.monotonic() - started
            time.sleep(0.25)
        return float("inf")

    def stop_latest(self) -> None:
        admitted = self.admitted()
        if admitted and not self.terminal(admitted[-1]["task_id"]):
            try:
                self.stop(admitted[-1]["task_id"])
            except Exception as exc:  # noqa: BLE001 - best effort after a timeout; recorded
                self.journey.notes.append(f"stop after timeout failed: {exc!r}")

    # -- logs

    def _write(self, said: str, answer: str, seconds: float | None = None,
               result: TaskResult | None = None) -> None:
        stamp = time.strftime("%H:%M:%S")
        head = f"[{stamp}] USER: {said}"
        if seconds is not None:
            head += f"   ({seconds:.1f}s)"
        self.transcript.write(head + "\n")
        if result is not None:
            self.transcript.write(f"  RESULT: {json.dumps(_task_facts(result))}\n")
        self.transcript.write("  ANSWER:\n" + "\n".join("    " + line for line in answer.splitlines()) + "\n\n")
        self.transcript.flush()

    def _with_tool_failure(self, event: dict[str, Any]) -> dict[str, Any]:
        """Inline a failed tool call's retained reason and message for the log reader."""
        ref = (event.get("payload") or {}).get("result_ref") if event.get("kind") == "tool.finished" else None
        if not ref:
            return event
        return {**event, "tool_failure": json.loads(self.service.store.artifact_bytes(ref))}

    def _dump_events(self) -> None:
        try:
            with (self.log_dir / "events.jsonl").open("w", encoding="utf-8") as out:
                for event in self.events():
                    retained = self._with_tool_failure(event)
                    out.write(json.dumps(retained, sort_keys=True) + "\n")
                    if failure := retained.get("tool_failure"):
                        self.journey.tool_failures.append(failure)
        except Exception as exc:  # noqa: BLE001 - the dump must not hide the journey result
            self.journey.notes.append(f"event dump failed: {exc!r}")


# ------------------------------------------------------------------ journeys


def j_build_pass(s: Session) -> None:
    _, result = s.turn("build it")
    expect(result is not None, "build it admitted no task")
    assert result is not None
    expect(result.outcome is TaskOutcome.PASS, f"clean tree build was {result.outcome.value}/{result.reason_code}")
    expect(result.verified_at_completion, "PASS without verification")
    expect(_task_facts(result)["proof_scope"] == "full_build", "PASS not bound to a full build")
    s.journey.passed("clean tree: PASS with full-build proof, no model involved")


def j_build_fail(s: Session) -> None:
    before = (s.repo / RING).read_bytes()
    _, result = s.turn("build it")
    expect(result is not None, "build it admitted no task")
    assert result is not None
    expect((result.outcome, result.reason_code) == (TaskOutcome.FAIL, "verification_failed"),
           f"compile error reported as {result.outcome.value}/{result.reason_code}")
    expect(_task_facts(result)["proof_scope"] == "observed_build_failure", "failure not bound to the observed build")
    expect(s.graph.history.failure_kind(result.task_id) == "build", "durable failure kind is not build")
    expect((s.repo / RING).read_bytes() == before, "build changed a source file")
    s.journey.passed("compile error: FAIL/verification_failed, observed-build proof, failure kind build")


def j_tests_fail(s: Session) -> None:
    _, result = s.turn("run the tests")
    expect(result is not None, "run the tests admitted no task")
    assert result is not None
    expect((result.outcome, result.reason_code) == (TaskOutcome.FAIL, "verification_failed"),
           f"failing test reported as {result.outcome.value}/{result.reason_code}")
    tools = [e.split(":")[0] for e in result.evidence_ids]
    expect(tools == ["build_target", "run_test"], f"evidence was {tools}")
    expect(s.graph.history.failure_kind(result.task_id) == "test", "durable failure kind is not test")
    s.journey.passed("failing test: build then tests, FAIL/verification_failed, failure kind test")


def j_ambiguous_fix(s: Session) -> None:
    s.turn("build it")
    s.turn("build it")
    tasks_before = len(s.admitted())
    answer, result = s.turn("fix it")
    expect(result is None and len(s.admitted()) == tasks_before, "fix it guessed between two failures")
    s.journey.measured["answer"] = answer[:600]
    s.journey.passed("two failed tasks: fix it asked which, admitted nothing")


def j_authority(s: Session) -> None:
    before = tree_digest(s.repo)
    for request in ("push this branch to origin", "delete the src directory and commit that"):
        answer, _ = s.turn(request)
        s.journey.measured[request] = answer[:600]
        # Refused outright with the supported path named: never a work proposal.
        expect("No task was run" in answer and "reply `work`" not in answer,
               f"{request!r} was not refused outright: {answer[:200]!r}")
    expect(tree_digest(s.repo) == before, "an out-of-authority request changed the repository")
    expect(_git(s.repo, "remote").strip() == "", "a remote appeared")
    s.journey.passed("push/delete requests: repository, index and history unchanged")


def j_stop_build(s: Session) -> None:
    thread = s.turn_async("build it")
    started = s.wait_for(lambda e: e.get("kind") == "tool.started", timeout=120)
    expect(started is not None, "the configured build never started")
    assert started is not None
    time.sleep(3)
    task_id = str(started["task_id"])
    running = processes_under(s.repo)
    s.journey.measured["processes_before_stop"] = len(running)
    if not running:
        s.journey.unknown("no build process was observed running, so Stop was not exercised mid-command")
        return
    seconds = s.stop(task_id)
    s.journey.measured["stop_to_terminal_s"] = None if seconds == float("inf") else round(seconds, 2)
    expect(seconds != float("inf"), f"no terminal state within {s.runner.stop_budget:.0f}s of Stop")
    thread.join(30)
    expect(thread.error is None, f"the stopped build turn raised {thread.error!r}"[:300])
    retained = s.graph.history.result_for_task(task_id)
    s.journey.measured["terminal"] = (
        {"status": retained.status, "verdict": retained.verdict} if retained else None
    )
    expect(retained is not None, "the stopped build has no retained terminal result")
    assert retained is not None
    expect(not retained.verified_at_completion, "a stopped build was recorded as verified")
    time.sleep(5)
    alive = processes_under(s.repo)
    s.journey.measured["surviving_processes"] = alive
    expect(not alive, f"{len(alive)} process(es) still running after Stop")
    s.journey.passed(f"Stop during a 10-minute build: terminal in {seconds:.1f}s, no surviving processes")


@dataclass
class AsyncTurn:
    """A user turn running in the background, and what it raised, if anything."""

    thread: threading.Thread | None = None
    error: BaseException | None = None

    def join(self, timeout: float) -> None:
        if self.thread is not None:
            self.thread.join(timeout)


def j_stop_generation(s: Session) -> None:
    s.turn("build it")
    thread = s.turn_async("fix it")
    admitted = s.wait_for(lambda e: e.get("kind") == "task.admitted" and e["payload"].get("skill", "").startswith("fix"),
                          timeout=60)
    expect(admitted is not None, "fix it was not admitted")
    assert admitted is not None
    time.sleep(8)  # inside model generation for any model that takes longer than this
    task_id = str(admitted["task_id"])
    if s.terminal(task_id):
        s.journey.unknown("the fix finished before Stop could land (model too fast for this probe)")
        return
    seconds = s.stop(task_id)
    s.journey.measured["stop_to_terminal_s"] = None if seconds == float("inf") else round(seconds, 2)
    expect(seconds != float("inf"), f"no terminal state within {s.runner.stop_budget:.0f}s of Stop")
    thread.join(30)
    expect(thread.error is None, f"the stopped fix turn raised {thread.error!r}"[:300])
    result = s.graph.history.result_for_task(task_id)
    expect(result is not None, "the stopped fix has no retained terminal result")
    assert result is not None
    s.journey.measured["terminal"] = {"status": result.status, "verdict": result.verdict}
    expect(not result.verified_at_completion, "a stopped fix was recorded as verified")
    expect(not (result.candidate and result.candidate.retained), "a stopped fix left an appliable candidate")
    alive = processes_under(s.repo)
    expect(not alive, f"{len(alive)} process(es) still running after Stop")
    # The user carries on after a Stop. Whatever the endpoint state, the next turn must
    # get an answer (even "restart the Hub"), never a crash.
    try:
        answer, _ = s.turn("thanks. what should I try next?", timeout=s.runner.stop_budget * 5)
    except TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001 - this is the observation, recorded below
        s.journey.measured["next_turn_after_stop"] = f"raised {type(exc).__name__}: {exc}"[:400]
        raise JourneyFailed(f"the turn after Stop crashed with {type(exc).__name__}: {exc}"[:300]) from exc
    s.journey.measured["next_turn_after_stop"] = answer[:400]
    s.journey.passed(f"Stop during model work: terminal in {seconds:.1f}s, nothing retained, no processes,"
                     " and the next turn was answered")


def j_fix_build(s: Session) -> None:
    s.turn("build it")
    broken = (s.repo / RING).read_bytes()
    _, result = s.turn("fix it")
    expect(result is not None, "fix it admitted no task")
    assert result is not None
    expect((s.repo / RING).read_bytes() == broken, "preparing a candidate changed the checkout")
    s.runner.candidates["fix-build"] = (s, result)
    if result.outcome is TaskOutcome.PASS:
        expect(result.verified_at_completion, "PASS without verification")
        s.journey.measured_as("fixed", "candidate verified by a full build; not applied yet")
    else:
        expect(not result.verified_at_completion, "a non-PASS claims verification")
        s.journey.measured_as(result.outcome.value, f"model did not produce a verified fix ({result.reason_code})")


class ScriptedCompileFix:
    """A worker that knows the fixture's compile error and fixes it, with no model.

    It drives the ordinary tools (propose_patch, apply_patch, build_target,
    submit_answer) through the real controller, candidate workspace and durable
    boundary, so the candidate controls can be exercised on any machine whether or
    not a model manages the fix. It proves the product path, never the model.
    """

    _EDITS = (("++count;", "++count_;"), ("return count_ == 0 }", "return count_ == 0; }"))

    def chat(self, messages: list[dict[str, Any]], tools: Any = None,
             max_tokens: int | None = None) -> ChatResponse:  # noqa: ARG002 - LLMClient shape
        results = [m for m in messages if m.get("role") == "tool"]
        step = len(results)
        if step < 2 * len(self._EDITS):
            if step % 2 == 0:
                find, replace = self._EDITS[step // 2]
                return ChatResponse(tool_calls=[tool_call("propose_patch", {
                    "path": RING, "find": find, "replace": replace,
                    "rationale": "fixture compile error"}, f"p{step}")])
            found = re.search(r'"patch_id":\s*"([0-9a-f]+)"', str(results[-1].get("content")))
            if found is None:
                return self._finish("diagnosis", "the patch was not proposed", [])
            return ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": found.group(1)},
                                                      f"a{step}")])
        if step == 2 * len(self._EDITS):
            return ChatResponse(tool_calls=[tool_call("build_target", {}, "b")])
        return self._finish("success", "fixed the two compile errors; the full build passes",
                            [f"build_target:{step - 1}"])

    @staticmethod
    def _finish(claim: str, summary: str, evidence: list[str]) -> ChatResponse:
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": claim, "summary": summary, "evidence_ids": evidence}, "done")])


def j_candidate_lifecycle(s: Session) -> None:
    """Needs a verified candidate from the model; exercises every candidate control."""
    _, built = s.turn("build it")
    broken = (s.repo / RING).read_bytes()
    _, fixed = s.turn("fix it")
    if fixed is None or fixed.outcome is not TaskOutcome.PASS:
        s.journey.unknown("the model produced no verified candidate, so the candidate controls could not be exercised")
        return
    cid = fixed.task_id
    answer, _ = s.turn(f"/diff {cid}")
    expect(RING in answer, "/diff did not show the changed file")

    # The user's own work: an unrelated edit and an unrelated staged file.
    notes = s.repo / "NOTES.md"
    notes.write_text("my own staged notes\n", encoding="utf-8")
    _git(s.repo, "add", "NOTES.md")
    readme = s.repo / "README.md"
    readme_before = readme.read_bytes() if readme.exists() else b""
    readme.write_bytes(readme_before + b"\nmy unrelated edit\n")

    # Stale: the target file changes after the candidate was prepared.
    (s.repo / RING).write_bytes(broken + b"\n// my edit\n")
    _, refused = s.turn(f"/apply {cid}")
    expect(refused is not None and refused.outcome is not TaskOutcome.PASS, "/apply accepted a stale target")
    expect((s.repo / RING).read_bytes() == broken + b"\n// my edit\n", "a refused /apply touched my edit")
    (s.repo / RING).write_bytes(broken)

    _, applied = s.turn(f"/apply {cid}")
    expect(applied is not None and applied.outcome is TaskOutcome.PASS, "/apply failed on an unchanged target")
    ok, log = independent_build(s.repo)
    expect(ok, "the applied change does not build independently:\n" + log[-1500:])
    expect(readme.read_bytes() == readme_before + b"\nmy unrelated edit\n", "/apply touched my unrelated edit")
    expect("NOTES.md" in _git(s.repo, "diff", "--cached", "--name-only"), "/apply unstaged my staged work")

    _, undone = s.turn(f"/undo {cid}")
    expect(undone is not None and undone.outcome is TaskOutcome.PASS, "/undo failed")
    expect((s.repo / RING).read_bytes() == broken, "/undo did not restore the exact bytes")

    _, reapplied = s.turn(f"/apply {cid}")
    s.journey.measured["apply_after_undo"] = reapplied.outcome.value if reapplied else None
    if reapplied is None or reapplied.outcome is not TaskOutcome.PASS:
        # A candidate is single-use after /undo. /commit still has to be exercised, so
        # prepare a fresh candidate for the same failure and apply that one.
        s.journey.notes.append("a candidate cannot be applied again after /undo (single-use)")
        # By task id: the refused stale /apply is a failed task too, so "fix it" is ambiguous.
        _, again = s.turn(f"fix task {built.task_id}") if built is not None else ("", None)
        if again is None or again.outcome is not TaskOutcome.PASS:
            s.journey.notes.append(
                "diff, stale refusal, apply, independent build, preserved work and exact undo passed"
            )
            s.journey.unknown("no second verified candidate; /commit was not exercised")
            return
        cid = again.task_id
        _, reapplied = s.turn(f"/apply {cid}")
        expect(reapplied is not None and reapplied.outcome is TaskOutcome.PASS, "/apply of a fresh candidate failed")
    head = _git(s.repo, "rev-parse", "HEAD").strip()
    _, committed = s.turn(f"/commit {cid}")
    expect(committed is not None and committed.outcome is TaskOutcome.PASS, "/commit failed")
    files = _git(s.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split()
    expect(files == [RING], f"the commit contains {files}")
    expect(_git(s.repo, "rev-parse", "HEAD^").strip() == head, "the commit is not on the current branch")
    expect("NOTES.md" in _git(s.repo, "diff", "--cached", "--name-only"), "/commit took my staged work")
    s.journey.passed("diff, stale refusal, apply, independent build, preserved work, exact undo, re-apply, commit")


def j_fix_tests(s: Session) -> None:
    s.turn("run the tests")
    _, result = s.turn("fix it")
    expect(result is not None, "fix it admitted no task")
    assert result is not None
    if result.outcome is TaskOutcome.PASS:
        expect(result.verified_at_completion, "PASS without verification")
        s.journey.measured_as("fixed", "candidate verified by a full test run; not applied")
    else:
        expect(not result.verified_at_completion, "a non-PASS claims verification")
        s.journey.measured_as(result.outcome.value, f"no verified fix ({result.reason_code})")


def j_change(s: Session) -> None:
    before = tree_digest(s.repo)
    _, result = s.turn("change: add a one-line comment at the top of src/text_util.cpp saying what the file contains")
    expect(result is not None, "change: admitted no task")
    assert result is not None
    expect(tree_digest(s.repo) == before, "preparing a change modified the checkout")
    if result.outcome is TaskOutcome.PASS:
        answer, _ = s.turn(f"/diff {result.task_id}")
        expect("text_util.cpp" in answer, "the verified change does not touch the requested file")
        s.journey.measured_as("changed", "verified by a full test run; not applied")
    else:
        s.journey.measured_as(result.outcome.value, f"no verified change ({result.reason_code})")


def j_questions(s: Session) -> None:
    for question in ("what does this repository do?", "where is the ring buffer implemented?"):
        answer, _ = s.turn(question)
        s.journey.measured[question] = answer[:800]
    s.journey.measured_as("answered", "read the answers in the transcript")


def r_questions(s: Session) -> None:
    before = _git(s.repo, "status", "--porcelain=v1")
    for question in ("what does this repository do?", "how is this repository built and tested?",
                     "what changed on my branch?"):
        answer, _ = s.turn(question)
        s.journey.measured[question] = answer[:800]
    expect(_git(s.repo, "status", "--porcelain=v1") == before, "answering questions changed the repository")
    s.journey.measured_as("answered", "read the answers in the transcript; repository unchanged")


def r_build(s: Session) -> None:
    before = _git(s.repo, "status", "--porcelain=v1")
    _, result = s.turn("build it")
    expect(result is not None, "build it admitted no task")
    assert result is not None
    expect(_git(s.repo, "status", "--porcelain=v1") == before, "building changed tracked or unignored files")
    if result.outcome is TaskOutcome.PASS:
        expect(result.verified_at_completion, "PASS without verification")
    s.journey.measured_as(result.outcome.value, f"{result.reason_code}; source unchanged")


# (id, title, kind, needs_model, needs_build, function) for --repo: never mutates it.
REPO_JOURNEYS: list[tuple[str, str, bool, bool, Callable[[Session], None]]] = [
    ("R01-questions", "questions about your repository", True, False, r_questions),
    ("R02-build", "build it in your repository", False, True, r_build),
]


# Journeys whose worker is ScriptedCompileFix instead of the model.
SCRIPTED_WORKER = frozenset({"J13-candidate-scripted"})
# (id, title, kind, scenario, needs_model, repo options, function)
JOURNEYS: list[tuple[str, str, str, str, bool, dict[str, bool], Callable[[Session], None]]] = [
    ("J01-build-pass", "build it on a clean tree", "product", "clean", False, {}, j_build_pass),
    ("J02-build-fail", "build it on a compile error", "product", "compile_error", False, {}, j_build_fail),
    ("J03-tests-fail", "run the tests with a failing test", "product", "test_failure", False, {}, j_tests_fail),
    ("J04-ambiguous", "fix it with two failures", "product", "compile_error", False, {}, j_ambiguous_fix),
    ("J05-stop-build", "Stop during a configured build", "product", "clean", False, {"slow_build": True}, j_stop_build),
    ("J06-authority", "requests outside its authority", "product", "clean", False, {}, j_authority),
    ("J07-stop-model", "Stop during model work", "product", "compile_error", True, {}, j_stop_generation),
    ("J08-fix-build", "fix it after a failed build", "model", "compile_error", True, {}, j_fix_build),
    ("J09-candidate", "diff, stale apply, apply, undo, commit", "product", "compile_error", True,
     {"allow_commit": True}, j_candidate_lifecycle),
    ("J10-fix-tests", "fix it after a failing test", "model", "test_failure", True, {}, j_fix_tests),
    ("J11-change", "change: a small edit", "model", "clean", True, {}, j_change),
    ("J12-questions", "questions about the repository", "model", "clean", True, {}, j_questions),
    ("J13-candidate-scripted", "candidate controls with a scripted fix (no model)", "product",
     "compile_error", False, {"allow_commit": True}, j_candidate_lifecycle),
]


# ------------------------------------------------------------------ runner


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.output: Path = args.output.resolve()
        self.profile: str = args.profile
        self.timeout: float = args.journey_timeout
        self.stop_budget: float = args.stop_budget
        self.repeat: int = getattr(args, "repeat", 1)
        chat = MODEL_PRESETS[args.profile]
        if args.base_url:
            chat = replace(chat, base_url=args.base_url)
        if args.model:
            chat = replace(chat, model=args.model)
        self.chat_config = chat
        self.worker_config = chat
        self.allow_model: bool = args.allow_model
        self.explicit_endpoint = bool(args.base_url)
        self.runtime_facts: Any = None
        self.candidates: dict[str, Any] = {}
        self.preconditions: dict[str, Any] = {}

    def check_preconditions(self) -> None:
        facts: dict[str, Any] = {
            # Exactly which product source produced this evidence.
            "product": package_identity(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "git": shutil.which("git"),
            "cmake": shutil.which("cmake"),
        }
        probe = self.output / "probe"
        if facts["git"] and facts["cmake"]:
            make_repo(probe, "clean")
            ok, log = independent_build(probe)
            facts["fixture_builds"] = ok
            if not ok:
                facts["fixture_build_log_tail"] = log[-1500:]
            shutil.rmtree(probe, ignore_errors=True)
        else:
            facts["fixture_builds"] = False
        if self.allow_model:
            if self.explicit_endpoint:
                # An endpoint the user started: use it, but only if it answers.
                reachable = endpoint_reachable(self.chat_config)
                facts["model_endpoint"] = {"ok": reachable, "message": "explicit --base-url"
                                           + ("" if reachable else " is not reachable")}
            else:
                ensured = HUB.ensure_managed_runtime(self.profile, self.chat_config, HUB._runtime_root())
                facts["model_endpoint"] = {"ok": bool(ensured.ok), "message": str(ensured.message)}
            if facts["model_endpoint"]["ok"]:
                probe = model_call_probe(self.chat_config)
                facts["model_call"] = probe
                if not probe["ok"]:
                    facts["model_endpoint"] = {
                        "ok": False,
                        "message": "the endpoint answers but a model call through the product "
                                   f"client failed: {probe['error']}",
                    }
        try:
            self.runtime_facts = RuntimeFacts.observe(self.profile, self.chat_config, execution_enabled=True)
            facts["runtime_header"] = str(self.runtime_facts.header())
        except Exception as exc:  # noqa: BLE001 - recorded; journeys needing it report UNKNOWN
            facts["runtime_header"] = f"UNKNOWN ({type(exc).__name__}: {exc})"
        self.preconditions = facts

    def run_repo(self, repo: Path, *, allow_build: bool, selected: set[str] | None) -> list[Journey]:
        """Read-only journeys over the user's own repository, in place. Never mutating."""
        results = []
        for jid, title, needs_model, needs_build, fn in REPO_JOURNEYS:
            journey = Journey(jid, title, "model" if needs_model else "product")
            results.append(journey)
            if selected and jid not in selected:
                journey.unknown("not selected")
                continue
            if needs_model and not (self.allow_model and self.preconditions.get("model_endpoint", {}).get("ok")):
                journey.unknown("needs a ready model endpoint (--allow-model)")
                continue
            if needs_build and not allow_build:
                journey.unknown("building your repository needs --allow-build")
                continue
            if not (repo / ".local-agent.toml").is_file():
                journey.unknown("your repository has no .local-agent.toml (the agent refuses to guess"
                                " how to build it); add one, then rerun with --only " + jid)
                continue
            print(f"--> {jid}: {title}", flush=True)
            started = time.monotonic()
            try:
                with Session(self, journey, repo, allow_execution=needs_build).open() as session:
                    fn(session)
            except JourneyFailed as exc:
                journey.failed(str(exc))
            except TimeoutError as exc:
                journey.unknown(f"timed out: {exc}")
            except Exception as exc:  # noqa: BLE001 - one broken journey must not end the run
                journey.unknown(f"harness error: {type(exc).__name__}: {exc}")
                journey.notes.append(traceback.format_exc()[-3000:])
            journey.seconds = round(time.monotonic() - started, 1)
            print(f"    {journey.status}  {journey.reason}  ({journey.seconds}s)", flush=True)
        return results

    def run(self, selected: set[str] | None) -> list[Journey]:
        results = []
        for jid, title, kind, scenario, needs_model, options, fn in JOURNEYS:
            # A model journey measures a stochastic model: --repeat runs it N times,
            # each attempt in its own repository and logs, so a rate can be reported.
            attempts = self.repeat if kind == "model" else 1
            for attempt in range(1, attempts + 1):
                aid = jid if attempts == 1 else f"{jid}.r{attempt}"
                results.append(self._run_one(aid, title, kind, scenario, needs_model, options, fn,
                                             selected_as=jid, selected=selected))
        return results

    def _run_one(self, jid: str, title: str, kind: str, scenario: str, needs_model: bool,
                 options: dict[str, bool], fn: Callable[[Session], None], *,
                 selected_as: str, selected: set[str] | None) -> Journey:
        journey = Journey(jid, title, kind)
        if selected and selected_as not in selected:
            journey.unknown("not selected")
            return journey
        if not self.preconditions.get("fixture_builds"):
            journey.unknown("the bundled C++ repository does not build on this machine (see preconditions)")
            return journey
        if needs_model and not self.allow_model:
            journey.unknown("needs the model; run with --allow-model")
            return journey
        if needs_model and not self.preconditions.get("model_endpoint", {}).get("ok"):
            journey.unknown("the model endpoint is not ready: " + str(self.preconditions.get("model_endpoint")))
            return journey
        print(f"--> {jid}: {title}", flush=True)
        started = time.monotonic()
        repo = make_repo(self.output / "repos" / jid, scenario, **options)
        try:
            with Session(self, journey, repo).open() as session:
                fn(session)
        except JourneyFailed as exc:
            journey.failed(str(exc))
        except TimeoutError as exc:
            journey.unknown(f"timed out: {exc}")
        except Exception as exc:  # noqa: BLE001 - one broken journey must not end the run
            journey.unknown(f"harness error: {type(exc).__name__}: {exc}")
            journey.notes.append(traceback.format_exc()[-3000:])
        journey.seconds = round(time.monotonic() - started, 1)
        print(f"    {journey.status}  {journey.reason}  ({journey.seconds}s)", flush=True)
        if journey.kind == "model":
            for line in failed_tool_lines(journey):
                print("    " + line, flush=True)
        return journey


def failed_tool_lines(journey: Journey) -> list[str]:
    return [
        f"{journey.id}: {failure['tool_name']} {failure['tool_reason']} "
        + " ".join(str(failure.get("detail", "")).split())[:300]
        for failure in journey.tool_failures
    ]


def model_rates(journeys: list[Journey]) -> list[str]:
    """Per repeated model journey, how often each outcome occurred: ``changed 2/3``."""
    groups: dict[str, list[Journey]] = {}
    for journey in journeys:
        base, sep, attempt = journey.id.rpartition(".r")
        if journey.kind == "model" and sep and attempt.isdigit():
            groups.setdefault(base, []).append(journey)
    lines = []
    for base, attempts in groups.items():
        counts: dict[str, int] = {}
        for journey in attempts:
            outcome = journey.status.split(":", 1)[-1]
            counts[outcome] = counts.get(outcome, 0) + 1
        shown = ", ".join(f"{outcome} {n}/{len(attempts)}" for outcome, n in sorted(counts.items()))
        lines.append(f"{base:<22} {shown}")
    return lines


def model_call_probe(config: ModelConfig) -> dict[str, Any]:
    """One tiny chat through the product's own client before any journey runs.

    A reachable ``/models`` does not prove the product can talk to the model: the
    client can fail to build, the served id can differ, or the template can be
    rejected. Every model journey would then end ``endpoint_unavailable`` one by
    one. One call here turns that into a single precondition with the real error.
    """
    started = time.monotonic()
    try:
        build_client(config).chat([{"role": "user", "content": "Reply with the word ready."}],
                                  max_tokens=8)
    except LLMTransportError as exc:
        error = f"{exc} (transport: {exc.kind})"
    except Exception as exc:  # noqa: BLE001 - whatever fails here is the finding
        error = f"{type(exc).__name__}: {exc}"
    else:
        return {"ok": True, "seconds": round(time.monotonic() - started, 1)}
    return {"ok": False, "seconds": round(time.monotonic() - started, 1), "error": error}


def source_line(product: Any) -> str:
    if not isinstance(product, dict):
        return "UNKNOWN"
    commit = product.get("package_commit")
    dirty = product.get("package_dirty")
    state = " (uncommitted changes)" if dirty else "" if dirty is False else " (dirty: unknown)"
    return (f"commit {str(commit)[:12] if commit else 'UNKNOWN'}{state}"
            f" · source sha256 {str(product.get('source_sha256', 'UNKNOWN'))[:16]}")


def model_line(preconditions: dict[str, Any]) -> str:
    endpoint = preconditions.get("model_endpoint")
    if not isinstance(endpoint, dict):
        return "not used (run with --allow-model to include the model journeys)"
    call = preconditions.get("model_call")
    if isinstance(call, dict) and call.get("ok"):
        return f"a call through the product client answered in {call.get('seconds')}s"
    return f"NOT USABLE: {endpoint.get('message')}"


def summary_text(runner: Runner, journeys: list[Journey], report_sha: str) -> str:
    counts: dict[str, int] = {}
    for journey in journeys:
        key = journey.status.split(":")[0]
        counts[key] = counts.get(key, 0) + 1
    product = [j for j in journeys if j.kind == "product"]
    lines = [
        "LOCAL CODE AGENT ACCEPTANCE JOURNEYS",
        "",
        f"Source    {source_line(runner.preconditions.get('product'))}",
        f"Runtime   {runner.preconditions.get('runtime_header', 'UNKNOWN')}",
        f"Model     {model_line(runner.preconditions)}",
        f"Product   PASS {sum(j.status == 'PASS' for j in product)} / FAIL {sum(j.status == 'FAIL' for j in product)}"
        f" / UNKNOWN {sum(j.status == 'UNKNOWN' for j in product)}",
        "",
    ]
    for journey in journeys:
        lines.append(f"{journey.id:<22} {journey.status:<18} {journey.seconds:>7.1f}s  {journey.reason}"[:160])
    rates = model_rates(journeys)
    if rates:
        lines += ["", "Model rates (repeated model journeys)", *rates]
    failures = [line for journey in journeys for line in failed_tool_lines(journey)]
    if failures:
        lines += ["", "Failed tool calls", *failures]
    lines += ["", f"Report SHA-256 {report_sha}"]
    return "\n".join(lines) + "\n"


def _windows_execution_state() -> Callable[[int], int]:
    import ctypes
    from ctypes import wintypes

    call = ctypes.WinDLL("kernel32", use_last_error=True).SetThreadExecutionState
    call.argtypes = [wintypes.DWORD]
    call.restype = wintypes.DWORD
    return call


@contextmanager
def keep_system_awake() -> Iterator[None]:
    """Hold a Windows thread request for this run; leave power settings alone."""
    if sys.platform != "win32":
        yield
        return
    call = _windows_execution_state()
    continuous = 0x80000000
    if not call(continuous | 0x00000001):
        raise OSError("Windows refused the acceptance keep-awake request")
    try:
        yield
    finally:
        if not call(continuous):
            print("WARNING: Windows refused to release the keep-awake request", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True,
                        help="new evidence directory for this run; keep the path short on Windows")
    parser.add_argument("--profile", default=None, choices=sorted(MODEL_PRESETS),
                        help="model preset (default: your `models use` choice)")
    parser.add_argument("--base-url", help="use an already-running endpoint instead of the profile's")
    parser.add_argument("--model", help="the model id that endpoint serves (default: the profile's)")
    parser.add_argument("--allow-model", action="store_true",
                        help="run the journeys that need the model (without it only deterministic ones run)")
    parser.add_argument("--only", action="append", default=[], help="run only these journey ids")
    parser.add_argument("--repo", type=Path,
                        help="also run read-only journeys over this repository, in place (never modified)")
    parser.add_argument("--allow-build", action="store_true",
                        help="with --repo: let R02 run that repository's configured build")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run each model journey this many times and report its outcome rate")
    parser.add_argument("--journey-timeout", type=float, default=900.0, help="seconds per user turn")
    parser.add_argument("--stop-budget", type=float, default=60.0, help="seconds Stop has to reach terminal")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    try:
        args.profile = resolve_preset(args.profile, default_runtime_root()).name
    except ModelChoiceError as exc:
        parser.error(str(exc))

    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("--output already exists; choose a new directory for this run "
                     "so earlier evidence is preserved")
    with keep_system_awake():
        runner = Runner(args)
        started = time.time()
        runner.check_preconditions()
        print(json.dumps(runner.preconditions, indent=1), flush=True)
        selected = set(args.only) or None
        journeys = runner.run(selected)
        if args.repo:
            journeys += runner.run_repo(args.repo.resolve(), allow_build=args.allow_build, selected=selected)
        report = {
            "schema": SCHEMA,
            "started_unix": started,
            "finished_unix": time.time(),
            "profile": runner.profile,
            "model": runner.chat_config.model,
            "base_url": runner.chat_config.base_url,
            "preconditions": runner.preconditions,
            "journeys": [journey.__dict__ for journey in journeys],
        }
        report_path = args.output / "journeys.json"
        report_path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
        text = summary_text(runner, journeys, sha)
        (args.output / "summary.txt").write_text(text, encoding="utf-8")
        print("\n" + text)
        return 1 if any(j.status == "FAIL" for j in journeys) else 0


if __name__ == "__main__":
    raise SystemExit(main())
