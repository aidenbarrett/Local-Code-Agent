from importlib.util import module_from_spec, spec_from_file_location
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


def test_public_presenter_keeps_the_provisioned_npu_profile_contract():
    presenter = _presenter()
    args = presenter.build_parser().parse_args(
        ["Inspect this repository", "--skill", "repo-navigation"]
    )
    command = presenter._agent_command(args)

    assert "--profile" in command
    assert command[command.index("--profile") + 1] == "ptl-npu-8b"
    assert command[-3:] == ["Inspect this repository", "--skill", "repo-navigation"]


def test_public_presenter_still_delegates_server_ownership_to_chat_entrypoint():
    presenter = _presenter()
    command = presenter._server_command("powershell.exe")

    assert command[0] == "powershell.exe"
    assert str(REPO / "chat.ps1") in command
    assert command[-2:] == ["qwen3-8b-npu", "--ensure-only"]


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


def test_root_run_task_uses_the_product_presenter_not_raw_cli_output():
    wrapper = (REPO / "local-code-agent.ps1").read_text(encoding="utf-8")
    assert "scripts\\run-task-ui.py" in wrapper
    run_task_block = wrapper.split("'run-task' {", 1)[1].split("'verification-demo' {", 1)[0]
    assert "local_agent.cli" not in run_task_block
