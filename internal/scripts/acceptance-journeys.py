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

Compare saved runs without starting a model or build:

    .\\local-code-agent.ps1 acceptance --compare C:\\lca-acc\\old C:\\lca-acc\\new

The comparison keeps outcome denominators, product verdicts and peak context tokens.
Missing journeys and unavailable metrics stay explicit; incompatible reports are refused.
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
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import psutil  # noqa: E402

from local_agent.config import MODEL_PRESETS, ModelConfig, load_repo_config  # noqa: E402
from local_agent.llm.client import build_client, tool_call  # noqa: E402
from local_agent.llm.protocol import ChatResponse, LLMTransportError, ToolCall  # noqa: E402
from local_agent.provenance import package_identity  # noqa: E402
from local_agent.session.conversation_gateway import conversation_budgets  # noqa: E402
from local_agent.session.contracts import TaskOutcome, TaskResult  # noqa: E402
from local_agent.session.conversation_store import (  # noqa: E402
    conversation, create_session, ensure_runtime, new_session,
)
from local_agent.session.runtime_facts import RuntimeFacts  # noqa: E402
from scripts import acceptance_corpus as corpus  # noqa: E402
from scripts.acceptance_compare import ComparisonError, comparison_text  # noqa: E402
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
        # The worker's own run metrics: how hard the turn pressed on the context budget.
        **{key: _metric_int(result.metrics, key) for key in _CONTEXT_METRICS},
    }


_CONTEXT_METRICS = ("llm_calls", "prompt_tokens", "context_peak_tokens", "compactions")


def _metric_int(metrics: object, key: str) -> int | None:
    value = metrics.get(key) if isinstance(metrics, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ------------------------------------------------------------------ repositories


def _git(root: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def make_repo(dest: Path, scenario: str, *, allow_commit: bool = False, slow_build: bool = False,
              deny_test: bool = False) -> Path:
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
    if deny_test:
        text = text.replace("allow_test = true", "allow_test = false")
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


def probe_fixture_build(output: Path) -> tuple[bool, str]:
    """Prove the bundled clean fixture builds, then remove the private probe."""
    probe = output / "probe"
    try:
        make_repo(probe, "clean")
        return independent_build(probe)
    finally:
        shutil.rmtree(probe, ignore_errors=True)


CTEST_PER_TEST_SECONDS = 120


def independent_suite(root: Path, build_dir: str = "build-independent") -> tuple[bool, str]:
    """Build with CMake and run the whole CTest suite directly; the agent is not consulted.

    Every test is bounded, so a test executable that never exits (a modal crash or assert
    dialog on Windows, say) is reported by name as a failed test instead of stalling the
    run. Output goes to a file, never a pipe a lingering child could hold open.
    """
    built, log = independent_build(root, build_dir)
    if not built:
        return False, "build failed:\n" + log
    with tempfile.TemporaryFile(mode="w+b") as capture:
        done = subprocess.run(
            ["ctest", "--test-dir", build_dir, "-C", "Debug", "--output-on-failure",
             "--timeout", str(CTEST_PER_TEST_SECONDS)],
            cwd=root, stdout=capture, stderr=subprocess.STDOUT, check=False, timeout=900)
        capture.seek(0)
        output = capture.read().decode("utf-8", errors="replace")
    return done.returncode == 0, output[-4000:]


def suite_failed_only(ctest_log: str, test_name: str) -> bool:
    """Did CTest report exactly this one test as failed?"""
    _, marker, listing = ctest_log.partition("The following tests FAILED:")
    failed = re.findall(r"^\s*\d+\s*-\s*(\S+)", listing, re.M) if marker else []
    return failed == [test_name]


def changed_paths(root: Path) -> list[str]:
    """Tracked changes against HEAD plus untracked files, as repository-relative paths."""
    tracked = _git(root, "diff", "--name-only", "HEAD").split()
    untracked = _git(root, "ls-files", "--others", "--exclude-standard").split()
    return sorted(set(tracked) | set(untracked))


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
                    worker_client=SCRIPTED_WORKERS.get(self.journey.id),
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

    _PATH = RING
    _EDITS: tuple[tuple[str, str], ...] = (
        ("++count;", "++count_;"), ("return count_ == 0 }", "return count_ == 0; }"),
    )

    def chat(self, messages: list[dict[str, Any]], tools: Any = None,
             max_tokens: int | None = None) -> ChatResponse:  # noqa: ARG002 - LLMClient shape
        results = [m for m in messages if m.get("role") == "tool"]
        step = len(results)
        if step < 2 * len(self._EDITS):
            if step % 2 == 0:
                find, replace = self._EDITS[step // 2]
                return ChatResponse(tool_calls=[tool_call("propose_patch", {
                    "path": self._PATH, "find": find, "replace": replace,
                    "rationale": "seeded compile error"}, f"p{step}")])
            found = re.search(r'"patch_id":\s*"([^"]+)"', str(results[-1].get("content")))
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


class ScriptedDirtyReview(ScriptedCompileFix):
    """Read actual Git facts for inspection; reuse the compile-fix worker otherwise."""

    def chat(self, messages: list[dict[str, Any]], tools: Any = None,
             max_tokens: int | None = None) -> ChatResponse:
        if not any("what have I changed?" in str(m.get("content", ""))
                   for m in messages if m.get("role") == "user"):
            return super().chat(messages, tools, max_tokens)
        results = [m for m in messages if m.get("role") == "tool"]
        if not results:
            return ChatResponse(tool_calls=[tool_call("git_status", {}, "status")])
        if len(results) < 3:
            return ChatResponse(tool_calls=[tool_call("git_diff", {
                "staged": len(results) == 1}, f"diff{len(results)}")])
        data = json.loads(str(results[0]["content"])).get("data", {})
        states = []
        for item in data.get("changed", []):
            if item.get("original_path"):
                states.append(f"renamed: {item['original_path']} -> {item['path']}")
            if item.get("staged"):
                states.append(f"staged: {item['path']}")
            if item.get("worktree"):
                states.append(f"unstaged: {item['path']}")
        for result in results[1:3]:
            diff = json.loads(str(result["content"])).get("data", {}).get("diff", "")
            states.extend(line for line in diff.splitlines() if line.startswith("Binary files "))
        states.extend(f"untracked: {path}" for path in data.get("untracked", []))
        return self._finish("diagnosis", "; ".join(states),
                            ["git_status:0", "git_diff:1", "git_diff:2"])


class ScriptedBranchReview(ScriptedCompileFix):
    """Report what the branch facts actually say, never what the branch is assumed to be."""

    def chat(self, messages: list[dict[str, Any]], tools: Any = None,
             max_tokens: int | None = None) -> ChatResponse:
        if not any("what changed on my branch?" in str(m.get("content", ""))
                   for m in messages if m.get("role") == "user"):
            return super().chat(messages, tools, max_tokens)
        results = [m for m in messages if m.get("role") == "tool"]
        if not results:
            return ChatResponse(tool_calls=[tool_call("git_branch_info", {"base": "main"}, "branch")])
        data = json.loads(str(results[0]["content"])).get("data", {})
        where = (f"detached at {data.get('head_commit')}" if data.get("detached")
                 else f"branch: {data.get('head')}")
        facts = [where, f"upstream: {data.get('upstream') or 'none'}",
                 f"merge base: {data.get('merge_base')}"]
        facts.extend(f"{item['status']} {item['path']}" for item in data.get("files_changed") or [])
        return self._finish("diagnosis", "; ".join(facts), ["git_branch_info:0"])


class ScriptedConflictExplain(ScriptedCompileFix):
    """Report the stopped operation, conflicts and ways out exactly as git_status states them."""

    def chat(self, messages: list[dict[str, Any]], tools: Any = None,
             max_tokens: int | None = None) -> ChatResponse:
        if not any("explain this conflict" in str(m.get("content", ""))
                   for m in messages if m.get("role") == "user"):
            return super().chat(messages, tools, max_tokens)
        results = [m for m in messages if m.get("role") == "tool"]
        if not results:
            return ChatResponse(tool_calls=[tool_call("git_status", {}, "status")])
        data = json.loads(str(results[0]["content"])).get("data", {})
        commands = data.get("operation_commands") or {}
        divergence = data.get("upstream_divergence")
        facts = [f"operation: {data.get('operation') or 'none'}"]
        facts.extend(f"conflict: {item['path']} ({item['state']})" for item in data.get("conflicted", []))
        facts.extend(f"{way}: {commands[way]}" for way in ("continue", "abort") if way in commands)
        facts.append("upstream: unknown" if divergence is None
                     else f"upstream: {divergence['ahead']} ahead, {divergence['behind']} behind")
        return self._finish("diagnosis", "; ".join(facts), ["git_status:0"])


class ScriptedMalformedCalls(ScriptedCompileFix):
    """Exercise malformed model output without repairing it or trusting its claim."""

    def chat(self, messages: list[dict[str, Any]], tools: Any = None,
             max_tokens: int | None = None) -> ChatResponse:  # noqa: ARG002 - LLMClient shape
        step = sum(m.get("role") == "tool" for m in messages)
        if step == 0:
            return ChatResponse(tool_calls=[tool_call("invented_patch_tool", {}, "unknown")])
        if step == 1:
            return ChatResponse(tool_calls=[ToolCall.from_parts(
                "invalid-json", "read_file", '{"path":"src/ring_buffer.cpp"')])
        if step == 2:
            return ChatResponse(tool_calls=[tool_call(
                "apply_patch", {"patch_id": "p-does-not-exist"}, "bogus-patch")])
        return self._finish("success", "the compile error is fixed", [])


class ScriptedTestTruth(ScriptedCompileFix):
    """Ask the real test tool for one precise fact, then report its typed result."""

    modes: dict[str, int] = {}

    def chat(self, messages: list[dict[str, Any]], tools: Any = None,
             max_tokens: int | None = None) -> ChatResponse:  # noqa: ARG002 - LLMClient shape
        latest_user = max(i for i, message in enumerate(messages) if message.get("role") == "user")
        results = [message for message in messages[latest_user + 1:] if message.get("role") == "tool"]
        request = str(messages[latest_user].get("content", ""))
        referenced = re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", request)
        mode = self.modes.get(referenced.group(0) if referenced else "", 0)
        if not results:
            return ChatResponse(tool_calls=[tool_call("build_target", {}, "build")])
        if mode == 2 and len(results) == 1:
            return ChatResponse(tool_calls=[tool_call("propose_patch", {
                "path": RING, "find": '#include "sandbox/ring_buffer.hpp"',
                "replace": '#include "sandbox/ring_buffer.hpp"\n// changed after build',
                "rationale": "make the just-built binary stale",
            }, "propose")])
        if mode == 2 and len(results) == 2:
            found = re.search(r'"patch_id":\s*"([^"]+)"', str(results[-1].get("content")))
            if found is None:
                return self._finish("diagnosis", "the stale-source patch was not proposed", [])
            return ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": found.group(1)}, "apply")])
        test_step = 3 if mode == 2 else 1
        if len(results) == test_step:
            name_filter = "definitely_no_such_test" if mode == 1 else "text_util"
            return ChatResponse(tool_calls=[tool_call("run_test", {"name_filter": name_filter}, "test")])
        content = str(results[-1].get("content", ""))
        evidence = [f"{message.get('name')}:{index}" for index, message in enumerate(results)]
        return self._finish("diagnosis", content, evidence)


class ScriptedRepoExplain(ScriptedCompileFix):
    """Explain the repository from what repo_info and list_files actually returned."""

    def chat(self, messages: list[dict[str, Any]], tools: Any = None,
             max_tokens: int | None = None) -> ChatResponse:
        latest = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        request = str(messages[latest].get("content", ""))
        if "repository" not in request:
            return super().chat(messages, tools, max_tokens)
        results = [m for m in messages[latest + 1:] if m.get("role") == "tool"]
        if not results:
            return ChatResponse(tool_calls=[tool_call("repo_info", {}, "info")])
        if len(results) == 1:
            return ChatResponse(tool_calls=[tool_call("list_files", {
                "path": ".", "pattern": "*", "recursive": False}, "files")])
        info = json.loads(str(results[0]["content"])).get("data", {})
        listing = json.loads(str(results[1]["content"])).get("data", {})
        profile = info.get("profiles", {}).get(info.get("default_profile"), {})
        commands = [" ".join(profile.get(step) or []) for step in ("configure", "build", "test")]
        files = [f for f in listing.get("files", []) if isinstance(f, str)][:8]
        summary = (f"Entry files: {', '.join(files)}. "
                   f"Configure: {commands[0]}. Build: {commands[1]}. Test: {commands[2]}.")
        return self._finish("diagnosis", summary, ["repo_info:0", "list_files:1"])


class ScriptedSymbolLookup(ScriptedCompileFix):
    """Follow the public symbol route through candidate search and a bounded read."""

    def chat(self, messages: list[dict[str, Any]], tools: Any = None,
             max_tokens: int | None = None) -> ChatResponse:
        latest = max(i for i, message in enumerate(messages) if message.get("role") == "user")
        request = str(messages[latest].get("content", ""))
        if "RingBuffer::full" not in request:
            return super().chat(messages, tools, max_tokens)
        results = [message for message in messages[latest + 1:] if message.get("role") == "tool"]
        if not results:
            # Pass the symbol exactly as the user wrote it (quotes, a trailing "()"):
            # the tool must accept every spelling the public route admitted.
            asked = re.search(r"where is (\S+) (?:defined|declared|implemented)", request,
                              re.IGNORECASE)
            symbol = asked.group(1) if asked else "RingBuffer::full"
            return ChatResponse(tool_calls=[tool_call(
                "find_definition", {"symbol": symbol}, "definition")])
        if len(results) == 1:
            matches = json.loads(str(results[0]["content"])).get("data", {}).get("matches", [])
            match = next(
                (item for item in matches
                 if item.get("file") == RING and isinstance(item.get("line"), int)),
                None,
            )
            if match is None:
                return self._finish("diagnosis", "bounded lookup found no fixture definition", [])
            line = int(match["line"])
            return ChatResponse(tool_calls=[tool_call("read_file", {
                "path": RING, "start_line": max(1, line - 2), "end_line": line + 2,
            }, "read")])
        search = json.loads(str(results[0]["content"])).get("data", {}).get("matches", [])
        match = next(item for item in search if item.get("file") == RING)
        line = int(match["line"])
        return self._finish(
            "diagnosis",
            f"RingBuffer::full is defined at {RING}:{line}; the surrounding source was read.",
            ["find_definition:0", "read_file:1"],
        )


# ---------------------------------------------------------------- answer grounding
#
# What the grounding check covers, exactly (the CAP-repo-explain claim says the same):
# - any token with a directory separator ("/" or "\\") whose last segment has a
#   lettered suffix, e.g. src/real.cpp, ./a.h, ..\\b.py, C:/x.cpp, //host/share/x.py;
# - a bare file name only when its suffix is a known source/build suffix, so prose
#   such as "e.g." or "Node.js" is never mistaken for a file;
# - any token starting with "file:". It always names a location outside the
#   repository, so it is "outside" without the suffix grammar: a well-formed URI is
#   reported as the percent-decoded absolute, drive or UNC path it names (its query
#   and fragment dropped); a malformed one (bad %-escape, invalid UTF-8, NUL, no
#   absolute path) is reported as the raw token.
# A trailing ":line[:column]" or "#Lline[-Lline]" anchor is removed before the suffix
# is checked. Network URLs (_NETWORK_URL) are not file references and are skipped;
# any other scheme fails the path grammar and is not observed.
# A reference is grounded only when it resolves to a regular file inside the
# repository: traversal, absolute, drive, UNC and file: references and symlinks whose
# target leaves the repository are "outside", never grounded.
_BARE_SUFFIXES = frozenset({
    "c", "cc", "cpp", "cxx", "h", "hh", "hpp", "hxx", "ipp", "inl", "tpp", "cmake",
    "toml", "md", "txt", "py", "json", "yaml", "yml", "ini", "cfg", "sh", "ps1", "in",
})
_TOKEN = re.compile(r"[^\s`'\"<>()\[\]{},;|*]+")
_NETWORK_URL = re.compile(r"^(?:https?|ftps?|ssh|git|svn|wss?)://", re.IGNORECASE)
_FILE_SCHEME = re.compile(r"^file:", re.IGNORECASE)
_FILE_URI = re.compile(r"^file:(?://(?P<host>[^/?#]*))?(?P<path>/[^?#]*)(?:[?#].*)?$",
                       re.IGNORECASE)
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ANCHOR = re.compile(r"(?:(?::\d+){1,2}|#L\d+(?:C\d+)?(?:-L?\d+(?:C\d+)?)?)$")
_PATH_TOKEN = re.compile(r"(?:[A-Za-z]:)?/{0,2}(?:[\w.+-]+/)*[\w.+-]+")
_LETTERED_SUFFIX = re.compile(r"[^/]\.[A-Za-z][A-Za-z0-9+]*$")
# A bounded refusal signal: the answer opens with one of these forms, and nothing else
# (length included) counts. It is reported, never used to decide whether an answer is right.
_REFUSAL = re.compile(
    r"\bi\s+(?:cannot|can't|can not|am unable to|am not able to)\s+"
    r"(?:answer|tell|determine|help|find|say|explain)"
    r"|\bi\s+(?:don't|do not)\s+know\b"
    r"|\b(?:unable|not possible|impossible|no way)\s+to\s+"
    r"(?:answer|determine|explain|tell|say|know|infer)\b"
    r"|\b(?:cannot|can't|can not|could not|couldn't)\s+be\s+"
    r"(?:answered|determined|explained|inferred|identified|established|known)\b"
    r"|\bnot\s+(?:enough|sufficient)\s+(?:information|context|detail|details|data|evidence)\b"
    r"|\b(?:insufficient|inadequate)\s+(?:information|context|detail|details|data|evidence)\b"
    r"|\b(?:information|context|detail|details|data|evidence)\s+(?:is|are)\s+"
    r"(?:insufficient|inadequate|not enough|not sufficient|unavailable|missing)\b"
    r"|\bno\s+(?:information|context|details?)\s+(?:is|are)\s+(?:available|provided)\b",
    re.IGNORECASE,
)


def _file_uri_reference(token: str) -> str:
    """What a ``file:`` token names, decoded once here at the trust boundary.

    A well-formed URI becomes the absolute, drive or UNC path it names; anything
    malformed stays the raw token. Either way the reference is outside the repository.
    """
    match = _FILE_URI.match(token)
    if match is None or _BAD_ESCAPE.search(token):
        return token
    try:
        host = unquote(match["host"] or "", errors="strict")
        path = unquote(match["path"], errors="strict")
    except UnicodeDecodeError:
        return token
    if "\0" in host or "\0" in path:
        return token
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]                                   # file:///C:/x.cpp
    return f"//{host}{path}" if host and host.lower() != "localhost" else path


def path_references(answer: str) -> list[str]:
    """File references in an answer, normalised to "/" with directory scope kept."""
    found: list[str] = []
    for raw in _TOKEN.findall(answer):
        token = raw.rstrip(".:!?")
        if not token or _NETWORK_URL.match(token):
            continue
        if _FILE_SCHEME.match(token):
            reference = _file_uri_reference(token)
            if reference not in found:
                found.append(reference)
            continue
        token = _ANCHOR.sub("", token.replace("\\", "/"))   # real.cpp:12:3, real.cpp#L12-L20
        if "@" in token:
            continue
        if "/" in token:
            if not _PATH_TOKEN.fullmatch(token) or not _LETTERED_SUFFIX.search(token):
                continue
        else:
            stem, dot, suffix = token.rpartition(".")
            if not dot or not stem or suffix.lower() not in _BARE_SUFFIXES:
                continue
            if not re.fullmatch(r"[\w.+-]+", token):
                continue
        if token.startswith("./"):
            token = token[2:]
        if token not in found:
            found.append(token)
    return found


def _files_by_name(repo: Path) -> dict[str, list[Path]]:
    names: dict[str, list[Path]] = {}
    for current, dirs, files in os.walk(repo, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            names.setdefault(name, []).append(Path(current) / name)
    return names


def ground_reference(repo: Path, ref: str, names: dict[str, list[Path]]) -> str:
    """``ok``, ``missing`` or ``outside`` for one reference, contained in the repository."""
    root = repo.resolve()

    def inside_file(path: Path) -> str:
        target = path.resolve()
        if not target.is_relative_to(root):
            return "outside"
        return "ok" if target.is_file() else "missing"

    if _FILE_SCHEME.match(ref):
        return "outside"                                  # a malformed file: URI
    if "/" in ref:
        if ref.startswith("/") or re.match(r"^[A-Za-z]:/", ref):
            return "outside"
        return inside_file(repo / ref)
    verdicts = [inside_file(path) for path in names.get(ref, [])]
    if "ok" in verdicts:
        return "ok"
    return "outside" if "outside" in verdicts else "missing"


def recognised_refusal(answer: str) -> bool:
    """The answer opens with a listed refusal form.

    A bounded signal over exactly the forms in ``_REFUSAL``: reported, never used to
    certify or reject an answer's meaning. A short answer is not a refusal; with
    nothing cited it is observed as no-evidence.
    """
    return _REFUSAL.search(answer.strip()[:160]) is not None


@dataclass(frozen=True)
class AnswerObservation:
    """What can be observed deterministically about a free-text answer.

    Only these dimensions are measured: cited files are real and contained (or
    missing, or outside the repository), configured commands are present (or
    omitted), and a bounded refusal signal. Whether the answer is *correct* is not
    measurable here; it stays unknown and is read in the transcript. A real token
    in a contradictory or negative sentence is still just a real token.
    """

    grounded: tuple[str, ...]
    missing: tuple[str, ...]
    outside: tuple[str, ...]
    required_commands: tuple[str, ...]
    omitted_commands: tuple[str, ...]
    recognised_refusal: bool
    expects_citation: bool = True   # False for questions with nothing to cite (branch)

    @property
    def status(self) -> str:
        """The worst observed dimension; never a statement about correctness."""
        if self.missing or self.outside:
            return "ungrounded"
        if self.omitted_commands:
            return "command-incomplete"
        if self.grounded or self.required_commands:
            return "evidence-grounded"
        if self.recognised_refusal:
            return "recognised-refusal"
        return "no-evidence" if self.expects_citation else "uncited"

    def explain(self) -> str:
        parts = []
        if self.missing:
            parts.append(f"named files that do not exist: {list(self.missing)}")
        if self.outside:
            parts.append(f"named files outside the repository: {list(self.outside)}")
        if self.omitted_commands:
            parts.append(f"omitted configured commands: {list(self.omitted_commands)}")
        if self.recognised_refusal:
            parts.append("opens with a recognised refusal form")
        if self.expects_citation and not (self.grounded or self.required_commands):
            parts.append("cites no repository file")
        return "; ".join(parts) or "every cited file is real and in the repository"


_STATUS_ORDER = ("uncited", "evidence-grounded", "no-evidence", "recognised-refusal",
                 "command-incomplete", "ungrounded")


def observe_answer(answer: str, repo: Path, required_commands: tuple[str, ...] = (), *,
                   expects_citation: bool = True) -> AnswerObservation:
    names = _files_by_name(repo)
    verdicts = {ref: ground_reference(repo, ref, names) for ref in path_references(answer)}
    return AnswerObservation(
        grounded=tuple(ref for ref, v in verdicts.items() if v == "ok"),
        missing=tuple(ref for ref, v in verdicts.items() if v == "missing"),
        outside=tuple(ref for ref, v in verdicts.items() if v == "outside"),
        required_commands=required_commands,
        omitted_commands=tuple(c for c in required_commands if c not in answer),
        recognised_refusal=recognised_refusal(answer),
        expects_citation=expects_citation,
    )


_BUILD_QUESTION = "how is this repository built and tested?"
SEMANTIC_LIMIT = ("answer correctness is not measured deterministically: unknown, read the "
                  "transcript")


def worst(observations: list[AnswerObservation]) -> AnswerObservation:
    return max(observations, key=lambda o: _STATUS_ORDER.index(o.status))


def configured_commands(repo: Path) -> list[str]:
    """The default profile's configure/build/test commands, as an answer would print them."""
    config = load_repo_config(repo)
    profile = config.profile(config.default_profile)
    return [" ".join(cmd) for cmd in (profile.configure, profile.build, profile.test) if cmd]


def j_repo_explain(s: Session) -> None:
    """R01: explaining the repository names real files and the configured commands."""
    before = _git(s.repo, "status", "--porcelain=v1")
    for question in ("explain this repository", "how is this repository built and tested?"):
        answer, result = s.turn(question)
        expect(result is not None, f"{question!r} admitted no task")
        # J22's worker and its expected response are controlled, so this is a
        # deterministic PASS; it says nothing about free model prose (Q06).
        seen = observe_answer(answer, s.repo, tuple(configured_commands(s.repo)))
        expect(not seen.missing and not seen.outside, f"{question!r}: {seen.explain()}")
        for command in seen.omitted_commands:
            expect(False, f"{question!r}: the answer omitted the configured command {command!r}")
        expect(bool(seen.grounded or seen.required_commands) and not seen.recognised_refusal,
               f"{question!r}: {seen.explain()}")
    expect(_git(s.repo, "status", "--porcelain=v1") == before, "explaining changed the repository")
    s.journey.passed("named only existing files and every configured configure/build/test "
                     "command; repository unchanged")


def j_symbol_lookup(s: Session) -> None:
    """A public symbol question returns an observed file and line without mutation."""
    before = _git(s.repo, "status", "--porcelain=v1")
    # The plain spelling and a presentation spelling (quoted, trailing "()") go
    # through the same route, worker and real tool.
    for question in ("where is RingBuffer::full defined?",
                     "where is `RingBuffer::full()` defined?"):
        answer, result = s.turn(question)
        expect(result is not None, f"{question!r} admitted no task")
        expect(RING in answer and "RingBuffer::full" in answer,
               f"{question!r}: the answer did not name its observed source: {answer!r}")
        expect(re.search(rf"{re.escape(RING)}:\d+", answer) is not None,
               f"{question!r}: the answer did not cite an observed line: {answer!r}")
    expect(_git(s.repo, "status", "--porcelain=v1") == before,
           "symbol lookup changed the repository")
    s.journey.passed("public route found and read RingBuffer::full with a file/line citation, "
                     "plain and quoted with ()")


def dirty_work_snapshot(repo: Path, tracked: str, untracked: str) -> dict[str, bytes]:
    """Index content/modes, both diffs and owned dirty bytes, without index stat-cache noise."""
    return {
        "index": _git(repo, "ls-files", "--stage").encode(),
        "staged_diff": _git(repo, "diff", "--cached", "--binary").encode(),
        "unstaged_diff": _git(repo, "diff", "--binary", "--", tracked).encode(),
        "tracked_bytes": (repo / tracked).read_bytes(),
        "untracked_bytes": (repo / untracked).read_bytes(),
        "head": _git(repo, "rev-parse", "HEAD").encode(),
    }


def j_dirty_worktree(s: Session) -> None:
    """R04/O04: inspection and candidate import preserve all three user-work states."""
    tracked, untracked = "README.md", "private-notes.bin"
    path = s.repo / tracked
    original = path.read_bytes()
    path.write_bytes(original + b"\nmy staged edit\n")
    _git(s.repo, "add", "--", tracked)
    path.write_bytes(path.read_bytes() + b"my separate unstaged edit\n")
    (s.repo / untracked).write_bytes(b"untracked\x00private\xff\r\n")
    before = dirty_work_snapshot(s.repo, tracked, untracked)
    s.journey.measured["preserved_work_sha256"] = {
        key: hashlib.sha256(value).hexdigest() for key, value in before.items()
    }
    answer, reviewed = s.turn("what have I changed?")
    expect(reviewed is not None, "change inspection admitted no task")
    for state, filename in (("staged", tracked), ("unstaged", tracked), ("untracked", untracked)):
        expect(re.search(r"(?<!\w)" + re.escape(f"{state}: {filename}"), answer) is not None,
               f"inspection omitted {state}: {filename}")
    expect(dirty_work_snapshot(s.repo, tracked, untracked) == before,
           "inspection changed user work or the index")
    broken = (s.repo / RING).read_bytes()
    s.turn("build it")
    _, fixed = s.turn("fix it")
    expect(fixed is not None and fixed.outcome is TaskOutcome.PASS,
           "scripted worker produced no verified candidate")
    assert fixed is not None
    expect((s.repo / RING).read_bytes() == broken, "candidate preparation touched its target")
    expect(dirty_work_snapshot(s.repo, tracked, untracked) == before,
           "candidate preparation changed user work or the index")
    _, applied = s.turn(f"/apply {fixed.task_id}")
    expect(applied is not None and applied.outcome is TaskOutcome.PASS, "candidate import failed")
    expect((s.repo / RING).read_bytes() != broken, "candidate import did not change its target")
    expect(dirty_work_snapshot(s.repo, tracked, untracked) == before,
           "candidate import changed user work, index or history")
    ok, log = independent_build(s.repo)
    expect(ok, "the imported candidate fails an independent build: " + log[-1500:])
    s.journey.passed("staged, unstaged and untracked states named; inspection, preparation and apply "
                     "preserved exact user bytes, index content and history; independent build passed")


def j_rename_binary(s: Session) -> None:
    """R04b: rename identity and binary diff stay readable without altering user work."""
    old, new, binary = "README.md", "renamed notes.md", "sample.bin"
    (s.repo / binary).write_bytes(b"before\x00private-binary-marker\xff")
    _git(s.repo, "add", "--", binary)
    _git(s.repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.com",
         "commit", "-qm", "tracked binary baseline")
    _git(s.repo, "mv", "--", old, new)
    (s.repo / binary).write_bytes(b"after\x00private-binary-marker\xfe")
    before = dirty_work_snapshot(s.repo, new, binary)
    answer, result = s.turn("what have I changed?")
    expect(result is not None, "rename inspection admitted no task")
    expect(f"renamed: {old} -> {new}" in answer, "inspection lost rename identity")
    expect(f"unstaged: {binary}" in answer and "Binary files " in answer,
           "inspection omitted the binary change")
    expect("private-binary-marker" not in answer, "inspection dumped binary bytes")
    expect(dirty_work_snapshot(s.repo, new, binary) == before,
           "rename/binary inspection changed index, history or file bytes")
    expect(not (s.repo / old).exists(), "inspection restored the old rename path")
    s.journey.passed("staged rename names both paths; binary diff reports metadata only; "
                     "index content, history and file bytes preserved")


def branch_review_snapshot(repo: Path) -> dict[str, str]:
    """Everything a read-only branch review must leave alone."""
    return {"head": _git(repo, "rev-parse", "HEAD"),
            "symbolic": _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
            "index": _git(repo, "ls-files", "--stage"),
            "status": _git(repo, "status", "--porcelain=v1", "-uall"),
            "branches": _git(repo, "for-each-ref", "--format=%(refname) %(objectname) %(upstream)")}


def conflict_snapshot(repo: Path, path: str) -> dict[str, str]:
    """A read-only conflict explanation must leave the stopped operation exactly where it is."""
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").strip())
    markers = sorted(name for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                                       "rebase-merge", "rebase-apply") if (git_dir / name).exists())
    return {"head": _git(repo, "rev-parse", "HEAD"),
            "index": _git(repo, "ls-files", "--stage"),
            "status": _git(repo, "status", "--porcelain=v1", "-uall"),
            "markers": " ".join(markers),
            "conflicted_bytes": hashlib.sha256((repo / path).read_bytes()).hexdigest()}


def j_conflict_explain(s: Session) -> None:
    """G01: a stopped merge and a stopped rebase are named with git's own ways out, read-only."""
    who = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid")
    path = "notes.txt"

    def commit(text: str, message: str) -> None:
        (s.repo / path).write_bytes(text.encode())
        _git(s.repo, "add", "--", path)
        _git(s.repo, *who, "commit", "-qm", message)

    _git(s.repo, "branch", "-M", "main")
    commit("base\n", "base")
    _git(s.repo, "switch", "-q", "-c", "other")
    commit("theirs\n", "theirs")
    _git(s.repo, "switch", "-q", "main")
    commit("ours\n", "ours")

    cases = (("merge", ("merge", "other")), ("rebase", ("rebase", "main")))
    for operation, command in cases:
        if operation == "rebase":
            _git(s.repo, "merge", "--abort")
            _git(s.repo, "switch", "-q", "other")
        _git(s.repo, *who, *command, check=False)
        before = conflict_snapshot(s.repo, path)
        expect(before["markers"], f"{operation}: the fixture did not stop on a conflict")
        answer, explained = s.turn("explain this conflict")
        expect(explained is not None, f"{operation}: conflict explanation admitted no task")
        for fact in (f"operation: {operation}", f"conflict: {path} (both modified)",
                     f"continue: git {operation} --continue", f"abort: git {operation} --abort",
                     "upstream: unknown"):
            expect(fact in answer, f"{operation}: conflict explanation omitted {fact!r}")
        expect(conflict_snapshot(s.repo, path) == before,
               f"{operation}: explaining the conflict resolved, staged, continued or aborted it")
    s.journey.passed("stopped merge and rebase named with the conflicted path, its state and git's "
                     "continue/abort commands; HEAD, index, markers and conflicted bytes unchanged")


def j_branch_review(s: Session) -> None:
    """R05: the merge base, the changed files, the upstream and a detached HEAD are all named."""
    who = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid")
    _git(s.repo, "branch", "-M", "main")
    base = _git(s.repo, "rev-parse", "HEAD").strip()
    _git(s.repo, "switch", "-q", "-c", "feature")
    text_util = s.repo / "src" / "text_util.cpp"
    text_util.write_bytes(text_util.read_bytes() + b"// feature work\n")
    (s.repo / "docs").mkdir(exist_ok=True)
    (s.repo / "docs" / "notes.md").write_bytes(b"feature notes\n")
    _git(s.repo, "add", "--", "src/text_util.cpp", "docs/notes.md")
    _git(s.repo, *who, "commit", "-qm", "feature work")
    tip = _git(s.repo, "rev-parse", "HEAD").strip()
    changed = ("M src/text_util.cpp", "A docs/notes.md")

    cases = (("no upstream", None, "branch: feature", "upstream: none"),
             ("upstream", ["branch", "--set-upstream-to=main", "feature"],
              "branch: feature", "upstream: main"),
             ("detached", ["switch", "-q", "--detach", "HEAD"],
              f"detached at {tip}", "upstream: none"))
    for label, setup, where, upstream in cases:
        if setup:
            _git(s.repo, *setup)
        before = branch_review_snapshot(s.repo)
        answer, reviewed = s.turn("what changed on my branch?")
        expect(reviewed is not None, f"{label}: branch review admitted no task")
        for fact in (where, upstream, f"merge base: {base}", *changed):
            expect(fact in answer, f"{label}: branch review omitted {fact!r}")
        expect(branch_review_snapshot(s.repo) == before,
               f"{label}: branch review changed HEAD, the index, the worktree or a branch")
    s.journey.passed("merge base and changed files named on a branch without upstream, with an "
                     "upstream and on a detached HEAD; HEAD, index, worktree and refs unchanged")


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


def _prepare_applied_candidate(s: Session) -> str:
    """Create and apply one scripted verified candidate through the public Hub path."""
    _, built = s.turn("build it")
    expect(built is not None and built.outcome is TaskOutcome.FAIL, "fixture did not fail")
    _, fixed = s.turn("fix it")
    expect(fixed is not None and fixed.outcome is TaskOutcome.PASS, "no verified candidate")
    assert fixed is not None
    _, applied = s.turn(f"/apply {fixed.task_id}")
    expect(applied is not None and applied.outcome is TaskOutcome.PASS, "/apply failed")
    return fixed.task_id


def j_exact_candidate_commit(s: Session) -> None:
    """Commit only the candidate while preserving the user's index and hook boundary."""
    candidate_id = _prepare_applied_candidate(s)
    notes = s.repo / "NOTES.md"
    notes.write_text("unrelated staged work\n", encoding="utf-8")
    _git(s.repo, "add", "NOTES.md")
    staged_before = _git(s.repo, "ls-files", "--stage", "--", "NOTES.md")

    marker = s.repo / "hook-ran"
    hooks = s.repo / ".test-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho ran > {marker.as_posix()}\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(s.repo, "config", "core.hooksPath", str(hooks))

    remote = s.repo.parent / "remote.git"
    _git(s.repo.parent, "init", "--bare", "-q", str(remote))
    _git(s.repo, "remote", "add", "origin", str(remote))
    remote_before = _git(remote, "for-each-ref", "--format=%(refname):%(objectname)")
    head = _git(s.repo, "rev-parse", "HEAD").strip()

    _, committed = s.turn(f"/commit {candidate_id}")
    expect(committed is not None and committed.outcome is TaskOutcome.PASS, "/commit failed")
    files = _git(s.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split()
    expect(files == [RING], f"the commit contains {files}")
    expect(_git(s.repo, "rev-parse", "HEAD^").strip() == head, "commit has the wrong parent")
    expect(_git(s.repo, "ls-files", "--stage", "--", "NOTES.md") == staged_before,
           "/commit changed the unrelated staged entry")
    expect(not marker.exists(), "/commit ran the repository pre-commit hook")
    expect(_git(remote, "for-each-ref", "--format=%(refname):%(objectname)") == remote_before,
           "/commit pushed a ref to the remote")
    s.journey.passed("candidate-only commit; unrelated index preserved; no hook; no push")


def j_commit_policy(s: Session) -> None:
    """A public /commit remains a typed refusal when repository policy disables it."""
    candidate_id = _prepare_applied_candidate(s)
    head = _git(s.repo, "rev-parse", "HEAD").strip()
    answer, refused = s.turn(f"/commit {candidate_id}")
    expect(refused is not None and refused.outcome is TaskOutcome.BLOCKED,
           "disabled /commit was not BLOCKED")
    assert refused is not None
    expect(refused.reason_code == "policy_denied",
           f"disabled /commit reason is {refused.reason_code!r}")
    expect(_git(s.repo, "rev-parse", "HEAD").strip() == head,
           "disabled /commit changed HEAD")
    expect("disabled" in answer.lower() or "policy" in answer.lower(),
           "disabled /commit did not explain the policy refusal")
    s.journey.passed("allow_commit=false is BLOCKED/policy_denied and leaves HEAD unchanged")


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


REGRESSION_REQUEST = ("change: add a regression test to tests/test_text_util.cpp that checks split "
                      "keeps a trailing empty field: split(\"a,\", ',') must return two parts, "
                      "\"a\" and an empty string")


def j_regression_test(s: Session) -> None:
    """R11: the model's new test must fail on a seeded bug the existing suite misses.

    The candidate is verified against the correct implementation by the product. The
    runner then applies it with /apply, takes only the changed test files into a copy
    of the fixture carrying the `untested_bug` scenario, and runs the suite itself.
    """
    before = tree_digest(s.repo)
    _, result = s.turn(REGRESSION_REQUEST)
    expect(result is not None, "change: admitted no task")
    assert result is not None
    expect(tree_digest(s.repo) == before, "preparing a change modified the checkout")
    if result.outcome is not TaskOutcome.PASS:
        expect(not result.verified_at_completion, "a non-PASS claims verification")
        s.journey.measured_as(result.outcome.value, f"no verified test ({result.reason_code})")
        return
    _, applied = s.turn(f"/apply {result.task_id}")
    expect(applied is not None and applied.outcome is TaskOutcome.PASS,
           "/apply failed on an unchanged target")
    changed = changed_paths(s.repo)
    s.journey.measured["changed"] = changed
    if not changed:
        s.journey.measured_as("no_test", "the verified change touched no file")
        return
    outside = [rel for rel in changed if not rel.startswith("tests/")]
    if outside:
        s.journey.measured_as("changed_source", f"asked for a test, also changed {', '.join(outside)}")
        return
    correct, log = independent_suite(s.repo)
    expect(correct, "the applied test fails on the correct implementation:\n" + log[-1500:])
    seeded = make_repo(s.runner.output / "repos" / f"{s.journey.id}-seeded", "untested_bug")
    for rel in changed:
        target = seeded / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(s.repo / rel, target)
    passed_on_bug, seeded_log = independent_suite(seeded)
    s.journey.measured["seeded_suite"] = seeded_log[-1500:]
    if passed_on_bug:
        s.journey.measured_as("missed", "the new test also passes on the seeded bug")
        return
    expect(suite_failed_only(seeded_log, "text_util"),
           "on the seeded bug something other than the text_util test failed:\n" + seeded_log[-1500:])
    s.journey.measured_as("caught", "fails on the seeded bug, passes on the correct implementation")


def j_malformed_calls(s: Session) -> None:
    """O03: malformed calls and an unsupported success claim fail closed and durably."""
    s.turn("build it")
    before = tree_digest(s.repo)
    _, result = s.turn("fix it")
    expect(result is not None, "fix it admitted no task")
    assert result is not None
    expect(result.outcome is not TaskOutcome.PASS, "malformed calls manufactured a PASS")
    expect(not result.verified_at_completion, "malformed calls manufactured verification")
    expect(tree_digest(s.repo) == before, "malformed calls changed the checkout")
    s.journey.passed("malformed calls refused with typed durable reasons; false success did not pass")


def j_test_truth(s: Session) -> None:
    """R07/J19: filtered, empty and stale runs retain their actual denominator."""
    failures = []
    for _ in range(3):
        _, failed = s.turn("run the tests")
        expect(failed is not None and failed.outcome is TaskOutcome.FAIL,
               "fixture test failure was not observed")
        failures.append(failed.task_id)
    ScriptedTestTruth.modes = {task_id: mode for mode, task_id in enumerate(failures)}

    answer, matched = s.turn(f"fix task {failures[0]}")
    expect(matched is not None, "filtered test admitted no task")
    expect("all tests passed (1 test(s))" in answer,
           "one-test filter did not report denominator 1")

    answer, empty = s.turn(f"fix task {failures[1]}")
    expect(empty is not None and empty.outcome is not TaskOutcome.PASS,
           "zero-test run manufactured a PASS")
    expect("ran 0 tests" in answer and "Nothing was verified" in answer,
           "zero-test result did not say that nothing was verified")

    answer, stale = s.turn(f"fix task {failures[2]}")
    expect(stale is not None and stale.outcome is not TaskOutcome.PASS,
           "stale test run manufactured a PASS")
    expect("STALE:" in answer and "OLD binary" in answer,
           "stale run did not identify the old binary")
    s.journey.passed("one-test filter reported 1; zero-test and stale-binary runs were non-PASS "
                     "and said that they verified nothing")


def j_test_policy(s: Session) -> None:
    """R07/J19: repository policy denial is typed and cannot become test evidence."""
    answer, result = s.turn("run the tests")
    expect(result is not None, "policy test admitted no task")
    expect((result.outcome, result.reason_code) == (TaskOutcome.BLOCKED, "policy_denied"),
           f"policy refusal was {result.outcome.value}/{result.reason_code}")
    expect("allow_test" in answer or "policy" in answer.lower(),
           "policy refusal did not name the disabled capability")
    expect(not result.verified_at_completion, "policy refusal manufactured verification")
    s.journey.passed("allow_test=false: BLOCKED/policy_denied, named policy, no verification")


def _measure_observations(s: Session, seen: list[AnswerObservation], suffix: str = "") -> None:
    """Report observed dimensions only; correctness of model prose stays unknown."""
    s.journey.measured["invented_paths"] = sorted({r for o in seen for r in o.missing})
    s.journey.measured["outside_paths"] = sorted({r for o in seen for r in o.outside})
    s.journey.measured["omitted_commands"] = sorted({c for o in seen for c in o.omitted_commands})
    s.journey.measured["recognised_refusals"] = sum(o.recognised_refusal for o in seen)
    s.journey.measured["answers_with_no_evidence"] = sum(
        o.status == "no-evidence" for o in seen)
    s.journey.measured["semantic_correctness"] = "unknown"
    head = worst(seen)
    reason = "; ".join(o.explain() for o in seen
                       if o.status not in ("evidence-grounded", "uncited"))
    reason = (reason or "every cited file is real and in the repository") + suffix
    s.journey.measured_as(head.status, f"{reason}; {SEMANTIC_LIMIT}")


def j_questions(s: Session) -> None:
    grades = []
    for question in ("what does this repository do?", "where is the ring buffer implemented?"):
        answer, _ = s.turn(question)
        s.journey.measured[question] = answer[:800]
        grades.append(observe_answer(answer, s.repo))
    _measure_observations(s, grades)




def r_questions(s: Session) -> None:
    before = _git(s.repo, "status", "--porcelain=v1")
    grades = []
    for question in ("what does this repository do?", _BUILD_QUESTION,
                     "what changed on my branch?"):
        answer, _ = s.turn(question)
        s.journey.measured[question] = answer[:800]
        # The build question is answered only with the configured commands, as in J22/Q06.
        required = tuple(configured_commands(s.repo)) if question == _BUILD_QUESTION else ()
        grades.append(observe_answer(answer, s.repo, required,
                                     expects_citation=question != "what changed on my branch?"))
    expect(
        _git(s.repo, "status", "--porcelain=v1") == before,
        "answering questions changed the repository",
    )
    _measure_observations(s, grades, "; repository unchanged")


def r_build(s: Session) -> None:
    before = _git(s.repo, "status", "--porcelain=v1")
    _, result = s.turn("build it")
    expect(result is not None, "build it admitted no task")
    assert result is not None
    expect(_git(s.repo, "status", "--porcelain=v1") == before, "building changed tracked or unignored files")
    if result.outcome is TaskOutcome.PASS:
        expect(result.verified_at_completion, "PASS without verification")
        expect(_task_facts(result)["proof_scope"] == "full_build",
               "PASS not bound to a full build")
    s.journey.measured_as(result.outcome.value, f"{result.reason_code}; source unchanged")


def r_tests(s: Session) -> None:
    """R03: the declared tests, with test proof separate from R02's build proof (#459)."""
    before = _git(s.repo, "status", "--porcelain=v1")
    _, result = s.turn("run the tests")
    if result is None:
        raise JourneyFailed("run the tests admitted no task")
    expect(_git(s.repo, "status", "--porcelain=v1") == before,
           "testing changed tracked or unignored files")
    tools = [evidence.split(":")[0] for evidence in result.evidence_ids]
    if result.outcome is TaskOutcome.PASS:
        expect(result.verified_at_completion, "PASS without verification")
        expect(_task_facts(result)["proof_scope"] == "full_test",
               "PASS not bound to the full test run")
        expect(tools == ["build_target", "run_test"], f"test evidence was {tools}")
    s.journey.measured_as(result.outcome.value,
                          f"{result.reason_code}; evidence {', '.join(tools)}")


# (id, title, kind, needs_model, needs_build, function) for --repo: never mutates it.
REPO_JOURNEYS: list[tuple[str, str, bool, bool, Callable[[Session], None]]] = [
    ("R01-questions", "questions about your repository", True, False, r_questions),
    ("R02-build", "build it in your repository", False, True, r_build),
    ("R03-tests", "run the tests in your repository", False, True, r_tests),
]


# Product journeys with scripted workers instead of the model.
SCRIPTED_WORKERS = {"J13-candidate-scripted": ScriptedCompileFix,
                    "J21-exact-commit": ScriptedCompileFix,
                    "J21b-commit-policy": ScriptedCompileFix,
                    "J15-dirty-worktree": ScriptedDirtyReview,
                    "J15b-rename-binary": ScriptedDirtyReview,
                    "J16-malformed-calls": ScriptedMalformedCalls,
                    "J17-branch-review": ScriptedBranchReview,
                    "J19-test-truth": ScriptedTestTruth,
                    "J19b-test-policy": ScriptedTestTruth,
                    "J20-conflict-explain": ScriptedConflictExplain,
                    "J22-repo-explain": ScriptedRepoExplain,
                    "J23-symbol-lookup": ScriptedSymbolLookup}
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
    ("J14-regression-test", "write a regression test that catches a seeded bug", "model", "clean",
     True, {}, j_regression_test),
    ("J15b-rename-binary", "inspect staged rename and modified binary", "product",
     "clean", False, {}, j_rename_binary),
    ("J15-dirty-worktree", "preserve staged, unstaged and untracked work", "product",
     "compile_error", False, {}, j_dirty_worktree),
    ("J16-malformed-calls", "refuse malformed model tool calls", "product",
     "compile_error", False, {}, j_malformed_calls),
    ("J17-branch-review", "review my branch: merge base, upstream, detached HEAD", "product",
     "clean", False, {}, j_branch_review),
    ("J19-test-truth", "filtered, empty and stale test-run truth", "product",
     "test_failure", False, {}, j_test_truth),
    ("J19b-test-policy", "typed refusal when repository policy disables tests", "product",
     "clean", False, {"deny_test": True}, j_test_policy),
    ("J20-conflict-explain", "explain a stopped merge or rebase without touching it", "product",
     "clean", False, {}, j_conflict_explain),
    ("J21-exact-commit", "commit only an applied candidate and preserve the index", "product",
     "compile_error", False, {"allow_commit": True}, j_exact_candidate_commit),
    ("J21b-commit-policy", "typed refusal when repository policy disables commit", "product",
     "compile_error", False, {}, j_commit_policy),
    ("J22-repo-explain", "explain the repository and how it is built, with real files only",
     "product", "clean", False, {}, j_repo_explain),
    ("J23-symbol-lookup", "find a C++ symbol through the public repository route",
     "product", "clean", False, {}, j_symbol_lookup),
    ("J13-candidate-scripted", "candidate controls with a scripted fix (no model)", "product",
     "compile_error", False, {"allow_commit": True}, j_candidate_lifecycle),
]


# ------------------------------------------------------------------ real repositories (#418)

# Q journeys run on a pinned third-party repository from internal/acceptance/corpus.toml,
# each in a disposable clone of the qualification cache (never the cache itself).
CORPUS_COMPILE_PATH = "test/options.cpp"


class ScriptedCorpusCompileFix(ScriptedCompileFix):
    """Fix the cxxopts seeded compile fault with ordinary tools and no model."""

    _PATH = CORPUS_COMPILE_PATH
    _EDITS = (('CHECK(reslt.count("s") == 1);', 'CHECK(result.count("s") == 1);'),)


# The harness's own builds run outside Local Code Agent's process ownership, so they must
# not leave anything behind either: MSBuild otherwise keeps reusable worker nodes (and
# the VS telemetry helper) alive after the build, which a CI runner then reports as
# orphans that are not the product's (#438 review).
_INDEPENDENT_ENV = {"MSBUILDDISABLENODEREUSE": "1", "VSCMD_SKIP_SENDTELEMETRY": "1"}
# Short on purpose: cxxopts' package tests nest try-compile projects several levels
# under the build directory, and MSBuild's file tracker fails past MAX_PATH (FTK1011).
INDEPENDENT_BUILD_DIR = "build-i"  # matches upstream .gitignore build-*/


def _independent_commands(repo: Path) -> list[list[str]]:
    """The repository's own configured configure/build/test commands, aimed at a
    separate build directory, so the check is independent of the agent's run but
    uses the same configuration (build type, options) the repository declares."""
    config = load_repo_config(repo)
    profile = config.profile(config.default_profile)
    return [[INDEPENDENT_BUILD_DIR if arg == config.build_dir else arg for arg in command]
            for command in (profile.configure, profile.build, profile.test) if command]


def independent_profile_suite(repo: Path) -> tuple[bool, str]:
    """Configure, build and test with the repository's own profile; the agent is not
    consulted. Every test is bounded; output goes to a file, never a pipe."""
    shutil.rmtree(repo / INDEPENDENT_BUILD_DIR, ignore_errors=True)
    env = {**os.environ, **_INDEPENDENT_ENV}
    log = ""
    for configured in _independent_commands(repo):
        executable = shutil.which(configured[0])
        if executable is None:
            return False, f"{configured[0]!r} is not on PATH"
        bound = (["--timeout", str(CTEST_PER_TEST_SECONDS)]
                 if Path(configured[0]).name.lower().startswith("ctest") else [])
        with tempfile.TemporaryFile(mode="w+b") as capture:
            done = subprocess.run(  # noqa: S603 - the repository's configured argv, resolved
                [executable, *configured[1:], *bound], cwd=repo, env=env, stdout=capture,
                stderr=subprocess.STDOUT, check=False, timeout=1800)
            capture.seek(0)
            log = capture.read().decode("utf-8", errors="replace")
        if done.returncode != 0:
            return False, log[-4000:]
    return True, log[-4000:]


def ctest_failed_names(log: str) -> list[str]:
    listing = log.partition("The following tests FAILED:")[2]
    return sorted(re.findall(r"^\s*\d+\s*-\s*(\S+)", listing, re.M))


def _corpus_fault(s: Session, kind: str) -> corpus.Fault:
    if s.runner.corpus is None:
        raise JourneyFailed("a corpus journey ran without a corpus")
    return s.runner.corpus.fault_of_kind(kind)


def _task(result: TaskResult | None, said: str) -> TaskResult:
    """The task a turn admitted; a turn that admitted none fails the journey."""
    if result is None:
        raise JourneyFailed(f"{said!r} admitted no task")
    return result


def _outcome(result: TaskResult | None) -> str:
    return f"{result.outcome.value}/{result.reason_code}" if result is not None else "no task"


def q_build_and_test(s: Session) -> None:
    """Q01: a clean real repository builds and its whole CTest suite passes."""
    cold = time.monotonic()
    built = _task(s.turn("build it")[1], "build it")
    s.journey.measured["cold_build_s"] = round(time.monotonic() - cold, 1)
    expect(built.outcome is TaskOutcome.PASS, f"clean build was {_outcome(built)}")
    expect(built.verified_at_completion, "build PASS without verification")
    expect(_task_facts(built)["proof_scope"] == "full_build", "PASS not bound to a full build")
    warm = time.monotonic()
    tested = _task(s.turn("run the tests")[1], "run the tests")
    s.journey.measured["warm_test_s"] = round(time.monotonic() - warm, 1)
    expect(tested.outcome is TaskOutcome.PASS, f"clean test run was {_outcome(tested)}")
    expect(tested.verified_at_completion, "test PASS without verification")
    ok, log = independent_profile_suite(s.repo)
    expect(ok, "the clean baseline is not green under the repository's own profile "
               f"(failed: {ctest_failed_names(log)}):\n" + log[-1500:])
    s.runner.corpus_baseline_green = True
    s.journey.measured["unrelated_work_preserved"] = True
    s.journey.passed("clean real repository: build and full CTest suite PASS with proof; "
                     "an independent run of the same profile is green")


def q_compile_diagnosis(s: Session) -> None:
    """Q02: a seeded compile error is reported with the right file and line."""
    fault = _corpus_fault(s, "compile")
    before = (s.repo / CORPUS_COMPILE_PATH).read_bytes()
    answer, admitted = s.turn("build it")
    result = _task(admitted, "build it")
    expect((result.outcome, result.reason_code) == (TaskOutcome.FAIL, "verification_failed"),
           f"compile error reported as {_outcome(result)}")
    expect(s.graph.history.failure_kind(result.task_id) == "build", "failure kind is not build")
    # The user reads the answer, so that is where the location must be (GNU and MSVC
    # alike: the product prints it repository-relative as path:line).
    for location in fault.expect:
        expect(location in answer, f"the answer did not name {location}: {answer[-400:]!r}")
    expect((s.repo / CORPUS_COMPILE_PATH).read_bytes() == before, "building changed a source file")
    s.journey.measured["answer"] = answer[:600]
    s.journey.measured["unrelated_work_preserved"] = True
    s.journey.passed(f"seeded compile error: FAIL with diagnosis at {', '.join(fault.expect)}")


def q_test_truth(s: Session) -> None:
    """Q03: a seeded failing assertion is reported as exactly the failing CTest tests."""
    fault = _corpus_fault(s, "test")
    answer, admitted = s.turn("run the tests")
    result = _task(admitted, "run the tests")
    expect((result.outcome, result.reason_code) == (TaskOutcome.FAIL, "verification_failed"),
           f"failing test reported as {_outcome(result)}")
    expect(s.graph.history.failure_kind(result.task_id) == "test", "failure kind is not test")
    named = [name for name in fault.expect if name in answer]
    s.journey.measured["answer"] = answer[:800]
    expect(named == list(fault.expect),
           f"the answer named {named} of the failing tests {fault.expect}")
    ok, log = independent_profile_suite(s.repo)
    expect(not ok, "the independent CTest run passed on a seeded failing test")
    failed = ctest_failed_names(log)
    expect(failed == sorted(fault.expect),
           f"independent CTest failed {failed}, expected exactly {sorted(fault.expect)}")
    if not s.runner.corpus_baseline_green:
        # The exact seeded delta needs a green baseline from the same machine and run.
        s.journey.unknown(f"the seeded failure is exactly {', '.join(fault.expect)}, but no "
                          "green clean baseline (Q01) was observed in this run")
        return
    s.journey.measured["unrelated_work_preserved"] = True
    s.journey.passed(f"seeded test failure: FAIL naming exactly {', '.join(fault.expect)}")


UNRELATED_NAME = "note[1] é.txt"


def q_candidate_lifecycle(s: Session) -> None:
    """Q04: scripted candidate fix; apply, undo, re-apply and exact commit amid user work."""
    path = s.repo / CORPUS_COMPILE_PATH
    broken = path.read_bytes()
    built = _task(s.turn("build it")[1], "build it")
    expect(built.outcome is TaskOutcome.FAIL, "the seeded fault did not fail")
    fixed = _task(s.turn("fix it")[1], "fix it")
    expect(fixed.outcome is TaskOutcome.PASS, f"the scripted fix gave {_outcome(fixed)}")
    cid = fixed.task_id

    # The user's own work, present for every control: staged, unstaged and an untracked
    # file whose name a Git pathspec would treat as a pattern.
    (s.repo / "STAGED.md").write_text("my staged notes\n", encoding="utf-8")
    _git(s.repo, "add", "STAGED.md")
    readme = s.repo / "README.md"
    readme_before = readme.read_bytes()
    readme.write_bytes(readme_before + b"\nmy unrelated edit\n")
    odd = s.repo / UNRELATED_NAME
    odd.write_text("untracked, pattern-like name\n", encoding="utf-8")
    staged = _git(s.repo, "ls-files", "--stage", "--", "STAGED.md")

    def user_work_intact() -> bool:
        return (readme.read_bytes() == readme_before + b"\nmy unrelated edit\n"
                and odd.read_text(encoding="utf-8") == "untracked, pattern-like name\n"
                and _git(s.repo, "ls-files", "--stage", "--", "STAGED.md") == staged)

    _, applied = s.turn(f"/apply {cid}")
    expect(applied is not None and applied.outcome is TaskOutcome.PASS, "/apply failed")
    ok, log = independent_profile_suite(s.repo)
    expect(ok, "the applied fix is not green under the repository's own profile:\n"
               + log[-1500:])
    expect(user_work_intact(), "/apply touched unrelated user work")

    _, undone = s.turn(f"/undo {cid}")
    expect(undone is not None and undone.outcome is TaskOutcome.PASS, "/undo failed")
    expect(path.read_bytes() == broken, "/undo did not restore the exact bytes")
    expect(user_work_intact(), "/undo touched unrelated user work")

    _, reapplied = s.turn(f"/apply {cid}")
    if reapplied is None or reapplied.outcome is not TaskOutcome.PASS:
        s.journey.notes.append("a candidate is single-use after /undo; a fresh one is prepared")
        again = _task(s.turn(f"fix task {built.task_id}")[1], "fix task")
        expect(again.outcome is TaskOutcome.PASS, "no second verified candidate")
        cid = again.task_id
        _, reapplied = s.turn(f"/apply {cid}")
        expect(reapplied is not None and reapplied.outcome is TaskOutcome.PASS, "re-apply failed")
    head = _git(s.repo, "rev-parse", "HEAD").strip()
    _, committed = s.turn(f"/commit {cid}")
    expect(committed is not None and committed.outcome is TaskOutcome.PASS, "/commit failed")
    files = _git(s.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split()
    expect(files == [CORPUS_COMPILE_PATH], f"the commit contains {files}")
    expect(_git(s.repo, "rev-parse", "HEAD^").strip() == head, "the commit has the wrong parent")
    expect(user_work_intact(), "/commit touched unrelated user work")
    s.journey.measured["unrelated_work_preserved"] = True
    s.journey.passed("scripted fix on a real repository: apply, independent build, exact undo, "
                     "re-apply, exact commit; staged, unstaged and pattern-named work preserved")


def q_stop_build(s: Session) -> None:
    """Q05: Stop during the real repository's build: terminal in budget, no strays,
    and the next task runs."""
    thread = s.turn_async("build it")
    started = s.wait_for(lambda e: e.get("kind") == "tool.started", timeout=120)
    if started is None:
        raise JourneyFailed("the build never started")
    task_id = str(started["task_id"])
    deadline = time.monotonic() + 60
    running = processes_under(s.repo)
    while not running and time.monotonic() < deadline and not s.terminal(task_id):
        time.sleep(0.2)
        running = processes_under(s.repo)
    s.journey.measured["processes_before_stop"] = len(running)
    if not running:
        s.journey.unknown("the build finished before a running process was observed; Stop was "
                          "not exercised mid-command")
        return
    seconds = s.stop(task_id)
    finite = seconds != float("inf")
    s.journey.measured["stop_to_terminal_s"] = round(seconds, 2) if finite else None
    expect(finite, f"no terminal state within {s.runner.stop_budget:.0f}s of Stop")
    thread.join(30)
    expect(thread.error is None, f"the stopped build turn raised {thread.error!r}"[:300])
    retained = s.graph.history.result_for_task(task_id)
    expect(retained is not None and not retained.verified_at_completion,
           "the stopped build has no unverified terminal result")
    time.sleep(3)
    alive = processes_under(s.repo)
    s.journey.measured["surviving_processes"] = alive
    expect(not alive, f"{len(alive)} process(es) still running after Stop")
    after = _task(s.turn("build it")[1], "build it")
    expect(after.outcome is TaskOutcome.PASS, f"the next build after Stop was {_outcome(after)}")
    s.journey.measured["unrelated_work_preserved"] = True
    s.journey.passed(f"Stop during the real build: terminal in {seconds:.1f}s, no surviving "
                     "processes, the next build passed")


def q_repo_explain(s: Session) -> None:
    """Q06 (model): R01/J22 grounded explanation of a real repository."""
    before = _git(s.repo, "status", "--porcelain=v1")
    grades = []
    for question in ("explain this repository", _BUILD_QUESTION):
        answer, _ = s.turn(question)
        s.journey.measured[question] = answer[:800]
        grades.append(observe_answer(answer, s.repo, tuple(configured_commands(s.repo))))
    expect(_git(s.repo, "status", "--porcelain=v1") == before, "explaining changed the repository")
    s.journey.measured["unrelated_work_preserved"] = True
    # Model prose is not authority: this is never counted as completed/verified.
    _measure_observations(s, grades)


def q_model_fix(s: Session) -> None:
    """Q07 (model): fix the seeded compile error through the model worker."""
    built = _task(s.turn("build it")[1], "build it")
    expect(built.outcome is TaskOutcome.FAIL, "the seeded fault did not fail")
    _, fixed = s.turn("fix it")
    s.journey.measured["unrelated_work_preserved"] = True
    if fixed is None:
        s.journey.measured_as("unknown", "fix it admitted no task")
        return
    if fixed.outcome is TaskOutcome.PASS:
        expect(fixed.verified_at_completion, "a PASS fix without verification")
        s.journey.measured_as("verified", "candidate fix verified by a full build")
    else:
        s.journey.measured_as("failed" if fixed.outcome is TaskOutcome.FAIL else "unknown",
                              f"{fixed.outcome.value}/{fixed.reason_code}")


# (id, title, kind, fault kind or None, needs_model, function)
CORPUS_JOURNEYS: list[tuple[str, str, str, str | None, bool, Callable[[Session], None]]] = [
    ("Q01-build-test", "build and test a real repository", "product", None, False,
     q_build_and_test),
    ("Q02-compile-diagnosis", "diagnose a seeded compile error with file and line", "product",
     "compile", False, q_compile_diagnosis),
    ("Q03-test-truth", "name the seeded failing CTest tests", "product", "test", False,
     q_test_truth),
    ("Q04-candidate-scripted", "scripted fix: apply, undo, re-apply, exact commit amid user work",
     "product", "compile", False, q_candidate_lifecycle),
    ("Q05-stop-build", "Stop during the real build", "product", None, False, q_stop_build),
    ("Q06-repo-explain", "grounded explanation of a real repository", "model", None, True,
     q_repo_explain),
    ("Q07-fix-model", "fix the seeded compile error with the model", "model", "compile", True,
     q_model_fix),
]
SCRIPTED_WORKERS["Q04-candidate-scripted"] = ScriptedCorpusCompileFix


def corpus_metrics(journeys: list[Journey]) -> dict[str, Any]:
    """Astra's qualification counts. Counts only: no rate from fewer than 10 attempts."""
    ran = [j for j in journeys if j.reason != "not selected"]
    # Only proved outcomes count. Free model prose (Q06's evidence-grounded) is an
    # observation whose correctness is unknown, never a completed verification.
    verified = [j for j in ran if j.status in ("PASS", "MEASURED:verified")]
    refusals = [j for j in ran if j.status == "UNKNOWN" or j.status.startswith("MEASURED:unknown")]
    return {
        "attempted": len(ran),
        "completed_verified": len(verified),
        "correct_refusals_or_unknowns": len(refusals),
        "user_interventions": 0,
        "cold_build_s": next((j.measured.get("cold_build_s") for j in ran
                              if "cold_build_s" in j.measured), None),
        "warm_test_s": next((j.measured.get("warm_test_s") for j in ran
                             if "warm_test_s" in j.measured), None),
        "unrelated_work_preserved": {j.id: j.measured.get("unrelated_work_preserved") for j in ran},
        "failure_reasons": {j.id: j.reason for j in ran if j.status not in ("PASS",)
                            and not j.status.startswith("MEASURED:verified")},
        "rate_claimed": False,
    }


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
        # A real repository from internal/acceptance/corpus.toml (#418), or None.
        self.corpus: corpus.CorpusRepository | None = getattr(args, "corpus_entry", None)
        self.corpus_cache: Path | None = getattr(args, "corpus_cache", None)
        # Set by Q01 when the clean repository is green under its own profile here.
        self.corpus_baseline_green = False

    def check_preconditions(self) -> None:
        facts: dict[str, Any] = {
            # Exactly which product source produced this evidence.
            "product": package_identity(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "git": shutil.which("git"),
            "cmake": shutil.which("cmake"),
        }
        if self.corpus is not None:
            # Corpus journeys never use the bundled fixture; its build is not a precondition.
            facts["fixture_builds"] = None
        elif facts["git"] and facts["cmake"]:
            ok, log = probe_fixture_build(self.output)
            facts["fixture_builds"] = ok
            if not ok:
                facts["fixture_build_log_tail"] = log[-1500:]
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
        self._execute(journey, repo, fn, started)
        return journey

    def _execute(self, journey: Journey, repo: Path, fn: Callable[[Session], None],
                 started: float) -> None:
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

    def _corpus_problem(self) -> tuple[Path | None, str | None]:
        """The verified cached copy, or why corpus journeys cannot run (UNKNOWN)."""
        if self.corpus is None or self.corpus_cache is None:
            return None, "no corpus selected"
        try:
            cached = corpus.cached_copy(self.corpus, self.corpus_cache)
            if cached is None:
                return None, (f"corpus {self.corpus.name} not fetched; run `acceptance --corpus "
                              f"{self.corpus.name} --fetch` while online")
            corpus.check_faults(self.corpus, cached)
        except corpus.CorpusError as exc:
            return None, f"corpus unusable: {exc}"
        return cached, None

    def run_corpus(self, selected: set[str] | None) -> list[Journey]:
        """Q journeys on the selected real repository, each in a disposable clone."""
        if self.corpus is None:
            raise RuntimeError("run_corpus needs --corpus")
        cached, problem = self._corpus_problem()
        self.preconditions["corpus"] = {
            "name": self.corpus.name, "commit": self.corpus.commit, "licence": self.corpus.licence,
            "cache": str(self.corpus_cache), "ready": problem is None, "problem": problem,
        }
        results = []
        model_ok = self.allow_model and self.preconditions.get("model_endpoint", {}).get("ok")
        for jid, title, kind, fault_kind, needs_model, fn in CORPUS_JOURNEYS:
            attempts = self.repeat if kind == "model" else 1
            for attempt in range(1, attempts + 1):
                aid = jid if attempts == 1 else f"{jid}.r{attempt}"
                journey = Journey(aid, title, kind)
                results.append(journey)
                if selected and jid not in selected:
                    journey.unknown("not selected")
                elif problem is not None or cached is None:
                    journey.unknown(problem or "corpus not available")
                elif needs_model and not self.allow_model:
                    journey.unknown("needs the model; run with --allow-model")
                elif needs_model and not model_ok:
                    journey.unknown("the model endpoint is not ready: "
                                    + str(self.preconditions.get("model_endpoint")))
                else:
                    sys.stdout.write(f"--> {aid}: {title}\n")
                    sys.stdout.flush()
                    started = time.monotonic()
                    fault = self.corpus.fault_of_kind(fault_kind) if fault_kind else None
                    repo = corpus.disposable_copy(self.corpus, cached, self.output / "repos" / aid,
                                                  fault=fault)
                    self._execute(journey, repo, fn, started)
        return results


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


def context_use_lines(journeys: list[Journey], budget_tokens: int) -> list[str]:
    """Per model journey: model calls, peak context against the budget, compactions."""
    lines = []
    for journey in journeys:
        if journey.kind != "model":
            continue
        peaks = [t["context_peak_tokens"] for t in journey.tasks
                 if isinstance(t.get("context_peak_tokens"), int)]
        if not peaks:
            continue
        calls = sum(t.get("llm_calls") or 0 for t in journey.tasks)
        compactions = sum(t.get("compactions") or 0 for t in journey.tasks)
        peak = max(peaks)
        share = f"{round(100 * peak / budget_tokens)}%" if budget_tokens > 0 else "?"
        lines.append(
            f"{journey.id:<22} {calls} model calls · peak context {peak:,}/{budget_tokens:,}"
            f" tokens ({share}) · {compactions} compactions"
        )
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


def corpus_summary_lines(repo: corpus.CorpusRepository, metrics: dict[str, Any]) -> list[str]:
    preserved = metrics["unrelated_work_preserved"]
    lines = [
        f"Real repository {repo.name} @ {repo.commit[:12]} ({repo.licence})",
        f"  completed verified {metrics['completed_verified']} of {metrics['attempted']} "
        "attempted; "
        f"correct refusals/unknowns {metrics['correct_refusals_or_unknowns']}; "
        f"user interventions {metrics['user_interventions']}",
        "  cold build " + (f"{metrics['cold_build_s']}s" if metrics["cold_build_s"] is not None
                           else "not measured")
        + ", warm test " + (f"{metrics['warm_test_s']}s" if metrics["warm_test_s"] is not None
                            else "not measured"),
        "  unrelated work preserved: " + ", ".join(
            f"{jid}={'yes' if ok else ('no' if ok is False else 'not run')}"
            for jid, ok in preserved.items()),
        "  counts only: no success rate is claimed from fewer than 10 attempts",
    ]
    for jid, reason in metrics["failure_reasons"].items():
        lines.append(f"  {jid}: {reason}"[:160])
    return lines


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
    measured = any(isinstance(task.get("context_peak_tokens"), int)
                   for journey in journeys if journey.kind == "model" for task in journey.tasks)
    context = (context_use_lines(journeys, runner.worker_config.context_budget_tokens)
               if measured else [])
    if context:
        lines += ["", "Context use (model journeys)", *context]
    failures = [line for journey in journeys for line in failed_tool_lines(journey)]
    if failures:
        lines += ["", "Failed tool calls", *failures]
    real_repository = getattr(runner, "corpus", None)
    if real_repository is not None:
        lines += ["", *corpus_summary_lines(real_repository, corpus_metrics(journeys))]
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


def _write_report(output: Path, runner: Runner, journeys: list[Journey], started: float) -> int:
    """Write journeys.json and summary.txt; the exit code is 1 when any claim failed."""
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
    if runner.corpus is not None:
        report["corpus"] = {"name": runner.corpus.name, "commit": runner.corpus.commit,
                            "licence": runner.corpus.licence,
                            "metrics": corpus_metrics(journeys)}
    report_path = output / "journeys.json"
    report_path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    text = summary_text(runner, journeys, sha)
    (output / "summary.txt").write_text(text, encoding="utf-8")
    print("\n" + text)
    return 1 if any(j.status == "FAIL" for j in journeys) else 0


def _prepare_corpus_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int | None:
    """Resolve --corpus/--corpus-cache; with --fetch, fetch and return the exit code."""
    if args.corpus is None:
        parser.error("--fetch needs --corpus NAME")
    try:
        entries = corpus.load_manifest()
    except corpus.CorpusError as exc:
        parser.error(str(exc))
    if args.corpus not in entries:
        known = ", ".join(sorted(entries))
        parser.error(f"--corpus {args.corpus!r} is not in the manifest ({known})")
    args.corpus_entry = entries[args.corpus]
    if args.corpus_cache is None:
        args.corpus_cache = corpus.cache_root(default_runtime_root())
    args.corpus_cache = args.corpus_cache.resolve()
    if not args.fetch:
        return None
    try:
        fetched = corpus.fetch(args.corpus_entry, args.corpus_cache)
        corpus.check_faults(args.corpus_entry, fetched)
    except (corpus.CorpusError, OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"fetch failed: {exc}\n")
        return 2
    sys.stdout.write(f"{args.corpus} {args.corpus_entry.commit} is cached at {fetched}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("OLD", "NEW"),
                        help="compare two retained output directories; no model or build runs")
    parser.add_argument("--output", type=Path,
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
                        help="with --repo: let R02/R03 run its configured build and tests")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run each model journey this many times and report its outcome rate")
    parser.add_argument("--journey-timeout", type=float, default=900.0, help="seconds per user turn")
    parser.add_argument("--stop-budget", type=float, default=60.0, help="seconds Stop has to reach terminal")
    parser.add_argument("--corpus", help="run the Q journeys on this real repository from "
                        "internal/acceptance/corpus.toml instead of the bundled fixture")
    parser.add_argument("--fetch", action="store_true",
                        help="with --corpus: clone it at its pinned commit into the qualification "
                             "cache (online), then exit")
    parser.add_argument("--corpus-cache", type=Path,
                        help="qualification cache directory (default: under the runtime root)")
    args = parser.parse_args(argv)
    if args.corpus is not None or args.fetch:
        fetched = _prepare_corpus_args(parser, args)
        if fetched is not None:
            return fetched
    if args.compare:
        if args.output is not None:
            parser.error("--compare cannot be combined with --output")
        try:
            print(comparison_text(*args.compare, SCHEMA), end="")
        except ComparisonError as exc:
            parser.error(str(exc))
        return 0
    if args.output is None:
        parser.error("--output is required unless --compare is used")
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
        journeys = (runner.run_corpus(selected) if runner.corpus is not None
                    else runner.run(selected))
        if args.repo:
            journeys += runner.run_repo(args.repo.resolve(), allow_build=args.allow_build, selected=selected)
        return _write_report(args.output, runner, journeys, started)


if __name__ == "__main__":
    raise SystemExit(main())
