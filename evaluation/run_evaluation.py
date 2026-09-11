#!/usr/bin/env python3
"""Run the eval suite against a live model server.

    python evaluation/run_evaluation.py --profile nuc-cpu-30b
    python evaluation/run_evaluation.py --profile ptl-npu-8b --repeat 3
    python evaluation/run_evaluation.py --base-url http://127.0.0.1:8000/v3 --model Qwen3-8B

Records wall-clock time and tool-call count per case as well as the score, so
"the 8B is nearly as good" can be checked rather than believed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# evaluation/run_evaluation.py -> evaluation/ -> repository root.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from task_contracts import (  # noqa: E402
    CASES,
    DIAGNOSTIC_CASES,
    KILL_THRESHOLD,
    SUCCESS_THRESHOLD,
    EvalCase,
)

from local_agent.agent import Orchestrator, SkillLibrary  # noqa: E402
from local_agent.config import MODEL_PRESETS, ModelConfig, load_repo_config  # noqa: E402
from local_agent.llm.client import OpenAICompatibleClient, sdk_identity  # noqa: E402
from local_agent.llm.router import (  # noqa: E402
    CHEAP,
    STRONG,
    TieredClient,
    build_tiered_client,
)
from local_agent.llm.models import CallStats, ChatResponse, ToolCall  # noqa: E402
from local_agent.provenance import package_identity  # noqa: E402
from local_agent.tools import build_registry  # noqa: E402

import oracle  # noqa: E402  (evaluation/oracle.py)

SANDBOX = REPO / "benchmark_fixture" / "cpp_project"


class RehearsalClient:
    """An offline stand-in that exercises the harness without a model server.

    It is NOT a model and its scores mean nothing about model quality. It exists
    so you can prove the eval harness, the scoring and the report generation all
    work end to end before you have OVMS running, and so that when a real run
    looks wrong you know the harness is not the thing that is broken.
    """

    def __init__(self, plan: list[str] | None = None) -> None:
        self.plan = plan or ["build_target", "run_test"]
        self.step = 0

    def chat(self, messages, tools=None, max_tokens=None):
        available = {t["function"]["name"] for t in (tools or [])}
        prompt_tokens = sum(len(str(m.get("content") or "")) for m in messages) // 4

        for name in self.plan[self.step :]:
            self.step += 1
            if name in available:
                return ChatResponse(
                    tool_calls=[ToolCall.from_parts(f"r{self.step}", name, "{}")],
                    stats=CallStats(
                        total_s=0.0, ttft_s=0.0, prompt_tokens=prompt_tokens,
                        completion_tokens=8, streamed=True,
                    ),
                )

        return ChatResponse(
            content=(
                "Rehearsal run. No model was involved, so this answer is a "
                "placeholder and the score below measures the harness only."
            ),
            stats=CallStats(
                total_s=0.0, ttft_s=0.0, prompt_tokens=prompt_tokens,
                completion_tokens=32, streamed=True,
            ),
        )


def _schema_hash(registry: Any, toolset: list[str]) -> str:
    """What the model was actually offered, hashed.

    Narrowing is one of the treatment's three components, so the offered
    toolset is the independent variable and belongs in the artifact. Nobody
    should have to reconstruct it from the source of whatever package they
    think produced the file.
    """
    try:
        blob = json.dumps(registry.schemas(sorted(toolset)), sort_keys=True)
    except Exception:  # pragma: no cover - never load-bearing
        return "unavailable"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _managed(source: Path) -> dict:
    return json.loads((source / "scenarios" / "manifest.json").read_text())


def _clear_readonly_and_retry(func, path, exc: BaseException) -> None:
    """Recover one failed removal, or re-raise. Takes an exception INSTANCE.

    git marks everything under `.git/objects` read-only on purpose: the files
    are content-addressed and immutable, so the bit is a statement about the
    data rather than about permissions. On POSIX that changes nothing, because
    unlinking needs write permission on the DIRECTORY, not on the file. On
    Windows the read-only attribute blocks deletion outright, so a second
    `prepare()` into the same workdir dies with

        PermissionError: [WinError 5] Access is denied: '...\\.git\\objects\\...'

    Three properties worth stating, because each was got wrong once:

    * Only the path the OS actually refused is touched. This is not a
      speculative walk chmodding a tree in advance.
    * The existing mode is PRESERVED and the owner write bit added. Setting the
      mode to `S_IWRITE` outright strips read and execute, which on POSIX makes
      a directory untraversable and turns one failed unlink into a failed
      subtree.
    * Anything that is not a `PermissionError` is re-raised, so a genuinely
      stuck tree still fails loudly rather than being quietly accepted and the
      next case running against the previous case's repository.
    """
    if not isinstance(exc, PermissionError):
        raise exc
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        raise exc
    if stat.S_ISLNK(mode):
        # chmod would follow the link and modify something we were not asked to
        # remove. A symlink that will not unlink is a directory problem.
        raise exc
    os.chmod(path, stat.S_IMODE(mode) | stat.S_IWUSR)
    func(path)


def _clear_readonly_and_retry_legacy(func, path, exc_info) -> None:
    """The same recovery, for the pre-3.12 `shutil.rmtree(onerror=...)` shape.

    `onerror` is handed a `sys.exc_info()` TUPLE; `onexc`, added in 3.12, is
    handed the exception instance. This project supports Python 3.11, where only
    `onerror` exists, so a single handler that assumes an instance is broken on
    the minimum supported version: `isinstance(tuple, PermissionError)` is False,
    and `raise <tuple>` then fails with

        TypeError: exceptions must derive from BaseException

    which is a worse failure than the one it was meant to repair. Two thin
    adapters over one implementation, rather than a version check inside the
    handler, so both shapes are nameable and both are testable.
    """
    _clear_readonly_and_retry(func, path, exc_info[1])


def _remove_tree(path: Path) -> None:
    """`shutil.rmtree` that can delete a worktree containing a git repository.

    The evaluator owns these trees completely: it creates them, runs `git init`
    inside them, and destroys them. Failing to destroy one is not hypothetical,
    it is the first thing that happens when a case is prepared twice into the
    same workdir on Windows.
    """
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly_and_retry)
    else:
        shutil.rmtree(path, onerror=_clear_readonly_and_retry_legacy)


def prepare(workdir: Path, scenario: str) -> tuple[Path, Path]:
    """A fresh worktree with the scenario applied, and the oracle taken from it.

    Returns (root, oracle_dir). The oracle lives beside the worktree, not inside
    it, so no repository-relative path the agent can name reaches it.

    The scenario sources live outside the worktree too, and that is not a
    tidiness preference. In the Slice 3 dataset the agent read
    `scenarios/clean/src/ring_buffer.cpp`, the correct implementation, while
    diagnosing the link error, and cited it in its answer. It got there on the
    merits anyway; a weaker model would not have to. Diffing the working tree
    against a pristine copy of every file solves the compile, link and test
    scenarios without understanding any of them, which would flatter exactly
    the cells the experiment is trying to measure honestly.

    So the worktree the model sees is the fixture minus `scenarios/` and
    `scripts/`: source, headers, tests, CMakeLists, README. The baseline is
    restored and committed from outside, then the scenario is applied from
    outside, so `git_diff` still shows the injected defect and nothing shows
    where it came from.
    """
    root = workdir / "cpp_project"
    _refuse_a_path_windows_cannot_build_in(root)
    if root.exists():
        _remove_tree(root)
    shutil.copytree(
        SANDBOX, root,
        ignore=shutil.ignore_patterns("build", ".local-agent", "scenarios", "scripts"),
    )

    manifest = _managed(SANDBOX)
    # Baseline first, from the pristine copy, so "base" is the clean state
    # whatever happens to be committed at the top level of the package.
    for rel in manifest["managed_files"]:
        shutil.copyfile(SANDBOX / "scenarios" / "clean" / rel, root / rel)

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=e@v.al", "-c", "user.name=eval", "commit", "-qm", "base"],
        cwd=root,
        check=True,
    )

    if scenario not in manifest["scenarios"]:
        raise RuntimeError(f"unknown scenario {scenario!r}")
    for rel in manifest["scenarios"][scenario]:
        shutil.copyfile(SANDBOX / "scenarios" / scenario / rel, root / rel)

    oracle_dir = oracle.snapshot(root, workdir / "oracle")
    return root, oracle_dir


class PreconditionError(RuntimeError):
    """The state the task presupposes could not be established."""


# CMake nests its own tree under the worktree, and the Visual Studio generator
# is the deepest of them: objects, dependency files and .tlog files land roughly
# a hundred characters below the root, measured at 68 under the Makefile
# generator. Windows still caps the tools involved at 260 characters, so a long
# worktree leaves CMake nothing to work with and configure fails.
#
# Measured on the work laptop, the same run_case call twice:
#
#     worktree root 155 chars   configure (debug) FAILED in 4.9s
#     worktree root  67 chars   built ok, 4 tests registered
#
# It surfaced as `PreconditionError: configure failed`, which points at CMake
# and says nothing about paths, and cost an evening. 140 sits below the observed
# failure and far above the observed success, and leaves CMake about 120.
#
# This refuses rather than relocates. Moving somebody's working directory out
# from under them to a place they did not ask for is worse than telling them,
# and the remedy is one flag.
_WINDOWS_WORKTREE_BUDGET = 140


def _refuse_a_path_windows_cannot_build_in(root: Path) -> None:
    """No-op off Windows, where there is no limit and nothing to check."""
    if sys.platform != "win32":
        return
    length = len(str(root.resolve()))
    if length <= _WINDOWS_WORKTREE_BUDGET:
        return
    raise PreconditionError(
        f"the worktree path is {length} characters, and CMake needs about 120 "
        f"more underneath it than Windows has left. Configure would fail here "
        f"with an error that blames CMake.\n"
        f"  {root}\n"
        f"Run from a shorter directory, or point the output somewhere short. "
        f"A worktree root under {_WINDOWS_WORKTREE_BUDGET} characters is known "
        f"to build; 155 is known to fail."
    )


def establish(case: EvalCase, registry: Any) -> dict[str, Any]:
    """Put the worktree into the state the task's question presupposes.

    Setup, not measurement. The model is not charged for it and does not see
    it happen; it simply finds the repository the way the task describes it.

    A diagnosis task handed an unconfigured tree measures whether the model can
    recover a precondition, which is a different experiment and, under a
    read-only skill with no build tool, an unwinnable one. Whether the model
    should recover it is a real question; it is not THIS question, and mixing
    the two produced a 0.25 on three cases in the first run that was read as a
    model pathology and was not.

    Nothing here is silent. If the state cannot be established the case does
    not run, because a case that starts from the wrong state produces a number
    that means nothing.
    """
    detail: dict[str, Any] = {"precondition": case.precondition}
    if case.precondition == "none":
        detail["ok"] = True
        return detail
    if case.precondition != "built":
        raise PreconditionError(f"unknown precondition {case.precondition!r}")

    started = time.monotonic()
    configure = registry.get("configure_project").handler()
    if not (configure.ran and configure.ok):
        raise PreconditionError(f"configure failed: {configure.summary}")
    build = registry.get("build_target").handler()
    if not (build.ran and build.ok):
        raise PreconditionError(f"build failed: {build.summary}")

    # Registered, not run. Proving ctest can enumerate the suite is what makes
    # the model's first filtered run_test return real evidence; actually
    # running it here would pay the timeout scenario's timeout twice and would
    # hand the model a warm result it did not earn.
    listed = registry.get("list_tests").handler()
    names = (listed.data or {}).get("tests") or []
    if not (listed.ran and listed.ok and names):
        raise PreconditionError(f"no tests registered after build: {listed.summary}")

    detail.update({"ok": True, "seconds": round(time.monotonic() - started, 2),
                   "tests_registered": len(names)})
    return detail


def _error_row(case: EvalCase, locus: str, exc: BaseException, tb: str, started: float) -> dict:
    """A row for a run that did not produce a result, and says why.

    No `outcome`: the model was never given a fair chance to have one, so this
    is neither FAIL nor BLOCKED. `validity` is eval-level and starts with
    invalid_, so it is kept out of every denominator like any other invalid
    run. The exception and its traceback are preserved; a crash we cannot see
    is a crash we will repeat.
    """
    return {
        "case": case.name,
        "scenario": case.scenario,
        "outcome": None,
        "validity": f"invalid_{locus}_error",
        "error": f"{locus}: {type(exc).__name__}: {exc}",
        "error_locus": locus,
        "error_type": type(exc).__name__,
        "traceback": tb[-3000:],
        "score": 0.0,
        "counted": False,
        "succeeded": False,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def write_atomic(path: Path, payload: dict) -> None:
    """Write to a sibling temp file, flush, fsync, then replace. A process that
    dies mid-serialisation leaves the previous good file, never a torn one."""
    import os
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# After this many harness errors in a row the workspace cannot be shown to be
# clean for the next case, so the suite stops loudly with every row so far kept.
MAX_CONSECUTIVE_HARNESS_ERRORS = 2


def run_all(
    selected: list[EvalCase],
    repeat: int,
    run_one: Any,
    out: Path,
    identity: dict,
    echo: Any = print,
) -> tuple[list[dict], bool]:
    """The suite loop: run, checkpoint, decide whether to continue.

    Returns (rows, stopped_early). Every row is on disk before the next case
    starts, atomically, so a forty-minute run never loses its first thirty-nine.
    """
    rows: list[dict] = []
    consecutive_harness = 0

    def checkpoint(final: bool) -> None:
        write_atomic(out, {**identity, "complete": final, "rows": rows})

    checkpoint(final=False)
    for case in selected:
        for attempt in range(repeat):
            row = run_one(case, attempt)
            row["attempt"] = attempt
            rows.append(row)
            checkpoint(final=False)

            if row.get("error"):
                mark = "ERR "
            elif row.get("succeeded"):
                mark = "PASS"
            elif row.get("outcome") == "blocked":
                mark = "BLKD"
            else:
                mark = "FAIL"
            tag = ""
            if row.get("scope_violation"):
                tag += f"  SCOPE VIOLATION ({', '.join(row.get('forbidden_calls', []))})"
            if row.get("oracle_tampered"):
                tag += "  ORACLE TAMPERED"
            if row.get("validity") not in (None, "valid"):
                tag += f"  {str(row['validity']).upper()}"
            echo(
                f"  {mark}  {case.name:26s} score={row['score']:.2f} "
                f"outcome={str(row.get('outcome') or '-'):<8s} "
                f"calls={row.get('tool_calls', '-'):>3} {row['elapsed_s']:>6.1f}s"
                + (f"  [{row['error']}]" if row.get("error") else "")
                + tag
            )
            for name, ok in (row.get("required_checks") or {}).items():
                if not ok:
                    echo(f"          REQUIRED, not done: {name}")
            if row.get("claim_ok") is False:
                echo(f"          REQUIRED, wrong claim: said "
                     f"{row.get('claim')!r}, contract expects "
                     f"{'/'.join(row.get('expected_claim') or [])}")
            for name, ok in (row.get("checks") or {}).items():
                if not ok:
                    echo(f"          missed: {name}")

            # A precondition failure is as fatal as a harness crash: the
            # fixture cannot reach the state its own tasks describe, so every
            # later number would be measured from the wrong start.
            if row.get("error_locus") in ("harness", "precondition"):
                consecutive_harness += 1
                if consecutive_harness >= MAX_CONSECUTIVE_HARNESS_ERRORS:
                    echo(
                        f"\nSTOPPING: {consecutive_harness} harness or precondition "
                        f"errors in a row. The workspace cannot be shown in the state "
                        f"the next case describes. "
                        f"{len(rows)} row(s) preserved in {out}."
                    )
                    return rows, True
            else:
                consecutive_harness = 0
    return rows, False


_TRANSCRIPT_CAP = 6_000  # characters per message; tool results can be large


def save_transcript(out: Path, case_name: str, attempt: int, messages: list[dict]) -> str:
    """The conversation the model actually saw, beside the results file.

    This is how "why did it call run_test six times" becomes a fact instead of
    a hypothesis. Capped per message, never per transcript.
    """
    folder = out.with_name(out.stem + "-transcripts")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{case_name}-{attempt}.json"
    trimmed = []
    for m in messages:
        m = dict(m)
        content = m.get("content")
        if isinstance(content, str) and len(content) > _TRANSCRIPT_CAP:
            m["content"] = content[:_TRANSCRIPT_CAP] + f"\n[... {len(content) - _TRANSCRIPT_CAP} more chars]"
        trimmed.append(m)
    write_atomic(path, {"case": case_name, "attempt": attempt, "messages": trimmed})
    return str(path)


def run_case(
    case: EvalCase,
    model: ModelConfig,
    workdir: Path,
    auto_approve: bool,
    rehearse: bool = False,
    cheap: ModelConfig | None = None,
    client: Any = None,
    transcript_out: Path | None = None,
    attempt: int = 0,
    keep_all_transcripts: bool = False,
    no_skill: bool = False,
    condition: str = "skill",
    catalogue: bool = False,
) -> dict:
    if no_skill:
        condition = "control"
    started = time.monotonic()
    try:
        root, oracle_dir = prepare(workdir, case.scenario)
        repo = load_repo_config(root)
        registry, _, _ = build_registry(repo)
        skills = SkillLibrary.discover(REPO / "skills")
    except Exception as exc:
        # The harness, not the model, failed. Say so, and keep going.
        import traceback
        return _error_row(case, "harness", exc, traceback.format_exc(), started)

    try:
        setup = establish(case, registry)
    except Exception as exc:
        # A fixture that cannot reach its own starting state is not a model
        # result at any score. It is not counted and it is loud.
        import traceback
        row = _error_row(case, "precondition", exc, traceback.format_exc(), started)
        row["precondition"] = case.precondition
        return row
    if client is not None:
        pass  # injected, for tests that drive the harness with a scripted model
    elif rehearse:
        client = RehearsalClient()
    elif cheap is not None:
        # Two tiers and three conditions do not mix, and the reason is subtle
        # enough to be worth refusing rather than documenting.
        #
        # The tier is chosen by `tier_for_skill(skill.name, skill.tier, ...)`.
        # The `narrow` and `skill` conditions both hold a skill object, because
        # narrow needs it for its toolset, so both inherit that skill's
        # declared tier. `control` holds no skill at all and falls to the
        # default. With a TieredClient in play that is not a reporting
        # difference, it is a different MODEL serving one arm of the
        # experiment, which would make the contrast meaningless while looking
        # entirely normal in the output.
        #
        # It has never been reachable from run_experiment.sh, which passes no --cheap.
        # "Never reached" is not the same as "cannot be reached", and this is
        # the measurement, so it is now the latter.
        if condition != "skill":
            raise SystemExit(
                "--cheap cannot be combined with the mechanism conditions: the "
                "routing tier is derived from the skill, which control does not "
                "have, so the conditions would not share a model. Run the "
                "mechanism experiment against a single pinned model."
            )
        client = build_tiered_client({STRONG: model, CHEAP: cheap}, default_tier=STRONG)
    else:
        client = OpenAICompatibleClient(model)

    orch = Orchestrator(
        repo=repo,
        registry=registry,
        client=client,
        skills=skills,
        approval=(lambda *a: auto_approve),
        context_budget_tokens=model.context_budget_tokens,
    )

    started = time.monotonic()
    try:
        result = orch.run(
            case.task,
            skill_name=None if condition == "control" else case.skill,
            condition=condition,
            catalogue=catalogue,
            verification_required=case.verification_required,
        )
        error = None
    except Exception as exc:
        # Slice 1 made server death a typed halt, so anything reaching here is
        # a genuine defect in the agent. Record it as one and keep the suite
        # alive; the traceback goes in the row, not on the terminal.
        import traceback
        return _error_row(case, "agent", exc, traceback.format_exc(), started)
    elapsed = time.monotonic() - started

    checks = {name: bool(fn(result)) for name, fn in case.checks.items()}
    score = sum(checks.values()) / len(checks) if checks else 0.0

    # Hard requirements, never averaged. Each is the difference between doing
    # the work and describing it, and a missing one cannot be outvoted by
    # quality points: a navigation run that called no tools at all scored 0.80
    # and was recorded as a capability success under the old averaging.
    required = {name: bool(fn(result)) for name, fn in case.required_checks.items()}
    required_ok = all(required.values())

    # The claim is the model's output, and `claim == "success"` is what makes
    # the orchestrator demand a passing verification. A model that answers
    # "diagnosis" on a build task dodges that requirement, and scored 1.00
    # doing it. What a finished answer claims is the task's contract to state.
    claim_ok = result.state.claim in case.expected_claim

    # The eval's own verdict, against the real oracle. The agent's `verified`
    # flag is what the agent believes about the worktree it edited; this is what
    # is true about the worktree once the expected truth is put back.
    try:
        tamper = oracle.compare(root, oracle_dir)
        oracle.restore(root, oracle_dir)
        eval_verification = oracle.verify(registry)
    except Exception as exc:
        import traceback
        row = _error_row(case, "harness", exc, traceback.format_exc(), started)
        row["agent_outcome_before_harness_error"] = result.outcome.value
        row["tool_calls"] = result.state.tool_calls
        return row
    # The agent said it verified success, and the restored oracle disagrees.
    # Whatever the cause, the agent's claim is not evidence.
    disagreement = bool(result.state.verified and not eval_verification["ok"])

    validity = result.state.validity.value
    counted = validity == "valid" and not tamper.tampered

    # Scope: a diagnosis task that edited the tree is wrong behaviour whatever
    # its answer says. Fails the case, does not merely cost a check.
    # Three different questions, kept apart on purpose.
    #
    #   forbidden_attempts   it reached for a tool this task forbids
    #   forbidden_calls      the call executed cleanly. NOT the same as damage:
    #                        propose_patch succeeds without touching the tree.
    #                        `mutation_epoch > 0` is what says the worktree
    #                        actually changed
    #   scope_violation      it reached for one that was available to it
    #
    # A model does not earn credit for staying in its lane because its illegal
    # edit had malformed arguments, so a failed attempt still violates scope.
    # But an attempt refused because the active skill never offered the tool is
    # already counted as tool_not_allowed, and that refusal IS the narrowing
    # treatment working. Counting it twice would make the treatment look worse
    # for preventing the thing, and would confound the two components we are
    # trying to tell apart.
    forbidden_attempts = [h.name for h in result.state.history
                          if h.name in case.forbidden_tools]
    forbidden_calls = [h.name for h in result.state.history
                       if h.name in case.forbidden_tools and h.execution == "ok"]
    scope_violation = any(
        h.name in case.forbidden_tools and h.reason != "tool_not_allowed"
        for h in result.state.history
    )

    # Two different model failures, kept apart. `invented` is a tool name that
    # exists nowhere. `not_allowed` is a real tool the active skill does not
    # offer, which is the direct measure of whether narrowing is fighting the
    # model or agreeing with it.
    invented_tool_calls = [h.name for h in result.state.history
                           if h.reason == "unknown_tool"]
    tool_not_allowed_calls = [h.name for h in result.state.history
                              if h.reason == "tool_not_allowed"]

    succeeded = bool(
        result.outcome.succeeded
        and required_ok
        and claim_ok
        and score >= SUCCESS_THRESHOLD
        and not tamper.tampered
        and not disagreement
        and not scope_violation
    )

    return {
        "case": case.name,
        "scenario": case.scenario,
        "condition": condition,
        "catalogue": catalogue,
        "skill": result.state.active_skill,
        "narrowed_by": result.state.narrowed_by,
        # Proof in the dataset, not a promise in a document, that this row was
        # served by one pinned model with no tier selection and no escalation.
        # The tier is derived from the skill, and control has no skill, so a
        # tiered run would silently serve one arm from a different model.
        "tiered": isinstance(client, TieredClient),
        "escalated": bool((result.routing.escalated if result.routing else False)),
        # prose or structured. Every condition is now told to submit, so prose
        # is a failure to follow the shared protocol rather than a choice
        # between two sanctioned endings. Recorded because a condition that
        # forgets the protocol more often is a real finding.
        "submission_mode": "structured" if result.state.claim is not None else "prose",
        "score": round(score, 3),
        "weight": case.weight,
        "checks": checks,
        "required_checks": required,
        "required_ok": required_ok,
        "expected_claim": sorted(str(c) for c in case.expected_claim),
        "claim_ok": claim_ok,
        "tool_calls": result.state.tool_calls,
        "tools_used": [h.name for h in result.state.history],
        # What an audit needs, per call: nothing here is prose the grader reads.
        "history": [
            {
                "name": h.name, "arguments": h.arguments, "execution": h.execution,
                "domain": h.domain, "reason": h.reason, "ok": h.ok, "epoch": h.epoch,
                "exit_code": h.exit_code, "summary": (h.summary or "")[:300],
                # What the shared classifier made of this call. Persisted so an
                # auditor can see the runtime's own verdict on each row rather
                # than having to re-derive it and hope the rules matched.
                "proof": h.proof,
                # The structured facts the classifier and the required checks
                # read, so a row can be re-scored offline without them silently
                # degrading to "absent, therefore fine". rescore.py treats an
                # absent key as unknown rather than as False, so this list has
                # to cover rescore.EVIDENCE_DEPENDENT or every new dataset is
                # born only partially replayable. A test pins that.
                "evidence": {k: (h.evidence or {}).get(k)
                             for k in ("totals", "stale_sources", "ran_nothing",
                                       "no_build_record", "build_profile_mismatch",
                                       "profile_mismatch", "built_profile")
                             if k in (h.evidence or {})},
            }
            for h in result.state.history
        ],
        "claim": result.state.claim,
        "cited_evidence": result.state.cited_evidence,
        "cited_correctly": result.state.cited_correctly,
        "cited_unknown": result.state.cited_unknown,
        "citation_schemes": result.state.citation_schemes,
        "verified": result.state.verified,
        "verification_attempted": result.state.verification_attempted,
        "mutation_epoch": result.state.mutation_epoch,
        "scope_violation": scope_violation,
        "forbidden_calls": forbidden_calls,
        "forbidden_attempts": forbidden_attempts,
        "offered_tools": sorted(result.state.toolset),
        "tool_schema_hash": _schema_hash(registry, result.state.toolset),
        "precondition": setup,
        "invented_tool_calls": invented_tool_calls,
        "tool_not_allowed_calls": tool_not_allowed_calls,
        "halt_reason": result.state.halt_reason,
        "elapsed_s": round(elapsed, 1),
        "metrics": result.state.metrics.as_dict(),
        "routing": result.routing.as_dict() if result.routing else None,
        "outcome": result.outcome.value,
        "halt_cause": result.state.halt_cause.value if result.state.halt_cause else None,
        "validity": validity,
        "oracle_tampered": tamper.tampered,
        "oracle_tamper": tamper.as_dict() if tamper.tampered else None,
        "eval_verification": eval_verification,
        "verification_disagreement": disagreement,
        # Only VALID, untampered runs enter a denominator. Everything else is
        # reported, kept, and counted separately.
        "counted": counted,
        "succeeded": succeeded,
        "answer": result.answer[:1500],
        "transcript": (
            save_transcript(transcript_out, case.name, attempt, result.messages)
            if transcript_out is not None and (keep_all_transcripts or not succeeded)
            else None
        ),
        "error": error,
    }


def build_ledger(rows: list[dict], tiered: bool = False) -> dict:
    """The task-level accounting, which is what the POC is actually about.

    Counted per task, not per model call. Six internal turns to answer one git
    query is one task, not six wins, and reporting it the other way would
    flatter the cheap tier for being chatty.
    """
    # Runs that may not enter a denominator: the agent edited the oracle, or the
    # run happened under a configuration nobody chose. Reported, never counted.
    excluded = [r for r in rows if r.get("counted") is False]
    tampered = [r for r in rows if r.get("oracle_tampered")]
    invalid = [r for r in rows if r.get("validity") not in (None, "valid")]
    errors = [r for r in rows if r.get("error")]
    all_rows = rows
    rows = [r for r in rows if r.get("counted", True)]

    total = len(rows)
    blocked = [r for r in rows if r.get("outcome") == "blocked"]
    escalated = [r for r in rows if r.get("outcome", "").startswith("escalated")]
    cheap_only = [r for r in rows if r.get("outcome") == "pass"]
    single = [r for r in rows if r.get("outcome") == "fail"]

    cheap_success = [r for r in cheap_only if r.get("succeeded")]
    escalated_success = [r for r in escalated if r.get("succeeded")]
    local_success = [r for r in rows if r.get("succeeded")]

    calls = {"cheap": 0, "strong": 0}
    scope_violations = [r for r in rows if r.get("scope_violation")]
    for row in rows:
        metrics = row.get("metrics") or {}
        tiers = metrics.get("tiers") or {}
        by_tier = tiers.get("by_tier") or {}
        if by_tier:
            for tier in calls:
                calls[tier] += by_tier.get(tier, {}).get("calls", 0)
        else:
            # A single-client run has no tier breakdown. Its calls all belong
            # to whichever tier the routing says served it. The first real run
            # reported 0 model calls against 92 actually made because of this.
            tier = (row.get("routing") or {}).get("final_tier") or "cheap"
            calls[tier if tier in calls else "cheap"] += int(metrics.get("llm_calls") or 0)
    call_total = sum(calls.values())

    def _median(values):
        return round(statistics.median(values), 1) if values else None

    attempted = total - len(blocked)
    discarded = sum(
        (r.get("routing") or {}).get("discarded_cheap_calls", 0) for r in rows
    )
    return {
        # Two denominators on purpose. Model capability excludes blocked tasks,
        # because the environment stopping a tool says nothing about the model.
        # End-to-end includes them, because a user whose task did not complete
        # does not care whose fault it was.
        "model_capability_rate": (
            round(len(local_success) / attempted, 3) if attempted else None
        ),
        "end_to_end_rate": round(len(local_success) / total, 3) if total else None,
        "environment_availability": round(attempted / total, 3) if total else None,
        "cheap_calls_discarded": discarded,
        "tasks": total,
        "tasks_submitted": len(all_rows),
        "excluded": len(excluded),
        "oracle_tampered": len(tampered),
        "oracle_tampered_cases": [r["case"] for r in tampered],
        "invalid_runs": len(invalid),
        # Rows where the harness or the agent raised. Never counted, always
        # listed: a crash we cannot see is a crash we will repeat.
        "errors": [
            {"case": r["case"], "locus": r.get("error_locus", "unknown"), "error": r["error"]}
            for r in errors
        ],
        "invalid_cases": [
            {"case": r["case"], "validity": r.get("validity")} for r in invalid
        ],
        "blocked": len(blocked),
        "blocked_cases": [r["case"] for r in blocked],
        "tiered": tiered,
        "scope_violations": len(scope_violations),
        "scope_violation_cases": [r["case"] for r in scope_violations],
        "precondition_failures": len(
            [r for r in rows if r.get("error_locus") == "precondition"]),
        "invented_tool_calls": sum(len(r.get("invented_tool_calls") or []) for r in rows),
        "tool_not_allowed_calls": sum(
            len(r.get("tool_not_allowed_calls") or []) for r in rows),
        "attempted": attempted,
        "cheap_only_tasks": len(cheap_only),
        "cheap_only_success": len(cheap_success),
        "escalated_tasks": len(escalated),
        "escalated_success": len(escalated_success),
        "single_tier_tasks": len(single),
        "local_success": len(local_success),
        "cheap_alone_rate": round(len(cheap_success) / attempted, 3) if attempted else None,
        "local_success_rate": round(len(local_success) / attempted, 3) if attempted else None,
        "cloud_required_rate": (
            round(1 - len(local_success) / attempted, 3) if attempted else None
        ),
        "calls_by_tier": calls,
        "cheap_call_share": round(calls["cheap"] / call_total, 3) if call_total else None,
        "median_seconds_cheap_only": _median(
            [r["elapsed_s"] for r in cheap_only if r.get("elapsed_s")]
        ),
        "median_seconds_escalated": _median(
            [r["elapsed_s"] for r in escalated if r.get("elapsed_s")]
        ),
        "escalations": [
            {"case": r["case"], "reason": (r.get("routing") or {}).get("escalation_reason")}
            for r in escalated
        ],
    }


def render_ledger(l: dict) -> str:
    def pct(value):
        return f"{value:.0%}" if value is not None else "n/a"

    lines = [
        f"Model capability success   {l['local_success']}/{l['attempted']}"
        f"   {pct(l['model_capability_rate'])}   (blocked excluded)",
        f"End-to-end completion      {l['local_success']}/{l['tasks']}"
        f"   {pct(l['end_to_end_rate'])}   (blocked included)",
        f"Environment availability   {l['attempted']}/{l['tasks']}"
        f"   {pct(l['environment_availability'])}",
        "",
        f"Tasks submitted:        {l.get('tasks_submitted', l['tasks'])}",
        f"Excluded from counts:   {l.get('excluded', 0)}"
        + (
            f"  (oracle tampered: {l['oracle_tampered']}"
            + (f" [{', '.join(l['oracle_tampered_cases'])}]" if l.get("oracle_tampered_cases") else "")
            + f", invalid run: {l['invalid_runs']})"
            if l.get("excluded") else ""
        ),
        (f"Errors (not counted):   {len(l['errors'])}  "
         + "; ".join(f"{e['case']} [{e['locus']}]" for e in l["errors"]))
        if l.get("errors") else "Errors (not counted):   0",
        f"Tasks counted:          {l['tasks']}",
        f"Blocked (environment):  {l['blocked']}"
        + (f"  {', '.join(l['blocked_cases'])}" if l["blocked_cases"] else ""),
        f"Attempted:              {l['attempted']}",
        f"Precondition failures:  {l['precondition_failures']}",
        f"Scope violations:       {l['scope_violations']}"
        + (f"  {', '.join(l['scope_violation_cases'])}" if l["scope_violation_cases"] else ""),
        f"Tool selection:         {l['invented_tool_calls']} invented, "
        f"{l['tool_not_allowed_calls']} real but not offered by the skill",
        "",
        f"Cheap tier alone:       {l['cheap_only_success']}/{l['attempted']}"
        f"   {pct(l['cheap_alone_rate'])}",
        f"Escalated success:      {l['escalated_success']}/{l['escalated_tasks']}",
        f"Local success total:    {l['local_success']}/{l['attempted']}"
        f"   {pct(l['local_success_rate'])}",
        f"Cloud would be needed:  {pct(l['cloud_required_rate'])}",
        "",
        # cheap and strong are routing labels. With one client the same
        # endpoint is called "cheap" under a skill and "strong" without one,
        # because tier_for_skill(None) defaults that way, so printing shares
        # would be reporting the label as if it were the model.
        (f"Model calls:  {l['calls_by_tier']['cheap'] + l['calls_by_tier']['strong']}, "
         f"one client, no tiering"
         if not l.get("tiered") else
         f"Model calls:  cheap {l['calls_by_tier']['cheap']} gross "
         f"({l['cheap_calls_discarded']} discarded by escalation), "
         f"strong {l['calls_by_tier']['strong']}  "
         f"({pct(l['cheap_call_share'])} cheap gross)"),
        f"Median task:  cheap-only {l['median_seconds_cheap_only']}s, "
        + (f"escalated {l['median_seconds_escalated']}s"
           if l["median_seconds_escalated"] is not None else "no escalations"),
    ]
    if l["escalations"]:
        lines += ["", "Escalated:"]
        lines += [f"  {e['case']}: {e['reason']}" for e in l["escalations"]]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(MODEL_PRESETS))
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--case", action="append", help="run only these cases")
    parser.add_argument("--repeat", type=int, default=1)
    # `action="store_true", default=True` could never be False. Approval mode
    # is part of the experiment's identity: these runs auto-approve on purpose,
    # so that an attempted out-of-scope mutation actually happens and can be
    # counted, rather than being silently prevented by a prompt nobody answers.
    parser.add_argument("--approval-mode", choices=["auto", "deny"], default="auto",
                        help="auto: approve every request, so scope violations "
                             "materialise and are measurable. deny: refuse every "
                             "request, which measures behaviour under refusal")
    parser.add_argument("--out", default="evals.json")
    parser.add_argument("--label", help="human name for this configuration, used in reports")
    parser.add_argument(
        "--cheap-profile",
        choices=sorted(MODEL_PRESETS),
        help="route cheap-tier skills to this endpoint and escalate to --profile "
             "only on failure; produces the cheap-tier share and escalation rate",
    )
    parser.add_argument("--workdir", default="/tmp/local-agent-evals")
    parser.add_argument(
        "--condition", choices=["control", "narrow", "skill"], default="skill",
        help="control: no procedure, every registered tool. narrow: no "
             "procedure, the skill's toolset. skill: the body and the toolset. "
             "control vs narrow is the effect of taking tools away; narrow vs "
             "skill is what the written procedure adds on top",
    )
    parser.add_argument(
        "--no-skill", action="store_true", help="older spelling of --condition control",
    )
    parser.add_argument(
        "--catalogue", action="store_true",
        help="show the list of skill names in the system prompt. Off by "
             "default: the harness pins the skill, so the catalogue is about "
             "discovery and routing, and leaving it in would make skill minus "
             "narrow mean procedure plus catalogue",
    )
    parser.add_argument(
        "--transcripts", choices=["failures", "all"], default="all",
        help="save transcripts for every row (default) or only for non-succeeded "
             "ones. Why a skill helped cannot be read out of a score, so a "
             "dataset without the successful paths cannot answer the question "
             "the experiment is asking",
    )
    parser.add_argument(
        "--rehearse",
        action="store_true",
        help="run the harness with no model at all, to prove the pipeline works",
    )
    args = parser.parse_args()

    model = MODEL_PRESETS.get(args.profile, ModelConfig()) if args.profile else ModelConfig.from_env()
    if args.base_url:
        model = ModelConfig(**{**model.__dict__, "base_url": args.base_url})
    if args.model:
        model = ModelConfig(**{**model.__dict__, "model": args.model})

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    selected = [c for c in CASES if not args.case or c.name in args.case]

    if args.rehearse:
        print("REHEARSAL: no model server is being used. Scores measure the "
              "harness, not any model.\n")
    else:
        print(f"endpoint: {model.base_url}  model: {model.model}  device: {model.device_note}")
    print(f"cases: {len(selected)} x {args.repeat}\n")

    identity = {
        "label": args.label
        or ("REHEARSAL (no model)" if args.rehearse else f"{model.model} on {model.device_note}"),
        "rehearsal": bool(args.rehearse),
        "endpoint": "none (rehearsal)" if args.rehearse else model.base_url,
        "model": "none (rehearsal)" if args.rehearse else model.model,
        "device": "none (rehearsal)" if args.rehearse else model.device_note,
        "model_identity": None if args.rehearse else {**model.identity(), **sdk_identity()},
        "package": package_identity(),
        "context_budget_tokens": model.context_budget_tokens,
        "cheap_profile": args.cheap_profile,
        "condition": "control" if args.no_skill else args.condition,
        "catalogue": args.catalogue,
        "approval_mode": args.approval_mode,
        # With a single client there is no cheap and no strong, only the model
        # that ran. tier_for_skill(None) labels the control "strong" and a
        # skill-routed run "cheap" on the SAME endpoint, so the tier shares are
        # routing metadata, not execution identity, and are meaningless here.
        "tiered": bool(args.cheap_profile),
        "kill_threshold": KILL_THRESHOLD,
    }
    cheap_cfg = MODEL_PRESETS[args.cheap_profile] if args.cheap_profile else None
    out_path = Path(args.out)
    rows, stopped = run_all(
        selected, args.repeat,
        lambda case, attempt=0: run_case(
            case, model, workdir, args.approval_mode == "auto", args.rehearse,
            cheap=cheap_cfg,
            transcript_out=out_path, attempt=attempt,
            keep_all_transcripts=(args.transcripts != "failures"),
            condition=("control" if args.no_skill else args.condition),
            catalogue=args.catalogue,
        ),
        out_path, identity,
    )

    weighted = sum(r["score"] * r.get("weight", 1.0) for r in rows)
    total_weight = sum(r.get("weight", 1.0) for r in rows)
    overall = weighted / total_weight if total_weight else 0.0
    print(f"\noverall: {overall:.3f} weighted over {len(rows)} run(s)")

    times = [r["elapsed_s"] for r in rows if r.get("elapsed_s")]
    if times:
        print(f"time per case: median {statistics.median(times):.1f}s, max {max(times):.1f}s")

    ttfts = [
        r["metrics"]["median_ttft_s"]
        for r in rows
        if r.get("metrics") and r["metrics"].get("median_ttft_s")
    ]
    if ttfts:
        print(f"median ttft across runs: {statistics.median(ttfts):.2f}s")
    compactions = sum(r.get("metrics", {}).get("compactions", 0) for r in rows)
    if compactions:
        print(
            f"context compactions: {compactions} "
            "(each discards the server prompt cache; raise context_budget_tokens "
            "or tighten max_tool_result_bytes)"
        )

    # The diagnostic cases are the ones that decide whether this is a tool or a
    # toy. Reported separately so a high score on trivial cases cannot hide a
    # failure on the ones that matter.
    ledger = build_ledger(rows, tiered=bool(args.cheap_profile))
    print()
    print(render_ledger(ledger))

    # Two numbers, because they answer different questions and the kill
    # decision belongs to the second. Quality is the average of the answer
    # rubric; capability is whether the work was done. The zero-test cheat
    # scored 1.00 on quality with required_ok false, so a quality-driven
    # verdict could have printed "capability 0/4" and "diagnostic subset 1.000
    # -> usable" in the same report.
    diagnostic = [r for r in rows if r["case"] in DIAGNOSTIC_CASES]
    diag_score = (
        sum(r["score"] for r in diagnostic) / len(diagnostic) if diagnostic else None
    )
    diag_capability = (
        sum(1 for r in diagnostic if r.get("succeeded")) / len(diagnostic)
        if diagnostic else None
    )
    if diag_capability is not None:
        verdict = "usable" if diag_capability >= KILL_THRESHOLD else "below the kill threshold"
        print(
            f"diagnostic subset: capability {diag_capability:.3f} over "
            f"{len(diagnostic)} run(s) -> {verdict} (threshold {KILL_THRESHOLD}); "
            f"answer quality {diag_score:.3f}"
        )

    write_atomic(
        Path(args.out),
        {
            **identity,
            "complete": not stopped,
            "stopped_early": stopped,
            "overall": overall,
            "tasks_without_escalation": ledger["cheap_only_success"],
            "tasks_routed": ledger["attempted"],
            "ledger": ledger,
            "diagnostic_quality_score": diag_score,
            "diagnostic_capability_rate": diag_capability,
            # Kept under the old name so earlier datasets stay comparable, but
            # it is the quality average and it does not drive the kill gate.
            "diagnostic_score": diag_score,
            "rows": rows,
        },
    )
    print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
