"""The acceptance journey runner: the deterministic journeys, and that it fails closed.

These run the real Session Hub composition against real CMake builds on both CI
platforms, so the runner Aiden starts on the target machine has already been
exercised end to end on Linux and Windows. Model journeys need an endpoint and
must report UNKNOWN, with the reason, when there is none.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "acceptance-journeys.py"
SPEC = importlib.util.spec_from_file_location("lca_acceptance_journeys", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
journeys = importlib.util.module_from_spec(SPEC)
sys.modules["lca_acceptance_journeys"] = journeys
SPEC.loader.exec_module(journeys)

DETERMINISTIC = ["J01-build-pass", "J02-build-fail", "J03-tests-fail", "J04-ambiguous", "J05-stop-build",
                 "J06-authority", "J13-candidate-scripted", "J15-dirty-worktree", "J15b-rename-binary",
                 "J16-malformed-calls", "J17-branch-review", "J19-test-truth", "J19b-test-policy",
                 "J20-conflict-explain", "J21-exact-commit", "J21b-commit-policy",
                 "J22-repo-explain", "J23-symbol-lookup", "J24-continuous-engineering"]


def _report(output: Path) -> dict[str, dict[str, object]]:
    report = json.loads((output / "journeys.json").read_text(encoding="utf-8"))
    assert report["schema"] == "lca.acceptance-journeys/1"
    return {j["id"]: j for j in report["journeys"]}


def _skip_redundant_fixture_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip only the clean probe; selected journeys still run their real builds."""
    monkeypatch.setattr(journeys, "probe_fixture_build", lambda _output: (True, ""))


def test_reusing_output_refuses_before_deleting_previous_evidence(tmp_path, capsys):
    out = tmp_path / "acc"
    locked_object = out / "probe" / ".git" / "objects" / "08" / "previous"
    locked_object.parent.mkdir(parents=True)
    locked_object.write_bytes(b"previous run evidence")
    old_report = out / "journeys.json"
    old_report.write_text('{"previous": true}', encoding="utf-8")

    with pytest.raises(SystemExit) as refusal:
        journeys.main(["--output", str(out)])

    assert refusal.value.code == 2
    assert "choose a new directory" in capsys.readouterr().err
    assert locked_object.read_bytes() == b"previous run evidence"
    assert old_report.read_text(encoding="utf-8") == '{"previous": true}'


def test_candidate_journey_is_incomplete_when_commit_cannot_be_exercised(tmp_path, monkeypatch):
    repo = journeys.make_repo(tmp_path / "repo", "compile_error", allow_commit=True)
    original = (repo / journeys.RING).read_bytes()
    monkeypatch.setattr(journeys, "independent_build", lambda _repo: (True, ""))
    build = journeys.TaskResult("build-task", journeys.TaskOutcome.FAIL, "compile error", False, ())
    fixed = journeys.TaskResult("fixed-task", journeys.TaskOutcome.PASS, "candidate", True, ())
    refused = journeys.TaskResult("refused-task", journeys.TaskOutcome.FAIL, "stale", False, ())
    applied = journeys.TaskResult("applied-task", journeys.TaskOutcome.PASS, "applied", True, ())
    undone = journeys.TaskResult("undone-task", journeys.TaskOutcome.PASS, "undone", True, ())
    calls = iter((
        ("build it", build),
        ("fix it", fixed),
        (f"/diff {fixed.task_id}", None),
        (f"/apply {fixed.task_id}", refused),
        (f"/apply {fixed.task_id}", applied),
        (f"/undo {fixed.task_id}", undone),
        (f"/apply {fixed.task_id}", refused),
        (f"fix task {build.task_id}", refused),
    ))

    class SessionStub:
        def __init__(self):
            self.repo = repo
            self.journey = journeys.Journey("J09-candidate", "candidate controls", "product")

        def turn(self, request):
            expected, result = next(calls)
            assert request == expected
            if request == f"/apply {fixed.task_id}" and result is applied:
                (repo / journeys.RING).write_bytes(original + b"\n// applied\n")
            if request == f"/undo {fixed.task_id}":
                (repo / journeys.RING).write_bytes(original)
            return (journeys.RING if request.startswith("/diff ") else "", result)

    session = SessionStub()
    journeys.j_candidate_lifecycle(session)

    assert session.journey.status == "UNKNOWN"
    assert "/commit was not exercised" in session.journey.reason
    assert any("exact undo passed" in note for note in session.journey.notes)
    assert next(calls, None) is None


def test_the_deterministic_journeys_pass_with_logs_and_no_model(tmp_path):
    out = tmp_path / "acc"
    # Exactly what the user runs without a model: every journey is attempted.
    assert journeys.main(["--output", str(out)]) == 0

    by_id = _report(out)
    for jid in DETERMINISTIC:
        assert by_id[jid]["status"] == "PASS", (jid, by_id[jid]["reason"], by_id[jid]["notes"])
        assert (out / "journeys" / jid / "transcript.txt").read_text(encoding="utf-8").strip()
        assert (out / "journeys" / jid / "events.jsonl").read_text(encoding="utf-8").strip()
    stop = by_id["J05-stop-build"]["measured"]
    assert stop["processes_before_stop"] >= 1 and stop["surviving_processes"] == []
    # Without --allow-model nothing that needs a model runs, and it says why.
    assert by_id["J08-fix-build"]["status"] == "UNKNOWN"
    assert "--allow-model" in by_id["J08-fix-build"]["reason"]
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "Product   PASS 19 / FAIL 0" in summary
    assert "Model     not used" in summary

    report = json.loads((out / "journeys.json").read_text(encoding="utf-8"))
    product = report["preconditions"]["product"]
    assert len(product["source_sha256"]) == 64
    assert f"source sha256 {product['source_sha256'][:16]}" in summary

    rename_transcript = (out / "journeys" / "J15b-rename-binary" / "transcript.txt").read_text(
        encoding="utf-8",
    )
    assert "renamed: README.md -> renamed notes.md" in rename_transcript
    assert "private-binary-marker" not in rename_transcript

    malformed = by_id["J16-malformed-calls"]
    assert malformed["tasks"][-1]["outcome"] != "pass"
    assert malformed["tasks"][-1]["verified"] is False
    assert {
        (failure["tool_name"], failure["tool_reason"])
        for failure in malformed["tool_failures"]
    } == {
        ("invented_patch_tool", "unknown_tool"),
        ("read_file", "invalid_model_response"),
        ("apply_patch", "bad_arguments"),
    }


def test_acceptance_source_line_changes_when_live_powershell_changes(tmp_path, monkeypatch):
    """The user-visible acceptance identity is bound to PowerShell runtime code."""
    helper = tmp_path / "internal" / "scripts" / "runtime-root.ps1"
    helper.parent.mkdir(parents=True)
    helper.write_text("# original runtime root\n", encoding="utf-8")

    from local_agent import provenance

    monkeypatch.setattr(provenance, "_ROOT", tmp_path)
    first = journeys.source_line(journeys.package_identity())
    helper.write_text("# changed runtime root\n", encoding="utf-8")
    second = journeys.source_line(journeys.package_identity())

    assert first != second
    assert "source sha256" in first
    assert "source sha256" in second


def test_a_journey_whose_claim_does_not_hold_fails_the_run(tmp_path, monkeypatch):
    # The clean-build journey pointed at a compile error must FAIL, not pass or skip.
    monkeypatch.setattr(journeys, "JOURNEYS", [
        ("JX-wrong", "clean-build check on a broken tree", "product", "compile_error", False, {},
         journeys.j_build_pass),
    ])
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out)]) == 1
    wrong = _report(out)["JX-wrong"]
    assert wrong["status"] == "FAIL"
    assert "clean tree build was fail/verification_failed" in wrong["reason"]


def _model_precondition(tmp_path, monkeypatch, args):
    """Run only the preconditions of a model run; return (preconditions, summary text)."""
    monkeypatch.setattr(journeys, "model_call_probe", lambda _config: {"ok": True, "seconds": 0.4})
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / "acc"
    journeys.main(["--output", str(out), "--allow-model", *args, "--only", "J08-fix-build"])
    report = json.loads((out / "journeys.json").read_text(encoding="utf-8"))
    return report["preconditions"], (out / "summary.txt").read_text(encoding="utf-8")


def test_a_managed_model_server_lifecycle_is_retained_as_evidence(tmp_path, monkeypatch):
    """#481: a run says whether it started, replaced or reused the server, from the owner."""
    from serving.managed_runtime import ReplacedReason, RuntimeEnsureResult, RuntimeLifecycle

    outcome = RuntimeEnsureResult(
        RuntimeLifecycle.REPLACED, "managed model endpoint ready",
        replaced_reason=ReplacedReason.CONFIG_CHANGED, changed_fields=("args",),
        launch_to_ready_ms=41250,
    )
    monkeypatch.setattr(journeys.HUB, "ensure_managed_runtime", lambda *_args: outcome)
    pre, summary = _model_precondition(tmp_path, monkeypatch, [])
    assert pre["model_endpoint"] == {
        "ok": True, "message": "managed model endpoint ready", "lifecycle": "replaced",
        "replaced_reason": "config_changed", "refused_reason": None,
        "changed_fields": ["args"], "launch_to_ready_ms": 41250,
    }
    assert "server started by this run, replacing a config_changed server (launch to ready 41.2s)" in summary


def test_a_reused_server_reports_no_launch_time(tmp_path, monkeypatch):
    from serving.managed_runtime import RuntimeEnsureResult, RuntimeLifecycle

    outcome = RuntimeEnsureResult(RuntimeLifecycle.REUSED, "owned model endpoint already ready")
    monkeypatch.setattr(journeys.HUB, "ensure_managed_runtime", lambda *_args: outcome)
    pre, summary = _model_precondition(tmp_path, monkeypatch, [])
    assert pre["model_endpoint"]["lifecycle"] == "reused"
    assert pre["model_endpoint"]["launch_to_ready_ms"] is None
    assert "server reused (already running before this run)" in summary


def test_an_explicit_endpoint_lifecycle_is_unknown_not_inferred(tmp_path, monkeypatch):
    monkeypatch.setattr(journeys, "endpoint_reachable", lambda _config: True)
    monkeypatch.setattr(journeys.HUB, "ensure_managed_runtime",
                        lambda *_args: (_ for _ in ()).throw(AssertionError("explicit endpoint was managed")))
    pre, summary = _model_precondition(
        tmp_path, monkeypatch, ["--base-url", "http://127.0.0.1:9/v1", "--model", "m"])
    assert pre["model_endpoint"]["lifecycle"] == "not_managed"
    assert pre["model_endpoint"]["launch_to_ready_ms"] is None
    assert "explicit --base-url); lifecycle unknown" in summary


@pytest.mark.parametrize(("endpoint", "phrase"), [
    ({"lifecycle": "started", "launch_to_ready_ms": 1500}, "server started by this run (launch to ready 1.5s)"),
    ({"lifecycle": "started", "launch_to_ready_ms": None}, "launch to ready not recorded"),
    ({}, "server lifecycle unknown"),
])
def test_the_lifecycle_phrase_reports_only_what_was_recorded(endpoint, phrase):
    assert phrase in journeys.lifecycle_phrase(endpoint)


def test_a_model_the_product_cannot_call_is_one_precondition_not_every_journey(tmp_path, monkeypatch):
    # /models answers, but the product's own client cannot complete a call.
    monkeypatch.setattr(journeys, "endpoint_reachable", lambda _config: True)

    def broken_client(_config):
        raise ModuleNotFoundError("No module named 'jiter'")

    monkeypatch.setattr(journeys, "build_client", broken_client)
    out = tmp_path / "acc"
    journeys.main(["--output", str(out), "--allow-model", "--base-url", "http://127.0.0.1:9/v1",
                   "--model", "m", "--only", "J08-fix-build"])
    report = json.loads((out / "journeys.json").read_text(encoding="utf-8"))
    pre = report["preconditions"]
    assert pre["model_call"]["ok"] is False
    assert pre["model_endpoint"]["ok"] is False
    assert "No module named 'jiter'" in pre["model_endpoint"]["message"]
    assert pre["model_endpoint"]["lifecycle"] == "not_managed", "a failed call keeps the lifecycle"
    assert "Model     NOT USABLE: " in (out / "summary.txt").read_text(encoding="utf-8")
    fix = _report(out)["J08-fix-build"]
    assert fix["status"] == "UNKNOWN"
    assert "No module named 'jiter'" in fix["reason"]


def test_repeat_runs_each_model_journey_n_times_and_reports_its_rate(tmp_path, monkeypatch, capsys):
    """A single model attempt is noise (J09 passed on 09d4d71 and not on e2b04cc)."""
    outcomes = iter(["changed", "fail", "changed"])
    ran: list[str] = []

    def model_journey(session):
        ran.append(session.journey.id)
        session.journey.measured_as(next(outcomes), "scripted")
        if session.journey.id == "M1.r2":
            session.journey.tool_failures.append({"tool_name": "read_file",
                                                  "tool_reason": "not_found", "detail": "missing"})

    def product_journey(session):
        ran.append(session.journey.id)
        session.journey.passed("scripted")

    class _Session:
        def __init__(self, runner, journey, repo):
            self.journey = journey

        def open(self):
            from contextlib import nullcontext
            return nullcontext(self)

    monkeypatch.setattr(journeys, "JOURNEYS", [
        ("P1", "a product journey", "product", "clean", False, {}, product_journey),
        ("M1", "a model journey", "model", "clean", True, {}, model_journey),
    ])
    monkeypatch.setattr(journeys, "Session", _Session)
    monkeypatch.setattr(journeys, "make_repo", lambda dest, scenario, **options: dest)
    runner = journeys.Runner(journeys.argparse.Namespace(
        output=tmp_path / "acc", profile="ptl-npu-8b", base_url=None, model=None,
        allow_model=True, journey_timeout=10.0, stop_budget=10.0, repeat=3))
    runner.preconditions = {"fixture_builds": True, "model_endpoint": {"ok": True}}

    results = runner.run(None)

    printed = capsys.readouterr().out
    assert "M1.r2: read_file not_found missing" in printed
    assert "M1.r1: read_file" not in printed
    assert ran == ["P1", "M1.r1", "M1.r2", "M1.r3"]
    assert [j.id for j in results] == ["P1", "M1.r1", "M1.r2", "M1.r3"]
    assert journeys.model_rates(results) == [f"{'M1':<22} changed 2/3, fail 1/3"]
    assert "Model rates (repeated model journeys)" in journeys.summary_text(runner, results, "0" * 64)


def test_repeat_must_be_positive(tmp_path, capsys):
    with pytest.raises(SystemExit) as refusal:
        journeys.main(["--output", str(tmp_path / "acc"), "--repeat", "0"])
    assert refusal.value.code == 2
    assert "--repeat must be at least 1" in capsys.readouterr().err
    assert not (tmp_path / "acc").exists()


def test_fixture_probe_seam_preserves_real_runtime_preconditions(tmp_path, monkeypatch):
    class RuntimeFactsStub:
        @staticmethod
        def header() -> str:
            return "observed runtime"

    runtime_facts = RuntimeFactsStub()
    monkeypatch.setattr(journeys.shutil, "which", lambda _name: "available")
    monkeypatch.setattr(journeys, "probe_fixture_build", lambda _output: (True, ""))
    monkeypatch.setattr(
        journeys.RuntimeFacts,
        "observe",
        lambda *_args, **_kwargs: runtime_facts,
    )
    runner = journeys.Runner(journeys.argparse.Namespace(
        output=tmp_path / "acc", profile="ptl-npu-8b", base_url=None, model=None,
        allow_model=False, journey_timeout=10.0, stop_budget=10.0, repeat=1,
    ))

    runner.check_preconditions()

    assert runner.runtime_facts is runtime_facts
    assert runner.preconditions["fixture_builds"] is True
    assert runner.preconditions["runtime_header"] == "observed runtime"


def test_event_dump_inlines_a_failed_tool_calls_retained_reason():
    """A journey log must say why a tool call failed, not only that it failed."""
    retained = {"schema": "lca.tool-failure/2", "tool_name": "read_file",
                "tool_reason": "not_found", "detail": "'src/x.cpp' is not a file"}
    ref = {"artifact_id": "a", "availability": "retained"}

    class _Store:
        def artifact_bytes(self, seen):
            assert seen is ref
            return json.dumps(retained).encode("utf-8")

    session = object.__new__(journeys.Session)
    session.service = type("S", (), {"store": _Store()})()
    failed = {"kind": "tool.finished", "payload": {"result_ref": ref}}
    clean = {"kind": "tool.finished", "payload": {"result_ref": None}}
    other = {"kind": "task.admitted", "payload": {}}

    assert session._with_tool_failure(failed)["tool_failure"] == retained
    assert session._with_tool_failure(clean) is clean
    assert session._with_tool_failure(other) is other


def test_failed_tool_summary_retains_evidence_and_prints_one_line(tmp_path):
    failure = {"schema": "lca.tool-failure/2", "tool_name": "read_file",
               "tool_reason": "not_found", "detail": "missing\nheader " + "x" * 400}
    session = object.__new__(journeys.Session)
    session.journey = journeys.Journey("J10", "test fix", "model")
    session.log_dir = tmp_path
    session.events = lambda: [{"kind": "tool.finished"}]
    session._with_tool_failure = lambda event: {**event, "tool_failure": failure}
    session._dump_events()
    assert session.journey.tool_failures == [failure]
    assert json.loads(json.dumps(session.journey.__dict__))["tool_failures"] == [failure]
    line, = journeys.failed_tool_lines(session.journey)
    assert line.startswith("J10: read_file not_found missing header ")
    assert "\n" not in line
    assert len(line.split("not_found ", 1)[1]) == 300
    runner = type("Runner", (), {"preconditions": {}})()
    text = journeys.summary_text(runner, [session.journey], "0" * 64)
    assert "Failed tool calls\n" + line in text
    assert journeys.SCHEMA == "lca.acceptance-journeys/1"
    clean = journeys.Journey("clean", "clean", "model")
    assert journeys.failed_tool_lines(clean) == []
    assert "Failed tool calls" not in journeys.summary_text(runner, [clean], "0" * 64)


@pytest.mark.parametrize("error", [None, RuntimeError, KeyboardInterrupt])
def test_keep_awake_releases_on_normal_error_and_interrupt(monkeypatch, error):
    calls = []
    monkeypatch.setattr(journeys.sys, "platform", "win32")
    monkeypatch.setattr(journeys, "_windows_execution_state", lambda: lambda flags: calls.append(flags) or 1)

    def run():
        with journeys.keep_system_awake():
            assert calls == [0x80000001]
            if error:
                raise error("interrupted")
    if error:
        with pytest.raises(error):
            run()
    else:
        run()
    assert calls == [0x80000001, 0x80000000]


def test_keep_awake_is_noop_elsewhere(monkeypatch):
    monkeypatch.setattr(journeys.sys, "platform", "linux")
    monkeypatch.setattr(journeys, "_windows_execution_state", lambda: pytest.fail("Windows API loaded"))
    with journeys.keep_system_awake():
        pass


def test_keep_awake_refusal_does_not_start_run(monkeypatch):
    monkeypatch.setattr(journeys.sys, "platform", "win32")
    monkeypatch.setattr(journeys, "_windows_execution_state", lambda: lambda flags: 0)
    with pytest.raises(OSError, match="refused"):
        with journeys.keep_system_awake():
            pytest.fail("run started")


def test_main_keeps_preconditions_inside_awake_scope(tmp_path, monkeypatch):
    from contextlib import contextmanager
    activity = []

    @contextmanager
    def awake():
        activity.append("acquire")
        try:
            yield
        finally:
            activity.append("release")

    def preconditions(self):
        assert activity == ["acquire"]
        raise KeyboardInterrupt

    monkeypatch.setattr(journeys, "keep_system_awake", awake)
    monkeypatch.setattr(journeys.Runner, "check_preconditions", preconditions)
    with pytest.raises(KeyboardInterrupt):
        journeys.main(["--output", str(tmp_path / "acc")])
    assert activity == ["acquire", "release"]


def test_task_facts_carry_the_workers_context_metrics():
    from local_agent.session.contracts import TaskOutcome, TaskResult

    measured = TaskResult("t1", TaskOutcome.FAIL, "no", False, metrics={
        "llm_calls": 14, "prompt_tokens": 90_000, "context_peak_tokens": 7_400, "compactions": 2,
        "proof_binding": None})
    facts = journeys._task_facts(measured)
    assert (facts["llm_calls"], facts["context_peak_tokens"], facts["compactions"]) == (14, 7_400, 2)
    unmeasured = journeys._task_facts(TaskResult("t2", TaskOutcome.FAIL, "no", False,
                                                 metrics={"llm_calls": True}))
    assert unmeasured["llm_calls"] is None and unmeasured["context_peak_tokens"] is None


def test_context_use_names_peak_against_budget_for_model_journeys_only():
    model = journeys.Journey("J08-fix-build.r1", "fix it", "model")
    model.tasks = [{"llm_calls": 9, "context_peak_tokens": 6_000, "compactions": 1},
                   {"llm_calls": 3, "context_peak_tokens": 7_200, "compactions": 0}]
    product = journeys.Journey("J01-build-pass", "build", "product")
    product.tasks = [{"llm_calls": 0, "context_peak_tokens": 100, "compactions": 0}]
    unmeasured = journeys.Journey("J10-fix-tests.r1", "fix it", "model")
    unmeasured.tasks = [{"llm_calls": None, "context_peak_tokens": None}]

    lines = journeys.context_use_lines([model, product, unmeasured], 8_000)
    assert lines == ["J08-fix-build.r1       12 model calls · peak context 7,200/8,000 tokens (90%)"
                     " · 1 compactions"]


def test_compare_cli_never_starts_a_runner(tmp_path, monkeypatch, capsys):
    old, new = tmp_path / "old", tmp_path / "new"
    for directory in (old, new):
        directory.mkdir()
        (directory / "journeys.json").write_text(json.dumps({
            "schema": journeys.SCHEMA, "model": "fixture",
            "journeys": [{"id": "J01", "kind": "product", "status": "PASS"}]}))
    monkeypatch.setattr(journeys, "Runner", lambda *_: pytest.fail("started a runner"))
    monkeypatch.setattr(journeys, "resolve_preset", lambda *_: pytest.fail("resolved a model"))
    assert journeys.main(["--compare", str(old), str(new)]) == 0
    assert "J01 | PASS 1/1 | PASS 1/1" in capsys.readouterr().out


def test_compare_cli_refuses_output_and_preserves_evidence(tmp_path):
    out = tmp_path / "unused"
    with pytest.raises(SystemExit) as refused:
        journeys.main(["--compare", "old", "new", "--output", str(out)])
    assert refused.value.code == 2
    assert not out.exists()


# R11: the seeded bug behind J14 has to be invisible to today's suite and caught by a
# real regression test, or the journey would measure nothing.
REGRESSION_ASSERT = '    assert(sandbox::split("a,", \',\').size() == 2);\n    std::puts("text_util ok");\n'


def _with_regression_test(repo: Path) -> Path:
    test = repo / "tests" / "test_text_util.cpp"
    text = test.read_text(encoding="utf-8")
    assert '    std::puts("text_util ok");\n' in text
    test.write_text(text.replace('    std::puts("text_util ok");\n', REGRESSION_ASSERT, 1), encoding="utf-8")
    return repo


def test_the_seeded_split_bug_passes_the_existing_suite(tmp_path):
    passed, log = journeys.independent_suite(journeys.make_repo(tmp_path / "seeded", "untested_bug"))
    assert passed, log


def test_a_regression_test_for_the_seed_fails_only_text_util_on_the_bug(tmp_path):
    correct, correct_log = journeys.independent_suite(
        _with_regression_test(journeys.make_repo(tmp_path / "clean", "clean")))
    assert correct, correct_log
    on_bug, bug_log = journeys.independent_suite(
        _with_regression_test(journeys.make_repo(tmp_path / "seeded", "untested_bug")))
    assert not on_bug
    assert journeys.suite_failed_only(bug_log, "text_util"), bug_log


def test_suite_failed_only_reads_the_ctest_failure_list():
    log = ("50% tests passed, 2 tests failed out of 4\n\nThe following tests FAILED:\n"
           "\t  2 - text_util (Failed)\n\t  3 - fd_owner (Failed)\nErrors while running CTest\n")
    assert not journeys.suite_failed_only(log, "text_util")
    assert journeys.suite_failed_only(log.replace("\t  3 - fd_owner (Failed)\n", ""), "text_util")
    assert not journeys.suite_failed_only("100% tests passed", "text_util")


def test_j14_is_a_model_journey_on_the_clean_fixture():
    row = next(r for r in journeys.JOURNEYS if r[0] == "J14-regression-test")
    assert row[2:5] == ("model", "clean", True)
    assert "untested_bug" in json.loads(
        (journeys.FIXTURE / "scenarios" / "manifest.json").read_text(encoding="utf-8"))["scenarios"]


@pytest.mark.parametrize("damage", ["index", "unstaged", "untracked"])
def test_dirty_worktree_journey_detects_user_work_damage(tmp_path, monkeypatch, damage):
    original_turn = journeys.Session.turn

    def damaged_turn(session, text, **kwargs):
        answer, result = original_turn(session, text, **kwargs)
        if text.startswith("/apply "):
            if damage == "index":
                journeys._git(session.repo, "add", "--", "README.md")
            elif damage == "unstaged":
                (session.repo / "README.md").write_bytes(b"lost user edit\n")
            else:
                (session.repo / "private-notes.bin").write_bytes(b"lost private bytes\n")
        return answer, result

    monkeypatch.setattr(journeys.Session, "turn", damaged_turn)
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out), "--only", "J15-dirty-worktree"]) == 1
    result = _report(out)["J15-dirty-worktree"]
    assert result["status"] == "FAIL"
    assert "candidate import changed user work, index or history" in result["reason"]


def test_dirty_inspection_does_not_count_unstaged_as_staged(tmp_path, monkeypatch):
    original_turn = journeys.Session.turn

    def omit_staged(session, text, **kwargs):
        answer, result = original_turn(session, text, **kwargs)
        if text == "what have I changed?":
            answer = answer.replace("staged: README.md; unstaged:", "unstaged:")
        return answer, result

    monkeypatch.setattr(journeys.Session, "turn", omit_staged)
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out), "--only", "J15-dirty-worktree"]) == 1
    assert "inspection omitted staged: README.md" in _report(out)["J15-dirty-worktree"]["reason"]


def test_rename_binary_journey_rejects_index_damage(tmp_path, monkeypatch):
    turn = journeys.Session.turn

    def damage(session, text, **kwargs):
        answer, result = turn(session, text, **kwargs)
        journeys._git(session.repo, 'add', '--', 'sample.bin')
        return answer, result

    monkeypatch.setattr(journeys.Session, 'turn', damage)
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / 'damaged'
    assert journeys.main(['--output', str(out), '--only', 'J15b-rename-binary']) == 1
    result = _report(out)['J15b-rename-binary']
    assert result['status'] == 'FAIL'
    assert 'changed index, history or file bytes' in result['reason']


def test_branch_review_fails_when_a_detached_head_is_reported_as_a_branch(tmp_path, monkeypatch):
    original_turn = journeys.Session.turn

    def call_detached_a_branch(session, text, **kwargs):
        answer, result = original_turn(session, text, **kwargs)
        if text == "what changed on my branch?":
            answer = re.sub(r"detached at [0-9a-f]+", "branch: feature", answer)
        return answer, result

    monkeypatch.setattr(journeys.Session, "turn", call_detached_a_branch)
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out), "--only", "J17-branch-review"]) == 1
    assert "detached: branch review omitted 'detached at" in _report(out)["J17-branch-review"]["reason"]


def test_branch_review_fails_when_the_review_moves_head(tmp_path, monkeypatch):
    original_turn = journeys.Session.turn

    def move_head(session, text, **kwargs):
        answer, result = original_turn(session, text, **kwargs)
        if text == "what changed on my branch?":
            journeys._git(session.repo, "switch", "-q", "main")
        return answer, result

    monkeypatch.setattr(journeys.Session, "turn", move_head)
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out), "--only", "J17-branch-review"]) == 1
    assert "changed HEAD, the index, the worktree or a branch" in _report(out)["J17-branch-review"]["reason"]


def test_conflict_explanation_fails_when_it_invents_the_wrong_way_out(tmp_path, monkeypatch):
    original_turn = journeys.Session.turn

    def wrong_command(session, text, **kwargs):
        answer, result = original_turn(session, text, **kwargs)
        if text == "explain this conflict":
            answer = answer.replace("git rebase --continue", "git merge --continue")
        return answer, result

    monkeypatch.setattr(journeys.Session, "turn", wrong_command)
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out), "--only", "J20-conflict-explain"]) == 1
    assert ("rebase: conflict explanation omitted 'continue: git rebase --continue'"
            in _report(out)["J20-conflict-explain"]["reason"])


def test_conflict_explanation_fails_when_it_aborts_the_merge(tmp_path, monkeypatch):
    original_turn = journeys.Session.turn

    def abort(session, text, **kwargs):
        answer, result = original_turn(session, text, **kwargs)
        if text == "explain this conflict":
            journeys._git(session.repo, "merge", "--abort", check=False)
        return answer, result

    monkeypatch.setattr(journeys.Session, "turn", abort)
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out), "--only", "J20-conflict-explain"]) == 1
    assert "merge: explaining the conflict resolved" in _report(out)["J20-conflict-explain"]["reason"]


def test_exact_commit_journey_fails_when_the_unrelated_staged_entry_is_lost(tmp_path, monkeypatch):
    original_turn = journeys.Session.turn

    def unstage(session, text, **kwargs):
        answer, result = original_turn(session, text, **kwargs)
        if text.startswith("/commit "):
            journeys._git(session.repo, "reset", "-q", "--", "NOTES.md")
        return answer, result

    monkeypatch.setattr(journeys.Session, "turn", unstage)
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out), "--only", "J21-exact-commit"]) == 1
    assert "changed the unrelated staged entry" in _report(out)["J21-exact-commit"]["reason"]


def test_repo_explanation_fails_when_the_answer_names_a_file_that_does_not_exist(tmp_path, monkeypatch):
    original_turn = journeys.Session.turn

    def invent(session, text, **kwargs):
        answer, result = original_turn(session, text, **kwargs)
        if text == "explain this repository":
            answer += " The core logic lives in src/imaginary_engine.cpp."
        return answer, result

    monkeypatch.setattr(journeys.Session, "turn", invent)
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out), "--only", "J22-repo-explain"]) == 1
    assert "src/imaginary_engine.cpp" in _report(out)["J22-repo-explain"]["reason"]


def test_repo_explanation_fails_when_a_configured_command_is_omitted(tmp_path, monkeypatch):
    original_turn = journeys.Session.turn

    def drop_test(session, text, **kwargs):
        answer, result = original_turn(session, text, **kwargs)
        return answer.split(" Test:")[0], result

    monkeypatch.setattr(journeys.Session, "turn", drop_test)
    _skip_redundant_fixture_probe(monkeypatch)
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out), "--only", "J22-repo-explain"]) == 1
    assert "omitted the configured command" in _report(out)["J22-repo-explain"]["reason"]


def test_public_symbol_lookup_returns_an_observed_file_and_line(tmp_path, monkeypatch):
    _skip_redundant_fixture_probe(monkeypatch)
    real_which = journeys.shutil.which
    monkeypatch.setattr(
        journeys.shutil,
        "which",
        lambda name: "/observed/cmake" if name == "cmake" else real_which(name),
    )
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out), "--only", "J23-symbol-lookup"]) == 0
    result = _report(out)["J23-symbol-lookup"]
    assert result["status"] == "PASS"
    assert "file/line citation" in result["reason"]


def _grounding_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "real.cpp").write_text("x", encoding="utf-8")
    (repo / "CMakeLists.txt").write_text("x", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("x", encoding="utf-8")
    (tmp_path / "outside.py").write_text("secret", encoding="utf-8")
    return repo


def _link(link, target):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available to this user on this host")


def test_real_files_bare_names_and_line_references_are_grounded(tmp_path):
    repo = _grounding_repo(tmp_path)
    answer = ("The entry point is src/real.cpp (see ./src/real.cpp:12 and real.cpp); "
              "it builds from CMakeLists.txt, documented in docs\\guide.md, as of "
              "https://example.com/a.md, e.g. like Node.js.")
    grade = journeys.observe_answer(answer, repo)
    assert grade.missing == () and grade.outside == ()
    assert grade.status == "evidence-grounded"
    assert "Node.js" not in journeys.path_references(answer)


@pytest.mark.parametrize(("answer_ref", "verdict"), [
    ("src/nonexistent.xyz", "missing"),     # any lettered suffix with a directory is checked
    ("src/missing.cpp", "missing"),
    ("gone.hpp", "missing"),
    ("../outside.py", "outside"),            # traversal never validates a sibling
    ("/etc/passwd.txt", "outside"),
    ("C:/Windows/win.ini", "outside"),
    ("src/missing.cpp#L12", "missing"),      # an anchor never hides the reference
    ("../outside.py#L3-L9", "outside"),
])
def test_unreal_and_external_references_are_never_grounded(tmp_path, answer_ref, verdict):
    repo = _grounding_repo(tmp_path)
    answer = f"The repository is organised around {answer_ref} and the build configuration."
    grade = journeys.observe_answer(answer, repo)
    assert grade.status == "ungrounded"
    reference = answer_ref.split("#")[0]     # reported without its line anchor
    assert reference in (grade.missing if verdict == "missing" else grade.outside)


def test_windows_separators_keep_their_directory_scope(tmp_path):
    repo = _grounding_repo(tmp_path)
    (repo / "nonexistent.cpp").write_text("x", encoding="utf-8")   # same bare name elsewhere
    grade = journeys.observe_answer(
        "The work happens in src\\nonexistent.cpp and the rest is glue code.", repo)
    assert grade.missing == ("src/nonexistent.cpp",)


def test_an_in_repo_symlink_to_an_outside_file_is_outside(tmp_path):
    repo = _grounding_repo(tmp_path)
    _link(repo / "src" / "external.py", tmp_path / "outside.py")
    for ref in ("src/external.py", "external.py"):
        grade = journeys.observe_answer(f"Most of the logic lives in {ref} for this project.", repo)
        assert grade.outside == (ref,), ref


def test_an_in_repo_symlink_to_an_in_repo_file_is_grounded(tmp_path):
    repo = _grounding_repo(tmp_path)
    _link(repo / "src" / "alias.cpp", repo / "src" / "real.cpp")
    grade = journeys.observe_answer("The alias src/alias.cpp points at the real source file.", repo)
    assert grade.status == "evidence-grounded"


# Astra's executed probes against 2799e6c: each reference vanished from observation.
@pytest.mark.parametrize(("answer", "reference"), [
    ("The configuration is file:///etc/passwd.txt.", "/etc/passwd.txt"),
    ("The configuration is file://server/share/secret.py.", "//server/share/secret.py"),
    ("The configuration is file:///C:/Windows/win.ini.", "C:/Windows/win.ini"),
    ("The implementation is //server/share/secret.py.", "//server/share/secret.py"),
    ("The implementation is \\\\server\\share\\secret.py.", "//server/share/secret.py"),
])
def test_file_uris_and_unc_paths_are_outside(tmp_path, answer, reference):
    seen = journeys.observe_answer(answer, _grounding_repo(tmp_path))
    assert seen.outside == (reference,)
    assert seen.status == "ungrounded"
    assert not seen.recognised_refusal


# Astra's executed probes against 2fe16ed: a file: URI is outside without the suffix
# grammar, decoded once; a malformed one is still outside, reported raw.
@pytest.mark.parametrize(("answer", "reference"), [
    ("The secret is file:///etc/passwd.", "/etc/passwd"),
    ("The config is file:///tmp/a%20b/config.py.", "/tmp/a b/config.py"),
    ("The config is file:///tmp/a%2Epy.", "/tmp/a.py"),
    ("The readme is file://server/share/README.", "//server/share/README"),
    ("It escapes via file:///repo/%2E%2E/%2E%2E/etc/shadow.", "/repo/../../etc/shadow"),
    ("It is at file:///etc/passwd#L3 for sure.", "/etc/passwd"),
    ("It is at file:///tmp/%zz.py today.", "file:///tmp/%zz.py"),           # bad escape
    ("It is at file:///tmp/a%00.py today.", "file:///tmp/a%00.py"),         # NUL
    ("It is at file:///tmp/%ff.py today.", "file:///tmp/%ff.py"),           # not UTF-8
    ("It is at file:relative.py today.", "file:relative.py"),               # no path
])
def test_every_file_uri_is_outside_decoded_once(tmp_path, answer, reference):
    seen = journeys.observe_answer(answer, _grounding_repo(tmp_path))
    assert seen.outside == (reference,)
    assert seen.status == "ungrounded"


@pytest.mark.parametrize("answer", [
    "The implementation is src/real.cpp#L12.",
    "The implementation is src/real.cpp#L12-L20.",
    "The implementation is src/real.cpp:12.",
    "Implemented in src/real.cpp.",
])
def test_anchored_and_short_answers_with_real_files_are_grounded(tmp_path, answer):
    seen = journeys.observe_answer(answer, _grounding_repo(tmp_path))
    assert seen.grounded == ("src/real.cpp",)
    assert seen.status == "evidence-grounded"
    assert not seen.recognised_refusal


def test_network_urls_are_not_file_references():
    assert journeys.path_references("See https://example.com/src/a.py and ssh://host/b.py.") == []


@pytest.mark.parametrize("answer", [
    "I cannot answer this question.",
    "I don't know what this repository does, sorry about that, really.",
])
def test_a_recognised_refusal_with_no_evidence_is_reported_as_one(tmp_path, answer):
    seen = journeys.observe_answer(answer, _grounding_repo(tmp_path))
    assert seen.recognised_refusal
    assert seen.status == "recognised-refusal"


@pytest.mark.parametrize("answer", [
    "The available files do not reveal what this repository does.",
    "",              # length is not a refusal form: short answers are just uncited
    "Not sure.",
])
def test_an_answer_citing_nothing_is_reported_as_no_evidence(tmp_path, answer):
    seen = journeys.observe_answer(answer, _grounding_repo(tmp_path))
    assert not seen.recognised_refusal
    assert seen.status == "no-evidence"


@pytest.mark.parametrize("answer", [
    # Astra's review of 4dc3b32. Scoped unknowns with real evidence...
    "I cannot determine which compiler this repository requires; src/real.cpp contains the parser.",
    "I cannot determine where the ring buffer is tested; src/real.cpp implements it.",
    "The compiler version cannot be determined\nThe parser lives in src/real.cpp.",
    # ...and contradictions that merely contain a real token. All are observed the
    # same way: evidence-grounded, with correctness unknown. None is a verdict.
    "The ring buffer is not implemented in src/real.cpp; that file is unrelated.",
    "src/real.cpp is not part of this repository; ignore that filename completely.",
])
def test_a_real_token_is_observed_not_certified(tmp_path, answer):
    seen = journeys.observe_answer(answer, _grounding_repo(tmp_path))
    assert seen.status == "evidence-grounded"
    assert not hasattr(seen, "answered") and "answered" not in journeys._STATUS_ORDER


@pytest.mark.parametrize("answer", [
    "I cannot determine whether the build supports Ninja; run cmake --build build.",
    "Do not run cmake --build build because that command is obsolete and unsafe.",
])
def test_a_command_mention_is_observed_not_certified(tmp_path, answer):
    seen = journeys.observe_answer(answer, _grounding_repo(tmp_path),
                                   ("cmake --build build",))
    assert seen.omitted_commands == ()
    assert seen.status == "evidence-grounded"


def test_omitted_configured_commands_are_command_incomplete(tmp_path):
    seen = journeys.observe_answer("Configure with cmake -S . -B build and then build it.",
                                   _grounding_repo(tmp_path),
                                   ("cmake -S . -B build", "ctest --test-dir build"))
    assert seen.status == "command-incomplete"
    assert seen.omitted_commands == ("ctest --test-dir build",)


def test_the_worst_observation_decides_and_every_problem_is_explained(tmp_path):
    repo = _grounding_repo(tmp_path)
    seen = [
        journeys.observe_answer("A full answer that names src/real.cpp clearly.", repo),
        journeys.observe_answer("I cannot answer this question.", repo),
        journeys.observe_answer("This answer invents src/ghost.cpp as the main module.", repo),
    ]
    assert journeys.worst(seen).status == "ungrounded"
    assert "src/ghost.cpp" in seen[2].explain()


class _QuestionSession:
    """Just enough of a Session to run a question journey against scripted answers."""

    def __init__(self, repo, answers):
        self.repo = repo
        self._answers = answers
        self.journey = journeys.Journey("R01-questions", "questions", "model")

    def turn(self, question):
        return self._answers[question], None


def _git_repo(repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)


def _r01(tmp_path, monkeypatch, purpose, build="Build it with cmake --build build today."):
    repo = _grounding_repo(tmp_path)
    _git_repo(repo)
    monkeypatch.setattr(journeys, "configured_commands", lambda _repo: ["cmake --build build"])
    session = _QuestionSession(repo, {
        "what does this repository do?": purpose,
        journeys._BUILD_QUESTION: build,
        "what changed on my branch?": "Nothing has changed on this branch since it was created.",
    })
    journeys.r_questions(session)
    return session.journey


def test_r01_reports_observations_and_leaves_correctness_unknown(tmp_path, monkeypatch):
    journey = _r01(tmp_path, monkeypatch,
                   "The ring buffer is not implemented in src/real.cpp; that file is unrelated.")
    assert journey.status == "MEASURED:evidence-grounded"
    assert journey.measured["semantic_correctness"] == "unknown"
    assert "read the transcript" in journey.reason


def test_r01_requires_the_configured_commands_like_j22(tmp_path, monkeypatch):
    journey = _r01(tmp_path, monkeypatch, "It is a small C++ library whose core is src/real.cpp.",
                   build="It is built with CMake from CMakeLists.txt in the usual way.")
    assert journey.status == "MEASURED:command-incomplete"
    assert journey.measured["omitted_commands"] == ["cmake --build build"]


def test_j12_reports_the_worst_observation(tmp_path):
    session = _QuestionSession(_grounding_repo(tmp_path), {
        "what does this repository do?": "It is a small C++ library whose core is src/real.cpp.",
        "where is the ring buffer implemented?": "I cannot answer this question.",
    })
    journeys.j_questions(session)
    assert session.journey.status == "MEASURED:recognised-refusal"


def test_model_answers_are_never_counted_as_completed_verified():
    """Q06 prose is an observation, not a verification (AGENTS: model prose is not
    authority). Only proved outcomes reach completed_verified."""
    measured = journeys.Journey("Q06-repo-explain", "explain", "model")
    measured.measured_as("evidence-grounded", "contradictory but cites a real file")
    proved = journeys.Journey("Q01-build-test", "build", "model")
    proved.passed("built and tested")
    metrics = journeys.corpus_metrics([measured, proved])
    assert metrics["completed_verified"] == 1


def test_grading_is_linear_on_pathological_whitespace(tmp_path):
    """Stacked optional whitespace groups once made a refusal check cubic."""
    import time

    padded = "I cannot tell" + " " * 20000 + "x"
    started = time.monotonic()
    journeys.observe_answer(padded, _grounding_repo(tmp_path))
    assert time.monotonic() - started < 2.0
