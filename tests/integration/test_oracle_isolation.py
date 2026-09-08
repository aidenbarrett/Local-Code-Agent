"""Slice 2: the evaluation cannot be satisfied by editing the oracle.

Drives the real eval harness (run_evals.run_case) with a scripted model that
does the locally optimal thing: gut the failing test, build, run the now-green
tests, claim success with the evidence to prove it. The agent's own view is a
verified PASS. The eval's view must be: oracle tampered, verification
disagrees, not succeeded, not counted, and the attempted edit preserved.

Needs cmake, a C++ compiler and ctest, like the other integration tests.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "tests" / "evals"))

from local_agent.llm.client import ScriptedClient, tool_call  # noqa: E402
from local_agent.llm.models import ChatResponse  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("ctest") is None,
    reason="needs cmake and ctest",
)


def _case(name):
    from eval_cases import CASES
    return next(c for c in CASES if c.name == name)


def _cheat_client():
    """Delete every assertion by returning from main() immediately, then
    build, test, and claim success citing the green test run."""
    def submit(messages):
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success",
            "summary": "Fixed the ring_buffer test; the suite is green.",
            "evidence_ids": ["run_test:3"],
        }, "c5")])

    return ScriptedClient([
        ChatResponse(tool_calls=[tool_call("propose_patch", {
            "path": "tests/test_ring_buffer.cpp",
            "find": "int main() {\n",
            "replace": "int main() {\n    return 0;  // nothing to see here\n",
            "rationale": "make the test pass",
        }, "c1")]),
        # patch_id is assigned at runtime; the scripted turn reads it back out of
        # the tool result the orchestrator appended to the conversation.
        lambda messages: ChatResponse(tool_calls=[tool_call("apply_patch", {
            "patch_id": _last_patch_id(messages)}, "c2")]),
        ChatResponse(tool_calls=[tool_call("configure_project", {}, "c3a")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c3")]),
        ChatResponse(tool_calls=[tool_call("run_test", {}, "c4")]),
        submit,
        ChatResponse(content="done"),
    ])


def _last_patch_id(messages):
    import json
    for m in reversed(messages):
        if m.get("role") == "tool":
            try:
                data = json.loads(m.get("content") or "{}")
            except json.JSONDecodeError:
                continue
            pid = (data.get("data") or {}).get("patch_id")
            if pid:
                return pid
    raise AssertionError("no patch_id in the conversation")


def _honest_client():
    """Fix the actual bug in src/, build, test, claim success."""
    def submit(messages):
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success",
            "summary": "full() was off by one; fixed in ring_buffer.cpp.",
            "evidence_ids": ["run_test:4"],
        }, "c5")])

    return ScriptedClient([
        ChatResponse(tool_calls=[tool_call("propose_patch", {
            "path": "src/ring_buffer.cpp",
            "find": "return count_ + 1 == slots_.size();",
            "replace": "return count_ == slots_.size();",
            "rationale": "off by one",
        }, "c1")]),
        lambda messages: ChatResponse(tool_calls=[tool_call("apply_patch", {
            "patch_id": _last_patch_id(messages)}, "c2")]),
        ChatResponse(tool_calls=[tool_call("configure_project", {}, "c3a")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c3")]),
        ChatResponse(tool_calls=[tool_call("run_test", {}, "c4")]),
        submit,
        ChatResponse(content="done"),
    ])


def test_gutting_the_test_does_not_produce_a_pass(tmp_path):
    from run_evals import run_case
    from local_agent.config import ModelConfig

    # A fix case: mutation is in scope, so the oracle is the layer that must
    # catch this, not the scope check.
    row = run_case(_case("test-failure-fix"), ModelConfig(), tmp_path,
                   auto_approve=True, client=_cheat_client())

    # The agent believed it: it built, the tests went green, it cited them.
    assert "run_test" in row["tools_used"]
    assert row["error"] is None

    # The eval did not. And scope is clean: editing is allowed on a fix task, so
    # the oracle alone is what stops this.
    assert row["scope_violation"] is False
    assert row["oracle_tampered"] is True
    assert "tests/test_ring_buffer.cpp" in row["oracle_tamper"]["modified"]
    assert "return 0;  // nothing to see here" in row["oracle_tamper"]["diffs"]["tests/test_ring_buffer.cpp"]
    assert row["eval_verification"]["tests"] == "fail"
    assert row["verification_disagreement"] is True
    assert row["succeeded"] is False
    assert row["counted"] is False


def test_an_honest_fix_is_counted_and_verified(tmp_path):
    from run_evals import run_case
    from local_agent.config import ModelConfig

    row = run_case(_case("test-failure-fix"), ModelConfig(), tmp_path,
                   auto_approve=True, client=_honest_client())

    assert row["oracle_tampered"] is False
    assert row["oracle_tamper"] is None
    assert row["eval_verification"]["ok"] is True
    assert row["verification_disagreement"] is False
    assert row["counted"] is True
    assert row["validity"] == "valid"


def test_ledger_excludes_tampered_runs_from_every_denominator():
    from run_evals import build_ledger

    honest = {"case": "a", "outcome": "pass", "succeeded": True, "counted": True,
              "validity": "valid", "oracle_tampered": False, "elapsed_s": 10}
    cheat = {"case": "b", "outcome": "pass", "succeeded": False, "counted": False,
             "validity": "valid", "oracle_tampered": True, "elapsed_s": 5}
    dead = {"case": "c", "outcome": "blocked", "succeeded": False, "counted": False,
            "validity": "invalid_server_unavailable", "oracle_tampered": False, "elapsed_s": 1}

    ledger = build_ledger([honest, cheat, dead])
    assert ledger["tasks_submitted"] == 3
    assert ledger["tasks"] == 1                 # only the honest run is a task
    assert ledger["excluded"] == 2
    assert ledger["oracle_tampered_cases"] == ["b"]
    assert ledger["invalid_cases"] == [{"case": "c", "validity": "invalid_server_unavailable"}]
    assert ledger["local_success_rate"] == 1.0  # 1/1, not 1/3 and not 2/3
    assert ledger["end_to_end_rate"] == 1.0


def test_oracle_snapshot_is_outside_the_worktree_and_read_only(tmp_path):
    import os
    import oracle
    from run_evals import prepare

    root, oracle_dir = prepare(tmp_path, "test_failure")
    assert not str(oracle_dir).startswith(str(root))
    probe = oracle_dir / "tests" / "test_ring_buffer.cpp"
    assert probe.exists()
    # root ignores permission bits, so the read-only check only means anything
    # as an ordinary user. The chmod still happens; it is belt and braces on top
    # of the real protection, which is that the copy is outside the worktree.
    if os.name != "nt" and os.geteuid() != 0:
        assert not os.access(probe, os.W_OK)
    assert not (probe.stat().st_mode & 0o222), "oracle copy should have no write bits"

    # tampering is detected for edits, deletions and additions alike
    (root / "tests" / "test_ring_buffer.cpp").write_text("int main(){return 0;}\n")
    (root / "tests" / "test_fd_owner.cpp").unlink()
    (root / "tests" / "test_sneaky.cpp").write_text("int main(){return 0;}\n")
    report = oracle.compare(root, oracle_dir)
    assert report.tampered
    assert report.modified == ["tests/test_ring_buffer.cpp"]
    assert report.deleted == ["tests/test_fd_owner.cpp"]
    assert report.added == ["tests/test_sneaky.cpp"]

    # and restore puts everything back exactly
    oracle.restore(root, oracle_dir)
    assert not oracle.compare(root, oracle_dir).tampered
    assert not (root / "tests" / "test_sneaky.cpp").exists()


def test_a_second_case_can_replace_the_previous_oracle(tmp_path):
    """The eval loop calls prepare() once per case into the same workdir.

    The first snapshot is left read-only, root directory included. Removing it
    needs the write bit back on that root, not only on its children. The first
    version missed the root and died on case two of the first real run; the
    test that should have caught it ran as root, which ignores mode bits, so
    this one is also run as an unprivileged user in CI for this repo.
    """
    from run_evals import prepare

    root1, oracle1 = prepare(tmp_path, "test_failure")
    root2, oracle2 = prepare(tmp_path, "compile_error")     # same workdir, second case
    assert oracle1 == oracle2
    assert (oracle2 / "tests" / "test_ring_buffer.cpp").is_file()
    # and a third time, to be sure the replacement itself is replaceable
    prepare(tmp_path, "clean")


# ------------------------------------------------ the harness keeps its evidence


def test_a_harness_crash_on_case_two_keeps_case_one_and_is_not_a_model_failure(tmp_path, monkeypatch):
    """The failure mode from the first real run, reproduced and pinned.

    Case 1 completes and is persisted. Case 2's prepare() raises. Case 1 must
    survive in the JSON on disk, case 2 must be recorded as an eval-level
    invalid run with the exception preserved, and nothing may be attributed to
    the model. One harness error is not enough to stop; the next case runs.
    """
    import json
    import run_evals
    from run_evals import run_all, run_case
    from local_agent.config import ModelConfig

    cases = [_case("test-failure-fix"), _case("clean-build"), _case("navigation")]
    real_prepare = run_evals.prepare
    calls = {"n": 0}

    def flaky_prepare(workdir, scenario):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError(13, "Permission denied", "tests")
        return real_prepare(workdir, scenario)

    monkeypatch.setattr(run_evals, "prepare", flaky_prepare)

    def run_one(case, attempt):
        # a fresh scripted model per case; the honest fix for case 1, prose after
        client = _honest_client() if case.name == "test-failure-fix" else \
            ScriptedClient([ChatResponse(content="looked around, nothing to report")])
        return run_case(case, ModelConfig(), tmp_path, auto_approve=True, client=client)

    out = tmp_path / "results.json"
    echoed: list[str] = []
    rows, stopped = run_all(cases, 1, run_one, out, {"label": "t"}, echo=echoed.append)

    assert stopped is False                       # one harness error does not stop the suite
    assert [r["case"] for r in rows] == ["test-failure-fix", "clean-build", "navigation"]

    on_disk = json.loads(out.read_text())
    assert on_disk["complete"] is False           # main() writes the final one
    assert [r["case"] for r in on_disk["rows"]] == [r["case"] for r in rows]

    first = on_disk["rows"][0]
    assert first["counted"] is True and first["validity"] == "valid"

    second = on_disk["rows"][1]
    assert second["outcome"] is None              # the model never had a chance
    assert second["validity"] == "invalid_harness_error"
    assert second["error_locus"] == "harness"
    assert second["error_type"] == "PermissionError"
    assert "Permission denied" in second["traceback"]
    assert second["counted"] is False and second["succeeded"] is False

    third = on_disk["rows"][2]
    assert third["error"] is None                 # the suite carried on

    # and the ledger keeps the harness error out of every denominator
    from run_evals import build_ledger
    ledger = build_ledger(on_disk["rows"])
    assert ledger["tasks_submitted"] == 3
    assert ledger["tasks"] == 2
    assert ledger["errors"] == [{"case": "clean-build", "locus": "harness",
                                 "error": "harness: PermissionError: [Errno 13] Permission denied: 'tests'"}]
    assert not (out.with_suffix(".json.tmp")).exists()   # atomic write left no temp file


def test_two_harness_errors_in_a_row_stop_the_suite_with_rows_preserved(tmp_path):
    import json
    from run_evals import run_all

    cases = [_case("clean-build"), _case("navigation"), _case("segfault")]
    seen = []

    def run_one(case, attempt):
        seen.append(case.name)
        from run_evals import _error_row
        import time
        return _error_row(case, "harness", RuntimeError("disk on fire"), "tb", time.monotonic())

    out = tmp_path / "r.json"
    rows, stopped = run_all(cases, 1, run_one, out, {"label": "t"}, echo=lambda *_: None)
    assert stopped is True
    assert seen == ["clean-build", "navigation"]    # the third never ran
    assert len(json.loads(out.read_text())["rows"]) == 2


# ------------------------------------------------------ reporter: audit fields


def test_rows_carry_what_an_audit_needs(tmp_path):
    """Every hypothesis in the slice-3 audit would have been a fact with these."""
    import json
    from run_evals import run_case
    from local_agent.config import ModelConfig

    out = tmp_path / "r.json"
    row = run_case(_case("test-failure-fix"), ModelConfig(), tmp_path,
                   auto_approve=True, client=_honest_client(), transcript_out=out, attempt=0)
    assert row["claim"] == "success"
    assert row["verified"] is True
    assert row["cited_correctly"] is True
    assert row["mutation_epoch"] == 1
    names = [h["name"] for h in row["history"]]
    assert names == ["propose_patch", "apply_patch", "configure_project", "build_target", "run_test"]
    assert row["history"][1]["epoch"] == 0 and row["history"][3]["epoch"] == 1
    assert all({"arguments", "execution", "domain", "reason", "summary"} <= set(h) for h in row["history"])
    assert row["transcript"] is None          # succeeded rows keep no transcript

    cheat = run_case(_case("test-failure-fix"), ModelConfig(), tmp_path,
                     auto_approve=True, client=_cheat_client(), transcript_out=out, attempt=0)
    assert cheat["transcript"] is not None    # failed rows do
    saved = json.loads(Path(cheat["transcript"]).read_text())
    assert saved["case"] == "test-failure-fix"
    roles = [m["role"] for m in saved["messages"]]
    assert "tool" in roles and "assistant" in roles

    # A controlled probe wants the path taken even when the model gets there.
    probe = run_case(_case("test-failure-fix"), ModelConfig(), tmp_path,
                     auto_approve=True, client=_honest_client(), transcript_out=out,
                     attempt=1, keep_all_transcripts=True)
    assert probe["succeeded"] is True and probe["transcript"] is not None
    assert Path(probe["transcript"]).name == "test-failure-fix-1.json"


def test_a_diagnosis_task_cannot_edit_the_tree_at_all(tmp_path):
    """Right answer, wrong behaviour. 'Tell me what is wrong' is not 'change it'.

    diagnose-test-failure used to offer propose_patch and apply_patch and to
    instruct their use in step 7, and the 30B did exactly as it was told: it
    diagnosed the defect correctly, patched it, and then could not rebuild,
    because the same skill has no build_target. Now the tools are not offered,
    so the attempt is refused as tool_not_allowed and the tree is untouched.
    """
    from run_evals import run_case
    from local_agent.config import ModelConfig

    def reaches_for_a_patch(messages):
        tools = [m for m in messages if m.get("role") == "tool"]
        if not tools:
            return ChatResponse(tool_calls=[
                tool_call("run_test", {"name_filter": "ring_buffer"}, "c1")])
        if len(tools) == 1:
            return ChatResponse(tool_calls=[tool_call("propose_patch", {
                "path": "src/ring_buffer.cpp",
                "find": "return count_ + 1 == slots_.size();",
                "replace": "return count_ == slots_.size();",
            }, "c2")])
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "diagnosis",
            "summary": "ring_buffer: full() is off by one, count_ + 1 == size.",
            "evidence_ids": ["c1"],
        }, "c3")])

    row = run_case(_case("test-failure-diagnose"), ModelConfig(), tmp_path,
                   auto_approve=True, client=ScriptedClient([reaches_for_a_patch] * 6))
    assert row["scope_violation"] is False        # nothing forbidden ever executed
    assert row["forbidden_calls"] == []
    assert "propose_patch" in row["tool_not_allowed_calls"]
    assert row["required_checks"]["did not modify the repository"] is True
    assert row["mutation_epoch"] == 0


def test_scope_violation_still_fires_where_a_skill_does_offer_the_tools(tmp_path):
    """Narrowing is the first defence, not the only one. A case that forbids a
    tool its skill happens to offer must still fail on scope."""
    import dataclasses
    from run_evals import run_case
    from eval_cases import MUTATING_TOOLS
    from local_agent.config import ModelConfig

    case = dataclasses.replace(_case("test-failure-fix"), forbidden_tools=MUTATING_TOOLS)
    row = run_case(case, ModelConfig(), tmp_path, auto_approve=True, client=_honest_client())
    assert row["scope_violation"] is True
    assert set(row["forbidden_calls"]) == {"propose_patch", "apply_patch"}
    assert row["succeeded"] is False


def test_ledger_counts_model_calls_for_a_single_client_run():
    from run_evals import build_ledger

    rows = [
        {"case": "a", "outcome": "pass", "succeeded": True, "counted": True, "validity": "valid",
         "elapsed_s": 5, "metrics": {"llm_calls": 5, "tiers": None},
         "routing": {"final_tier": "cheap", "escalated": False}},
        {"case": "b", "outcome": "fail", "succeeded": False, "counted": True, "validity": "valid",
         "elapsed_s": 5, "metrics": {"llm_calls": 16, "tiers": None},
         "routing": {"final_tier": "cheap", "escalated": False}},
    ]
    ledger = build_ledger(rows)
    assert ledger["calls_by_tier"] == {"cheap": 21, "strong": 0}   # was 0 and 0
    assert ledger["cheap_call_share"] == 1.0


def test_console_mark_follows_succeeded_not_score(tmp_path):
    """navigation, first real run: 'PASS' printed, 'fail' counted."""
    from run_evals import run_all

    def run_one(case, attempt):
        return {"case": case.name, "score": 1.0, "outcome": "fail", "succeeded": False,
                "counted": True, "validity": "valid", "elapsed_s": 1.0, "tool_calls": 3}

    echoed: list[str] = []
    run_all([_case("navigation")], 1, run_one, tmp_path / "o.json", {"label": "t"}, echo=echoed.append)
    line = next(l for l in echoed if "navigation" in l)
    assert line.startswith("  FAIL")
    assert "score=1.00" in line and "outcome=fail" in line


# ------------------------------------------------- preconditions

def test_a_diagnosis_case_starts_from_a_built_tree(tmp_path):
    """The task says the ring_buffer test is failing. That must be true when
    the model arrives.

    The first controlled probe on the 30B spent all seven of its tool calls on
    run_test, every one answered "the project is not configured or not built",
    and was scored as a failed diagnosis. It was not a diagnosis at all: under
    diagnose-test-failure the model has no configure_project and no
    build_target, so the state it was asked to explain could not be reached
    from where it was put. This test is the thing that would have caught it.
    """
    from run_evals import establish, prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    case = _case("test-failure-diagnose")
    assert case.precondition == "built"

    root, _ = prepare(tmp_path, case.scenario)
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)

    # Before: exactly what the probe saw.
    cold = registry.get("run_test").handler(name_filter="ring_buffer")
    assert not cold.ok
    assert "not configured or not built" in cold.summary

    setup = establish(case, registry)
    assert setup["ok"] and setup["tests_registered"] >= 1

    # After: the evidence the task presupposes, which is the whole point.
    hot = registry.get("run_test").handler(name_filter="ring_buffer")
    assert hot.ran and not hot.ok
    assert hot.data["failed"], "the ring_buffer test must actually fail"
    assert any(f["name"] == "ring_buffer" for f in hot.data["failed"])
    # The three things the diagnosis question presupposes, in the model's
    # first tool result: which test, what it did, and where to look.
    assert "ring_buffer" in hot.summary
    assertion = hot.data["assertions"][0]["text"]
    assert "test_ring_buffer.cpp:14" in assertion
    assert "buffer.push(3)" in assertion


def test_a_build_case_is_not_pre_built(tmp_path):
    """Building a broken build before the model sees it would delete the task."""
    from run_evals import establish, prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    case = _case("compile-error-locate")
    assert case.precondition == "none"
    root, _ = prepare(tmp_path, case.scenario)
    registry, _, _ = build_registry(load_repo_config(root))
    assert establish(case, registry) == {"precondition": "none", "ok": True}
    assert not (root / "build").exists()


def test_a_fixture_that_cannot_reach_its_own_start_state_is_not_a_model_result(tmp_path):
    """A precondition failure is loud, uncounted, and stops the suite. It is
    not a zero: a zero would be a claim about the model."""
    import json
    from run_evals import run_all, run_case, PreconditionError, _error_row
    from local_agent.config import ModelConfig

    case = _case("test-failure-diagnose")

    def boom(_case, _attempt=0):
        try:
            raise PreconditionError("build failed: no compiler")
        except PreconditionError as exc:
            row = _error_row(_case, "precondition", exc, "tb", 0.0)
            row["precondition"] = _case.precondition
            return row

    out = tmp_path / "r.json"
    rows, stopped = run_all([case, case, case], 1, boom, out, {"label": "x"}, lambda *_: None)
    assert stopped is True                      # never limped on to a third
    assert len(rows) == 2
    assert all(r["validity"] == "invalid_precondition_error" for r in rows)
    assert all(r["counted"] is False for r in rows)
    saved = json.loads(out.read_text())
    assert len(saved["rows"]) == 2


def test_the_rerun_probe_reaches_the_question_we_meant_to_ask(tmp_path):
    """End to end through run_case: with the precondition established, the
    model's FIRST run_test returns the ring_buffer failure, the assertion and
    the source line. That is the acceptance criterion for the rerun, checked
    here rather than hoped for on the NUC."""
    from run_evals import run_case
    from local_agent.config import ModelConfig

    seen = {}

    def diagnose(messages):
        tools = [m for m in messages if m.get("role") == "tool"]
        if not tools:
            return ChatResponse(tool_calls=[
                tool_call("run_test", {"name_filter": "ring_buffer"}, "c1")])
        seen["first_tool_result"] = tools[0]["content"]
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "diagnosis",
            "summary": "The ring_buffer test fails: push(3) returns false because "
                       "the buffer reports full one slot early, an off by one in "
                       "the capacity check.",
            "evidence_ids": ["run_test:1"],
        }, "c2")])

    row = run_case(_case("test-failure-diagnose"), ModelConfig(), tmp_path,
                   auto_approve=True, client=ScriptedClient([diagnose] * 4))

    assert row["precondition"]["ok"] is True
    body = seen["first_tool_result"]
    assert "not configured or not built" not in body
    assert "ring_buffer" in body
    assert "test_ring_buffer.cpp:14" in body
    assert "buffer.push(3)" in body
    assert row["tool_calls"] == 1
    assert row["required_checks"]["reproduced the failure"] is True
    assert row["checks"]["named the test"] is True
    assert row["checks"]["identified the predicate"] is True
    assert row["checks"]["did not halt"] is True
    assert row["required_ok"] is True and row["claim_ok"] is True
    assert row["succeeded"] is True


def test_run_test_says_when_it_is_reporting_a_stale_binary(tmp_path):
    """ctest does not compile. A source edit followed by run_test returns the
    OLD binary's result, byte for byte identical to the one before the edit.

    The 30B hit this and could not get out: it fixed the defect correctly,
    reran, saw the same failure, reread its own file, saw the fix was there,
    reran, and looped until the repeat guard stopped it. Ten tool calls and
    about seven minutes, all of them spent on a lie of omission by this tool.
    """
    from run_evals import establish, prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    case = _case("test-failure-diagnose")
    root, _ = prepare(tmp_path, case.scenario)
    registry, _, _ = build_registry(load_repo_config(root))
    establish(case, registry)

    fresh = registry.get("run_test").handler(name_filter="ring_buffer")
    assert not fresh.ok and "STALE" not in fresh.summary
    assert fresh.data["stale_sources"] == []

    source = root / "src" / "ring_buffer.cpp"
    source.write_text(source.read_text().replace(
        "return count_ + 1 == slots_.size();", "return count_ == slots_.size();"))

    stale = registry.get("run_test").handler(name_filter="ring_buffer")
    assert stale.summary.startswith("STALE:")
    assert "src/ring_buffer.cpp" in stale.summary
    assert "build_target" in stale.summary
    assert stale.data["stale_sources"] == ["src/ring_buffer.cpp"]

    # And after a rebuild the note is gone, because it is no longer true.
    assert registry.get("build_target").handler().ok
    rebuilt = registry.get("run_test").handler(name_filter="ring_buffer")
    assert "STALE" not in rebuilt.summary
    assert rebuilt.ok, rebuilt.summary


# ------------------------------------------------- Slice 4: a fixture that
# ------------------------------------------------- keeps its own answers

def test_the_worktree_contains_no_answer_key(tmp_path):
    """In the Slice 3 dataset the model read `scenarios/clean/src/ring_buffer.cpp`,
    the correct implementation, while diagnosing the link error, and cited it in
    its answer. Diffing the working tree against a pristine copy of every file
    solves compile, link and test scenarios without understanding them, which
    would flatter exactly the cells the experiment has to measure honestly."""
    from run_evals import prepare

    for scenario in ("clean", "compile_error", "link_error", "test_failure",
                     "crash", "timeout"):
        root, oracle_dir = prepare(tmp_path, scenario)
        assert not (root / "scenarios").exists(), scenario
        assert not (root / "scripts").exists(), scenario
        # Nothing anywhere under the worktree, whatever it is called.
        leaked = [p for p in root.rglob("*")
                  if "scenario" in p.name.lower() and ".git" not in p.parts]
        assert leaked == [], (scenario, leaked)
        # And the oracle is still outside it.
        assert oracle_dir.resolve() not in root.resolve().parents
        assert not str(oracle_dir.resolve()).startswith(str(root.resolve()))


def test_the_scenario_is_still_applied_and_still_visible_in_the_diff(tmp_path):
    """Removing the answer key must not remove the evidence. `git_diff` showing
    the injected defect is realistic and is the same in every cell; a pristine
    copy of every other file beside it is neither."""
    import subprocess
    from run_evals import prepare

    root, _ = prepare(tmp_path, "test_failure")
    assert "count_ + 1 == slots_.size()" in (root / "src" / "ring_buffer.cpp").read_text()
    diff = subprocess.run(["git", "diff"], cwd=root, capture_output=True, text=True).stdout
    assert "-bool RingBuffer::full() const { return count_ == slots_.size(); }" in diff
    assert "+bool RingBuffer::full() const { return count_ + 1 == slots_.size(); }" in diff

    # The baseline commit is the clean tree, whatever the package happens to
    # have committed at its top level.
    show = subprocess.run(["git", "show", "HEAD:src/ring_buffer.cpp"],
                          cwd=root, capture_output=True, text=True).stdout
    assert "count_ == slots_.size()" in show and "count_ + 1" not in show


def test_a_filter_that_matches_nothing_is_not_a_pass(tmp_path):
    """`run_test(name_filter="Crash")` returned "all tests passed (0 test(s))".
    Nothing ran. The 30B was sent down this hole twice in the Slice 3 run and
    spent four calls climbing out of it."""
    from run_evals import establish, prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    case = _case("test-failure-diagnose")
    root, _ = prepare(tmp_path, case.scenario)
    registry, _, _ = build_registry(load_repo_config(root))
    establish(case, registry)

    empty = registry.get("run_test").handler(name_filter="Crash")
    assert empty.ok is False
    assert empty.domain_status.value == "unknown"     # not pass, not fail
    assert empty.data["ran_nothing"] is True
    assert "ran 0 tests" in empty.summary and "'Crash'" in empty.summary
    assert "list_tests" in empty.summary

    real = registry.get("run_test").handler(name_filter="ring_buffer")
    assert real.data["ran_nothing"] is False and real.domain_status.value == "fail"


def test_legal_ctest_regexes_are_accepted(tmp_path):
    """`.*timeout.*` is a valid ctest pattern and was refused as invalid."""
    from local_agent.tools.base import ToolError
    from run_evals import establish, prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    case = _case("test-failure-diagnose")
    root, _ = prepare(tmp_path, case.scenario)
    registry, _, _ = build_registry(load_repo_config(root))
    establish(case, registry)
    run_test = registry.get("run_test").handler

    for pattern in (".*ring.*", "^ring_buffer$", "ring_buffer|slow", ".*[Rr]ing.*"):
        result = run_test(name_filter=pattern)
        assert result.execution_status.value == "ok", (pattern, result.summary)

    for bad, expected in (("x; rm -rf /", "metacharacter"),
                          ("a`b", "metacharacter"),
                          ("(", "not a valid regular expression"),
                          ("x" * 300, "limit")):
        try:
            run_test(name_filter=bad)
        except ToolError as exc:
            assert expected in str(exc), (bad, str(exc))
        else:
            raise AssertionError(f"{bad!r} should have been refused")


# ------------------------------------------------- the control condition

def test_the_no_skill_control_differs_only_in_the_treatment(tmp_path):
    """The control has to remove the three things a skill supplies and nothing
    else. Anything else removed with them is a second difference, and a two-
    difference comparison cannot attribute an effect to either of them.

    Today `skill=None` fell back to the read-only `_default` toolset, which is
    narrowing under another name: a control that cannot call build_target has
    not been denied a procedure, it has been denied the job.
    """
    from run_evals import run_case
    from local_agent.config import ModelConfig

    seen = {}

    def observe(messages):
        seen.setdefault("messages", messages)
        seen.setdefault("system", "\n".join(
            m["content"] for m in messages if m["role"] == "system"))
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "diagnosis", "summary": "ring_buffer full() is off by one",
        }, "c1")])

    row = run_case(_case("test-failure-diagnose"), ModelConfig(), tmp_path,
                   auto_approve=True, client=ScriptedClient([observe] * 3),
                   no_skill=True)

    assert row["condition"] == "control"
    assert row["skill"] is None
    # No procedure.
    assert "Active skill:" not in seen["system"]
    assert "Reproduce\n   before theorising" not in seen["system"]
    # No catalogue.
    assert "Available skills:" not in seen["system"]
    assert "diagnose-test-failure" not in seen["system"]
    # No narrowing is asserted properly in
    # test_the_control_sees_every_registered_tool; what belongs here is that
    # the run itself was well formed.
    assert row["precondition"]["ok"] is True
    assert row["error"] is None

    # Everything else identical: same rules of engagement, same repository
    # facts, same policy.
    assert "You are a local engineering agent" in seen["system"]
    assert "Repository: cpp-sandbox" in seen["system"]
    assert "Permissions:" in seen["system"]


def test_the_control_sees_every_registered_tool(tmp_path):
    from run_evals import prepare
    from local_agent.agent import Orchestrator, SkillLibrary
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    root, _ = prepare(tmp_path, "test_failure")
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)
    orch = Orchestrator(
        repo=repo, registry=registry, client=ScriptedClient([ChatResponse(content="x")]),
        skills=SkillLibrary.discover(REPO / ".github" / "skills"),
        approval=lambda *a: True,
    )
    treated = orch._toolset_for("diagnose-test-failure",
                                orch.skills.get("diagnose-test-failure"))
    control = orch._toolset_for(None, None, no_skill=True)

    assert set(control) == set(registry.names())
    assert set(treated) < set(control), "the treatment must be the narrower world"
    assert "build_target" in control and "build_target" not in treated


def test_the_control_is_held_to_the_same_success_bar(sandbox):
    """verification_required comes from the case, not from a skill that the
    control does not have. Otherwise the control could claim success without
    building and be graded more leniently for it."""
    from local_agent.agent.outcome import Outcome

    turns = [ChatResponse(tool_calls=[tool_call("submit_answer", {
        "claim": "success", "summary": "fixed it, trust me",
    }, "c1")])]
    result = _orch_no_skill(sandbox.root, ScriptedClient(turns))
    assert result.state.claim == "success"
    assert result.state.verified is False
    assert result.outcome is not Outcome.PASS


def _orch_no_skill(root, client):
    from local_agent.agent import Orchestrator, SkillLibrary
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)
    orch = Orchestrator(
        repo=repo, registry=registry, client=client,
        skills=SkillLibrary.discover(REPO / ".github" / "skills"),
        approval=lambda *a: True,
    )
    return orch.run("why does ring_buffer fail", no_skill=True,
                    verification_required=True)


def test_the_three_conditions_differ_by_exactly_one_thing_each(tmp_path):
    """control -> narrow is tools. narrow -> skill is procedure.

    Two cells could only say whether the whole package helps. Every scope
    violation in the first control run was a reach for a tool the treatment
    withholds, so the two components plainly do not contribute equally, and a
    design that cannot separate them cannot say which one the NPU story rests
    on.
    """
    from run_evals import run_case
    from local_agent.config import ModelConfig

    seen = {}

    def observe(messages):
        key = observe.condition
        seen.setdefault(key, "\n".join(
            m["content"] for m in messages if m["role"] == "system"))
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "diagnosis", "summary": "ring_buffer full() is off by one, capacity",
        }, "c1")])

    rows = {}
    for condition in ("control", "narrow", "skill"):
        observe.condition = condition
        rows[condition] = run_case(
            _case("test-failure-diagnose"), ModelConfig(), tmp_path,
            auto_approve=True, client=ScriptedClient([observe] * 3),
            condition=condition)

    # The procedure appears in exactly one of them.
    assert "Active skill: diagnose-test-failure" in seen["skill"]
    assert "Active skill:" not in seen["narrow"]
    assert "Active skill:" not in seen["control"]
    assert "Classifying by status" not in seen["narrow"]

    # The toolset narrows in exactly one place: control -> narrow.
    control, narrow, skill = (set(rows[c]["offered_tools"]) for c in
                              ("control", "narrow", "skill"))
    assert narrow == skill, "narrow and skill must offer the identical toolset"
    assert narrow < control, "narrowing must be a strict reduction"
    assert "propose_patch" in control and "propose_patch" not in narrow

    # The catalogue is absent from all three, so skill minus narrow is
    # procedure and not procedure plus catalogue.
    for condition in ("control", "narrow", "skill"):
        assert "Available skills:" not in seen[condition], condition
        assert rows[condition]["catalogue"] is False

    # And the bookkeeping says which skill shaped the tools even when its body
    # never spoke.
    assert rows["narrow"]["skill"] is None
    assert rows["narrow"]["narrowed_by"] == "diagnose-test-failure"
    assert rows["skill"]["skill"] == "diagnose-test-failure"
    assert rows["control"]["narrowed_by"] is None


def test_how_a_run_finished_is_recorded(tmp_path):
    """Every condition is told to submit, so prose is a failure to follow the
    shared protocol rather than a choice between two sanctioned endings.
    Recorded because a condition that forgets it more often is a real
    finding."""
    from run_evals import run_case
    from local_agent.config import ModelConfig

    structured = run_case(_case("navigation"), ModelConfig(), tmp_path,
                          auto_approve=True, client=ScriptedClient([lambda m: ChatResponse(
                              tool_calls=[tool_call("read_file", {
                                  "path": "include/sandbox/ring_buffer.hpp"}, "c1")])
                              if not [x for x in m if x.get("role") == "tool"] else
                              ChatResponse(tool_calls=[tool_call("submit_answer", {
                                  "claim": "diagnosis",
                                  "summary": "ring_buffer.hpp; push returns false"}, "c2")])] * 4))
    assert structured["submission_mode"] == "structured"

    prose = run_case(_case("navigation"), ModelConfig(), tmp_path,
                     auto_approve=True, client=ScriptedClient([lambda m: ChatResponse(
                         tool_calls=[tool_call("read_file", {
                             "path": "include/sandbox/ring_buffer.hpp"}, "c1")])
                         if not [x for x in m if x.get("role") == "tool"] else
                         ChatResponse(content="RingBuffer is in ring_buffer.hpp; "
                                              "push returns false when full.")] * 4))
    assert prose["submission_mode"] == "prose"
    # The finishing protocol is in the prompt every condition receives, so an
    # unclaimed answer is an unfinished one. It used to pass: a run that did
    # every step correctly and then wrote "the fix did not work and the tests
    # are still failing" was recorded as a success, because there was no claim
    # for the evidence to contradict.
    assert prose["claim_ok"] is False
    assert prose["succeeded"] is False
