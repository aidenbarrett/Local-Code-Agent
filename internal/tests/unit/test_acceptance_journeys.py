"""The acceptance journey runner: the deterministic journeys, and that it fails closed.

These run the real Session Hub composition against real CMake builds on both CI
platforms, so the runner Aiden starts on the target machine has already been
exercised end to end on Linux and Windows. Model journeys need an endpoint and
must report UNKNOWN, with the reason, when there is none.
"""
from __future__ import annotations

import importlib.util
import json
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
                 "J06-authority", "J13-candidate-scripted"]


def _report(output: Path) -> dict[str, dict[str, object]]:
    report = json.loads((output / "journeys.json").read_text(encoding="utf-8"))
    assert report["schema"] == "lca.acceptance-journeys/1"
    return {j["id"]: j for j in report["journeys"]}


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
    assert "Product   PASS 7 / FAIL 0" in summary
    assert "Model     not used" in summary


def test_a_journey_whose_claim_does_not_hold_fails_the_run(tmp_path, monkeypatch):
    # The clean-build journey pointed at a compile error must FAIL, not pass or skip.
    monkeypatch.setattr(journeys, "JOURNEYS", [
        ("JX-wrong", "clean-build check on a broken tree", "product", "compile_error", False, {},
         journeys.j_build_pass),
    ])
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out)]) == 1
    wrong = _report(out)["JX-wrong"]
    assert wrong["status"] == "FAIL"
    assert "clean tree build was fail/verification_failed" in wrong["reason"]


def test_the_report_names_the_product_source_that_produced_it(tmp_path):
    out = tmp_path / "acc"
    journeys.main(["--output", str(out), "--only", "J04-ambiguous"])
    report = json.loads((out / "journeys.json").read_text(encoding="utf-8"))
    product = report["preconditions"]["product"]
    assert len(product["source_sha256"]) == 64
    assert f"source sha256 {product['source_sha256'][:16]}" in (out / "summary.txt").read_text(encoding="utf-8")


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
