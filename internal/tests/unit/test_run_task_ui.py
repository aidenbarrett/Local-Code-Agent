from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[3]
INTERNAL = REPO / "internal"
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))


def _presenter():
    path = INTERNAL / "scripts" / "run-task-ui.py"
    spec = spec_from_file_location("run_task_ui", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_command_uses_resolved_profile():
    presenter = _presenter()
    args = presenter.build_parser().parse_args(
        ["Inspect this repository", "--profile", "ptl-gpu-30b", "--skill", "repo-navigation"]
    )
    command = presenter._agent_command(args)
    assert command[command.index("--profile") + 1] == "ptl-gpu-30b"
    assert command[-3:] == ["Inspect this repository", "--skill", "repo-navigation"]


def test_report_split_keeps_answer_separate_from_operator_telemetry():
    presenter = _presenter()
    answer, summary = presenter._split_report(
        "The repository uses Python compileall.\n\n"
        "--- run summary ---\n"
        "outcome: pass\n"
        "skill: repo-navigation\n"
        "tool calls: 1\n"
    )

    assert answer == "The repository uses Python compileall."
    assert summary == ["outcome: pass", "skill: repo-navigation", "tool calls: 1"]


def test_summary_parser_separates_warnings_and_evidence():
    presenter = _presenter()
    fields, warnings, evidence = presenter._parse_summary(
        [
            "outcome: pass",
            "phase: report",
            "evidence:",
            "  - repo_info: repository profile loaded",
            "warnings:",
            "  - example warning",
        ]
    )

    assert fields["outcome"] == "pass"
    assert fields["phase"] == "report"
    assert evidence == ["repo_info: repository profile loaded"]
    assert warnings == ["example warning"]


def test_internal_cheap_tier_is_translated_for_public_output():
    presenter = _presenter()
    skill, route = presenter._route_display("repo-navigation -> cheap tier")

    assert skill == "repo-navigation"
    assert route == "Use the selected model route"
    assert "cheap" not in route.lower()


def test_model_observer_expands_timings_into_clear_labeled_rows():
    presenter = _presenter()
    stream = StringIO()
    term = presenter.ui(stream=stream, colour=False)

    presenter._show_observer_line(
        term,
        "~~ model 3.4s ttft 3.25s in=2753 out=16 cached=1024",
    )
    output = stream.getvalue()

    assert "Model response" in output
    assert "Model call time" in output
    assert "3.4 s" in output
    assert "First token" in output
    assert "3.25 s" in output
    assert "Token usage" in output
    assert "2,753 input · 16 output" in output
    assert "Prompt cache" in output
    assert "1,024 tokens reused" in output
    assert "TTFT" not in output


def test_wall_time_tuple_is_decomposed_for_humans():
    presenter = _presenter()
    timing = presenter._parse_wall_timing(
        "13.6s (model 12.0s over 2 call(s), tools 0.0s, overhead 1.6s)"
    )

    assert timing == {
        "total": "13.6 s",
        "model": "12.0 s across 2 model calls",
        "tools": "0.0 s",
        "overhead": "1.6 s",
    }


def test_root_run_task_uses_the_product_presenter_not_raw_cli_output():
    wrapper = (REPO / "local-code-agent.ps1").read_text(encoding="utf-8")
    assert "scripts\\run-task-ui.py" in wrapper
    run_task_block = wrapper.split("'run-task' {", 1)[1].split("'verification-demo' {", 1)[0]
    assert "local_agent.cli" not in run_task_block


def test_default_stored_and_explicit_choice_drive_server_agent_and_labels(tmp_path, monkeypatch):
    from serving.model_choice import store_preset
    from serving.managed_runtime import RuntimeEnsureResult
    presenter = _presenter()
    monkeypatch.setattr(presenter, "default_runtime_root", lambda: tmp_path)
    cases = [(None, []), ("ptl-gpu-30b", []), ("ptl-gpu-30b", ["--profile", "ptl-npu-8b"])]
    for stored, flags in cases:
        if stored:
            store_preset(tmp_path, stored)
        expected = flags[-1] if flags else stored or presenter.resolve_preset(None, tmp_path).name
        config = presenter.MODEL_PRESETS[expected]
        stream = StringIO()
        monkeypatch.setattr(presenter, "ui", lambda: presenter_ui(stream))
        calls = []
        def ensure(profile, seen, root):
            calls.append((profile, seen, root))
            return RuntimeEnsureResult(True, "ready")
        monkeypatch.setattr(presenter, "ensure_managed_runtime", ensure)
        def run(term, args):
            command = presenter._agent_command(args)
            assert command[command.index("--profile") + 1] == expected
            return 0, "answer\n--- run summary ---\noutcome: pass\n"
        monkeypatch.setattr(presenter, "_run_agent", run)
        assert presenter.main(["Inspect", *flags]) == 0
        assert calls == [(expected, config, tmp_path)]
        assert config.model in stream.getvalue()
        assert presenter.device_label(config.device) in stream.getvalue()
    assert "Qwen3-8B" not in (INTERNAL / "scripts" / "run-task-ui.py").read_text()


def presenter_ui(stream):
    from terminal_ui import ui
    return ui(stream=stream, colour=False)


def test_missing_weights_refuses_before_agent_and_preserves_pull_command(tmp_path, monkeypatch):
    presenter = _presenter()
    monkeypatch.setattr(presenter, "default_runtime_root", lambda: tmp_path)
    def refuse(*args):
        raise presenter.Refusal("missing weights; run: .\\local-code-agent.ps1 models pull ptl-gpu-30b")
    monkeypatch.setattr(presenter, "ensure_managed_runtime", refuse)
    monkeypatch.setattr(presenter, "_run_agent", lambda *args: (_ for _ in ()).throw(AssertionError("agent ran")))
    stream = StringIO()
    monkeypatch.setattr(presenter, "ui", lambda: presenter_ui(stream))
    assert presenter.main(["Inspect", "--profile", "ptl-gpu-30b"]) == 2
    assert "models pull ptl-gpu-30b" in stream.getvalue()
