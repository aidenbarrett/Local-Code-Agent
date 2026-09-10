"""Slice 2: the evaluation cannot be satisfied by editing the oracle.

Drives the real eval harness (run_evaluation.run_case) with a scripted model that
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
sys.path.insert(0, str(REPO / "evaluation"))

from local_agent.llm.client import ScriptedClient, tool_call  # noqa: E402
from local_agent.llm.models import ChatResponse  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("ctest") is None,
    reason="needs cmake and ctest",
)


def _case(name):
    from task_contracts import CASES
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
    from run_evaluation import run_case
    from local_agent.config import ModelConfig

    row = run_case(_case("test-failure-fix"), ModelConfig(), tmp_path,
                   auto_approve=True, client=_cheat_client())

    assert "run_test" in row["tools_used"]
    assert row["error"] is None
    assert row["scope_violation"] is False
    assert row["oracle_tampered"] is True
    assert "tests/test_ring_buffer.cpp" in row["oracle_tamper"]["modified"]
    assert "return 0;  // nothing to see here" in row["oracle_tamper"]["diffs"]["tests/test_ring_buffer.cpp"]
    assert row["eval_verification"]["tests"] == "fail"
    assert row["verification_disagreement"] is True
    assert row["succeeded"] is False
    assert row["counted"] is False


def test_an_honest_fix_is_counted_and_verified(tmp_path):
    from run_evaluation import run_case
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
    from run_evaluation import build_ledger

    honest = {"case": "a", "outcome": "pass", "succeeded": True, "counted": True,
              "validity": "valid", "oracle_tampered": False, "elapsed_s": 10}
    cheat = {"case": "b", "outcome": "pass", "succeeded": False, "counted": False,
             "validity": "valid", "oracle_tampered": True, "elapsed_s": 5}
    dead = {"case": "c", "outcome": "blocked", "succeeded": False, "counted": False,
            "validity": "invalid_server_unavailable", "oracle_tampered": False, "elapsed_s": 1}

    ledger = build_ledger([honest, cheat, dead])
    assert ledger["tasks_submitted"] == 3
    assert ledger["tasks"] == 1
    assert ledger["excluded"] == 2
    assert ledger["oracle_tampered_cases"] == ["b"]
    assert ledger["invalid_cases"] == [{"case": "c", "validity": "invalid_server_unavailable"}]
    assert ledger["local_success_rate"] == 1.0
    assert ledger["end_to_end_rate"] == 1.0


def test_oracle_snapshot_is_outside_the_worktree_and_read_only(tmp_path):
    import os
    import oracle
    from run_evaluation import prepare

    root, oracle_dir = prepare(tmp_path, "test_failure")
    assert not str(oracle_dir).startswith(str(root))
    probe = oracle_dir / "tests" / "test_ring_buffer.cpp"
    assert probe.exists()
    if os.name != "nt" and os.geteuid() != 0:
        assert not os.access(probe, os.W_OK)
    assert not (probe.stat().st_mode & 0o222), "oracle copy should have no write bits"

    (root / "tests" / "test_ring_buffer.cpp").write_text("int main(){return 0;}\n")
    (root / "tests" / "test_fd_owner.cpp").unlink()
    (root / "tests" / "test_sneaky.cpp").write_text("int main(){return 0;}\n")
    report = oracle.compare(root, oracle_dir)
    assert report.tampered
    assert report.modified == ["tests/test_ring_buffer.cpp"]
    assert report.deleted == ["tests/test_fd_owner.cpp"]
    assert report.added == ["tests/test_sneaky.cpp"]

    oracle.restore(root, oracle_dir)
    assert not oracle.compare(root, oracle_dir).tampered
    assert not (root / "tests" / "test_sneaky.cpp").exists()


def test_a_second_case_can_replace_the_previous_oracle(tmp_path):
    from run_evaluation import prepare

    root1, oracle1 = prepare(tmp_path, "test_failure")
    root2, oracle2 = prepare(tmp_path, "compile_error")
    assert oracle1 == oracle2
    assert (oracle2 / "tests" / "test_ring_buffer.cpp").is_file()
    prepare(tmp_path, "clean")


def test_a_harness_crash_on_case_two_keeps_case_one_and_is_not_a_model_failure(tmp_path, monkeypatch):
    import json
    import run_evaluation
    from run_evaluation import run_all, run_case
    from local_agent.config import ModelConfig

    cases = [_case("test-failure-fix"), _case("clean-build"), _case("navigation")]
    real_prepare = run_evaluation.prepare
    calls = {"n": 0}

    def flaky_prepare(workdir, scenario):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError(13, "Permission denied", "tests")
        return real_prepare(workdir, scenario)

    monkeypatch.setattr(run_evaluation, "prepare", flaky_prepare)

    def run_one(case, attempt):
        client = _honest_client() if case.name == "test-failure-fix" else \
            ScriptedClient([ChatResponse(content="looked around, nothing to report")])
        return run_case(case, ModelConfig(), tmp_path, auto_approve=True, client=client)

    out = tmp_path / "results.json"
    echoed: list[str] = []
    rows, stopped = run_all(cases, 1, run_one, out, {"label": "t"}, echo=echoed.append)

    assert stopped is False
    assert [r["case"] for r in rows] == ["test-failure-fix", "clean-build", "navigation"]

    on_disk = json.loads(out.read_text())
    assert on_disk["complete"] is False
    assert [r["case"] for r in on_disk["rows"]] == [r["case"] for r in rows]

    first = on_disk["rows"][0]
    assert first["counted"] is True and first["validity"] == "valid"

    second = on_disk["rows"][1]
    assert second["outcome"] is None
    assert second["validity"] == "invalid_harness_error"
    assert second["error_locus"] == "harness"
    assert second["error_type"] == "PermissionError"
    assert "Permission denied" in second["traceback"]
    assert second["counted"] is False and second["succeeded"] is False

    third = on_disk["rows"][2]
    assert third["error"] is None

    from run_evaluation import build_ledger
    ledger = build_ledger(on_disk["rows"])
    assert ledger["tasks_submitted"] == 3
    assert ledger["tasks"] == 2
    assert ledger["errors"] == [{"case": "clean-build", "locus": "harness",
                                 "error": "harness: PermissionError: [Errno 13] Permission denied: 'tests'"}]
    assert not (out.with_suffix(".json.tmp")).exists()


def test_two_harness_errors_in_a_row_stop_the_suite_with_rows_preserved(tmp_path):
    import json
    from run_evaluation import run_all

    cases = [_case("clean-build"), _case("navigation"), _case("segfault")]
    seen = []

    def run_one(case, attempt):
        seen.append(case.name)
        from run_evaluation import _error_row
        import time
        return _error_row(case, "harness", RuntimeError("disk on fire"), "tb", time.monotonic())

    out = tmp_path / "r.json"
    rows, stopped = run_all(cases, 1, run_one, out, {"label": "t"}, echo=lambda *_: None)
    assert stopped is True
    assert seen == ["clean-build", "navigation"]
    assert len(json.loads(out.read_text())["rows"]) == 2


def test_rows_carry_what_an_audit_needs(tmp_path):
    import json
    from run_evaluation import run_case
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
    assert row["transcript"] is None

    cheat = run_case(_case("test-failure-fix"), ModelConfig(), tmp_path,
                     auto_approve=True, client=_cheat_client(), transcript_out=out, attempt=0)
    assert cheat["transcript"] is not None
    saved = json.loads(Path(cheat["transcript"]).read_text())
    assert saved["case"] == "test-failure-fix"
    roles = [m["role"] for m in saved["messages"]]
    assert "tool" in roles and "assistant" in roles

    probe = run_case(_case("test-failure-fix"), ModelConfig(), tmp_path,
                     auto_approve=True, client=_honest_client(), transcript_out=out,
                     attempt=1, keep_all_transcripts=True)
    assert probe["succeeded"] is True and probe["transcript"] is not None
    assert Path(probe["transcript"]).name == "test-failure-fix-1.json"


def test_a_diagnosis_task_cannot_edit_the_tree_at_all(tmp_path):
    from run_evaluation import run_case
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
    assert row["scope_violation"] is False
    assert row["forbidden_calls"] == []
    assert "propose_patch" in row["tool_not_allowed_calls"]
    assert row["required_checks"]["did not modify the repository"] is True
    assert row["mutation_epoch"] == 0


def test_scope_violation_still_fires_where_a_skill_does_offer_the_tools(tmp_path):
    import dataclasses
    from run_evaluation import run_case
    from task_contracts import MUTATING_TOOLS
    from local_agent.config import ModelConfig

    case = dataclasses.replace(_case("test-failure-fix"), forbidden_tools=MUTATING_TOOLS)
    row = run_case(case, ModelConfig(), tmp_path, auto_approve=True, client=_honest_client())
    assert row["scope_violation"] is True
    assert set(row["forbidden_calls"]) == {"propose_patch", "apply_patch"}
    assert row["succeeded"] is False


def test_ledger_counts_model_calls_for_a_single_client_run():
    from run_evaluation import build_ledger

    rows = [
        {"case": "a", "outcome": "pass", "succeeded": True, "counted": True, "validity": "valid",
         "elapsed_s": 5, "metrics": {"llm_calls": 5, "tiers": None},
         "routing": {"final_tier": "cheap", "escalated": False}},
        {"case": "b", "outcome": "fail", "succeeded": False, "counted": True, "validity": "valid",
         "elapsed_s": 5, "metrics": {"llm_calls": 16, "tiers": None},
         "routing": {"final_tier": "cheap", "escalated": False}},
    ]
    ledger = build_ledger(rows)
    assert ledger["calls_by_tier"] == {"cheap": 21, "strong": 0}
    assert ledger["cheap_call_share"] == 1.0


def test_console_mark_follows_succeeded_not_score(tmp_path):
    from run_evaluation import run_all

    def run_one(case, attempt):
        return {"case": case.name, "score": 1.0, "outcome": "fail", "succeeded": False,
                "counted": True, "validity": "valid", "elapsed_s": 1.0, "tool_calls": 3}

    echoed: list[str] = []
    run_all([_case("navigation")], 1, run_one, tmp_path / "o.json", {"label": "t"}, echo=echoed.append)
    line = next(l for l in echoed if "navigation" in l)
    assert line.startswith("  FAIL")
    assert "score=1.00" in line and "outcome=fail" in line


def test_a_diagnosis_case_starts_from_a_built_tree(tmp_path):
    from run_evaluation import establish, prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    case = _case("test-failure-diagnose")
    assert case.precondition == "built"

    root, _ = prepare(tmp_path, case.scenario)
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)

    cold = registry.get("run_test").handler(name_filter="ring_buffer")
    assert not cold.ok
    assert "not configured or not built" in cold.summary

    setup = establish(case, registry)
    assert setup["ok"] and setup["tests_registered"] >= 1

    hot = registry.get("run_test").handler(name_filter="ring_buffer")
    assert hot.ran and not hot.ok
    assert hot.data["failed"], "the ring_buffer test must actually fail"
    assert any(f["name"] == "ring_buffer" for f in hot.data["failed"])
    assert "ring_buffer" in hot.summary
    assertion = hot.data["assertions"][0]["text"]
    assert "test_ring_buffer.cpp" in assertion
    assert "buffer.push(3)" in assertion


def test_a_build_case_is_not_pre_built(tmp_path):
    from run_evaluation import establish, prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    case = _case("compile-error-locate")
    assert case.precondition == "none"
    root, _ = prepare(tmp_path, case.scenario)
    registry, _, _ = build_registry(load_repo_config(root))
    assert establish(case, registry) == {"precondition": "none", "ok": True}
    assert not (root / "build").exists()


def test_a_fixture_that_cannot_reach_its_own_start_state_is_not_a_model_result(tmp_path):
    import json
    from run_evaluation import run_all, PreconditionError, _error_row

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
    assert stopped is True
    assert len(rows) == 2
    assert all(r["validity"] == "invalid_precondition_error" for r in rows)
    assert all(r["counted"] is False for r in rows)
    saved = json.loads(out.read_text())
    assert len(saved["rows"]) == 2


def test_the_rerun_probe_reaches_the_question_we_meant_to_ask(tmp_path):
    from run_evaluation import run_case
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
    assert "test_ring_buffer.cpp" in body
    assert "buffer.push(3)" in body
    assert row["tool_calls"] == 1
    assert row["required_checks"]["reproduced the failure"] is True
    assert row["checks"]["named the test"] is True
    assert row["checks"]["identified the predicate"] is True
    assert row["checks"]["did not halt"] is True
    assert row["required_ok"] is True and row["claim_ok"] is True
    assert row["succeeded"] is True


def test_run_test_says_when_it_is_reporting_a_stale_binary(tmp_path):
    from run_evaluation import establish, prepare
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

    assert registry.get("build_target").handler().ok
    rebuilt = registry.get("run_test").handler(name_filter="ring_buffer")
    assert "STALE" not in rebuilt.summary
    assert rebuilt.ok, rebuilt.summary


def test_the_worktree_contains_no_answer_key(tmp_path):
    from run_evaluation import prepare

    for scenario in ("clean", "compile_error", "link_error", "test_failure",
                     "crash", "timeout"):
        root, oracle_dir = prepare(tmp_path, scenario)
        assert not (root / "scenarios").exists(), scenario
        assert not (root / "scripts").exists(), scenario
        leaked = [p for p in root.rglob("*")
                  if "scenario" in p.name.lower() and ".git" not in p.parts]
        assert leaked == [], (scenario, leaked)
        assert oracle_dir.resolve() not in root.resolve().parents
        assert not str(oracle_dir.resolve()).startswith(str(root.resolve()))


def test_the_scenario_is_still_applied_and_still_visible_in_the_diff(tmp_path):
    import subprocess
    from run_evaluation import prepare

    root, _ = prepare(tmp_path, "test_failure")
    assert "count_ + 1 == slots_.size()" in (root / "src" / "ring_buffer.cpp").read_text()
    diff = subprocess.run(["git", "diff"], cwd=root, capture_output=True, text=True).stdout
    assert "-bool RingBuffer::full() const { return count_ == slots_.size(); }" in diff
    assert "+bool RingBuffer::full() const { return count_ + 1 == slots_.size(); }" in diff

    show = subprocess.run(["git", "show", "HEAD:src/ring_buffer.cpp"],
                          cwd=root, capture_output=True, text=True).stdout
    assert "count_ == slots_.size()" in show and "count_ + 1" not in show


def test_a_filter_that_matches_nothing_is_not_a_pass(tmp_path):
    from run_evaluation import establish, prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    case = _case("test-failure-diagnose")
    root, _ = prepare(tmp_path, case.scenario)
    registry, _, _ = build_registry(load_repo_config(root))
    establish(case, registry)

    empty = registry.get("run_test").handler(name_filter="Crash")
    assert empty.ok is False
    assert empty.domain_status.value == "unknown"
    assert empty.data["ran_nothing"] is True
    assert "ran 0 tests" in empty.summary and "'Crash'" in empty.summary
    assert "list_tests" in empty.summary

    real = registry.get("run_test").handler(name_filter="ring_buffer")
    assert real.data["ran_nothing"] is False and real.domain_status.value == "fail"


def test_legal_ctest_regexes_are_accepted(tmp_path):
    from local_agent.tools.base import ToolError
    from run_evaluation import establish, prepare
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


def test_the_no_skill_control_differs_only_in_the_treatment(tmp_path):
    from run_evaluation import run_case
    from local_agent.config import ModelConfig

    seen = {}

    def observe(messages):
        key = observe.condition
        seen.setdefault(key, "\n".join(
            m["content"] for m in messages if m["role"] == "system"))
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "diagnosis", "summary": "ring_buffer full() is off by one",
        }, "c1")])

    row = run_case(_case("test-failure-diagnose"), ModelConfig(), tmp_path,
                   auto_approve=True, client=ScriptedClient([observe] * 3),
                   no_skill=True)

    assert row["condition"] == "control"
    assert row["skill"] is None
    assert "Active skill:" not in seen["system"]
    assert "Reproduce\n   before theorising" not in seen["system"]
    assert "Available skills:" not in seen["system"]
    assert "diagnose-test-failure" not in seen["system"]
    assert row["precondition"]["ok"] is True
    assert row["error"] is None
    assert "You are a local engineering agent" in seen["system"]
    assert "Repository: cpp-sandbox" in seen["system"]
    assert "Permissions:" in seen["system"]


def test_the_control_sees_every_registered_tool(tmp_path):
    from run_evaluation import prepare
    from local_agent.agent import Orchestrator, SkillLibrary
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    root, _ = prepare(tmp_path, "test_failure")
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)
    orch = Orchestrator(
        repo=repo, registry=registry, client=ScriptedClient([ChatResponse(content="x")]),
        skills=SkillLibrary.discover(REPO / "skills"),
        approval=lambda *a: True,
    )
    treated = orch._toolset_for("diagnose-test-failure",
                                orch.skills.get("diagnose-test-failure"))
    control = orch._toolset_for(None, None, no_skill=True)

    from local_agent.agent.contracts import REFERENCE_TOOL

    assert set(control) == set(registry.names()) - {REFERENCE_TOOL}
    assert REFERENCE_TOOL not in control
    assert set(treated) < set(control) | {REFERENCE_TOOL}
    assert "build_target" in control and "build_target" not in treated


def test_the_control_is_held_to_the_same_success_bar(sandbox):
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
        skills=SkillLibrary.discover(REPO / "skills"),
        approval=lambda *a: True,
    )
    return orch.run("why does ring_buffer fail", no_skill=True,
                    verification_required=True)


def test_the_three_conditions_differ_by_exactly_one_thing_each(tmp_path):
    from run_evaluation import run_case
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

    assert "Active skill: diagnose-test-failure" in seen["skill"]
    assert "Active skill:" not in seen["narrow"]
    assert "Active skill:" not in seen["control"]
    assert "Classifying by status" not in seen["narrow"]

    control, narrow, skill = (set(rows[c]["offered_tools"]) for c in
                              ("control", "narrow", "skill"))
    assert narrow == skill
    assert narrow < control
    assert "propose_patch" in control and "propose_patch" not in narrow

    for condition in ("control", "narrow", "skill"):
        assert "Available skills:" not in seen[condition], condition
        assert rows[condition]["catalogue"] is False

    assert rows["narrow"]["skill"] is None
    assert rows["narrow"]["narrowed_by"] == "diagnose-test-failure"
    assert rows["skill"]["skill"] == "diagnose-test-failure"
    assert rows["control"]["narrowed_by"] is None


def test_how_a_run_finished_is_recorded(tmp_path):
    from run_evaluation import run_case
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
    assert prose["claim_ok"] is False
    assert prose["succeeded"] is False
