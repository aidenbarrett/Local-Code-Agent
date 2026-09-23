from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from local_agent.session.task_admission import execution_contract_sha256
from local_agent.session.task_controller import TaskController


@dataclass(frozen=True)
class _Policy:
    allow_patch: bool = False
    allow_commit: bool = False
    allow_build: bool = False
    allow_test: bool = False
    command_timeout_seconds: int = 30
    max_tool_calls: int = 4


@dataclass(frozen=True)
class _Repo:
    root: Path
    name: str = "fixture"
    build_dir: str = "build"
    run_dir: str = ".local-agent/runs"
    skills_dir: str = "skills"
    default_profile: str = "default"
    profiles: dict = field(default_factory=dict)
    policy: _Policy = field(default_factory=_Policy)


def _write_skill(root: Path, body: str = "Inspect the repository.") -> Path:
    skill = root / "skills" / "inspect"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: inspect\ndescription: Inspect repository\ntools: [read_file]\n---\n"
        + body
        + "\n",
        encoding="utf-8",
    )
    return skill


def _controller(tmp_path: Path) -> TaskController:
    _write_skill(tmp_path)
    return TaskController(_Repo(tmp_path), lambda: None, object())


def test_effective_skill_fingerprint_changes_with_procedure_bytes(tmp_path):
    controller = _controller(tmp_path)
    before = controller.effective_skill_sha256("inspect")

    (tmp_path / "skills" / "inspect" / "SKILL.md").write_text(
        "---\nname: inspect\ndescription: Inspect repository\ntools: [read_file]\n---\n"
        "Inspect the repository and include build metadata.\n",
        encoding="utf-8",
    )
    after = controller.effective_skill_sha256("inspect")

    assert before != after


def test_effective_skill_fingerprint_binds_on_demand_reference_bytes(tmp_path):
    controller = _controller(tmp_path)
    skill = tmp_path / "skills" / "inspect"
    references = skill / "references"
    references.mkdir()
    (references / "procedure.md").write_text("version one\n", encoding="utf-8")
    before = controller.effective_skill_sha256("inspect")

    (references / "procedure.md").write_text("version two\n", encoding="utf-8")
    after = controller.effective_skill_sha256("inspect")

    assert before != after


def test_execution_contract_binds_selected_effective_skill(tmp_path):
    controller = _controller(tmp_path)
    before = execution_contract_sha256(controller, skill_name="inspect")

    _write_skill(tmp_path, "Changed procedure bytes.")
    after = execution_contract_sha256(controller, skill_name="inspect")

    assert before != after


def test_unrelated_skill_does_not_change_pinned_skill_identity(tmp_path):
    controller = _controller(tmp_path)
    before = execution_contract_sha256(controller, skill_name="inspect")

    other = tmp_path / "skills" / "other"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text(
        "---\nname: other\ndescription: Other procedure\ntools: [read_file]\n---\nOther.\n",
        encoding="utf-8",
    )
    after = execution_contract_sha256(controller, skill_name="inspect")

    assert before == after
