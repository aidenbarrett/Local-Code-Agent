"""Repository configuration is validated before it grants authority (#399)."""

import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest

from local_agent.config import ConfigError, load_repo_config
from local_agent.session.candidate_change import commit_candidate
from local_agent.session.contracts import TaskOutcome


BASE = '[profiles.debug]\nbuild = ["true"]\n'


def _write(root: Path, body: str) -> None:
    root.mkdir()
    (root / ".local-agent.toml").write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allow_build", '"false"'),
        ("allow_test", '"true"'),
        ("allow_patch", "0"),
        ("allow_commit", "1"),
    ],
)
def test_policy_authority_requires_toml_boolean(tmp_path: Path, field: str, value: str) -> None:
    root = tmp_path / "repo"
    _write(root, BASE + f"[policy]\n{field} = {value}\n")

    with pytest.raises(ConfigError, match=rf"policy\.{field} must be a boolean"):
        load_repo_config(root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_timeout_seconds", "false"),
        ("max_tool_calls", "0"),
        ("max_repeat_calls", "-1"),
        ("max_tool_result_bytes", "2147483648"),
        ("max_read_bytes", '"400000"'),
    ],
)
def test_policy_budgets_require_bounded_positive_integer(
    tmp_path: Path, field: str, value: str,
) -> None:
    root = tmp_path / "repo"
    _write(root, BASE + f"[policy]\n{field} = {value}\n")

    with pytest.raises(ConfigError, match=rf"policy\.{field} must be a positive"):
        load_repo_config(root)


@pytest.mark.parametrize(
    "body, field",
    [
        ('repo = "not a table"\n' + BASE, "repo"),
        ('profiles = "not a table"\n', "profiles"),
        ('[profiles.debug]\nbuild = ["true"]\nenv = "not a table"\n', "profiles.debug.env"),
        ('[profiles.debug]\nbuild = ["true"]\n[profiles.debug.env]\nCOUNT = 3\n',
         "profiles.debug.env values"),
        ('[repo]\ndefault_profile = "missing"\n' + BASE, "repo.default_profile"),
    ],
)
def test_config_rejects_invalid_tables_environment_and_selection(
    tmp_path: Path, body: str, field: str,
) -> None:
    root = tmp_path / "repo"
    _write(root, body)

    with pytest.raises(ConfigError, match=field):
        load_repo_config(root)


def test_valid_false_reaches_controller_as_policy_denied(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, BASE + "[policy]\nallow_commit = false\n")
    config = load_repo_config(root)

    result = commit_candidate(
        object(),  # policy refusal happens before the workspace authority is called
        config,
        task_id="controller-task",
        request_text="/commit 00000000-0000-0000-0000-000000000001",
    )

    assert result.outcome is TaskOutcome.BLOCKED
    assert result.reason_code == "policy_denied"
    assert not result.verified_at_completion


def test_valid_typed_configuration_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(
        root,
        '[repo]\ndefault_profile = "debug"\n'
        '[profiles.debug]\nbuild = ["true"]\n'
        '[profiles.debug.env]\nMODE = "strict"\n'
        '[policy]\nallow_build = false\nallow_patch = true\nmax_tool_calls = 2\n',
    )

    config = load_repo_config(root)

    assert config.default_profile == "debug"
    assert config.profile().env == {"MODE": "strict"}
    assert config.policy.allow_build is False
    assert config.policy.allow_patch is True
    assert config.policy.max_tool_calls == 2


@pytest.mark.parametrize(
    ("body", "key", "place"),
    [
        (BASE + "[policy]\nallow_bulid = false\n", "allow_bulid", "[policy]"),
        (BASE + '[repo]\nbuild_directory = "out"\n', "build_directory", "[repo]"),
        ('[profiles.debug]\nbuild = ["true"]\ntests = ["ctest"]\n', "tests", "[profiles.debug]"),
        (BASE + "[polcy]\nallow_patch = true\n", "polcy", "the top level"),
    ],
)
def test_an_unknown_key_is_refused_not_ignored(tmp_path: Path, body: str, key: str, place: str) -> None:
    """A misspelt key used to leave its default in force: `allow_bulid = false` kept
    building allowed without a word (#399 relay review)."""
    root = tmp_path / "repo"
    _write(root, body)

    with pytest.raises(ConfigError, match=re.escape(f"unknown key(s) {key} in {place}")):
        load_repo_config(root)


def test_every_key_the_shipped_configs_use_is_known(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[3]
    for config in (repo, repo / "internal" / "benchmark_fixture" / "cpp_project"):
        load_repo_config(config)


@pytest.mark.parametrize("field", ["configure", "build", "test"])
@pytest.mark.parametrize("executable", ["", "   ", "\t"])
def test_command_executable_must_be_nonempty(
    tmp_path: Path, field: str, executable: str,
) -> None:
    root = tmp_path / "repo"
    _write(root, f"[profiles.debug]\n{field} = [{json.dumps(executable)}]\n")

    with pytest.raises(
        ConfigError, match=rf"profiles\.debug\.{field} executable must be a non-empty string",
    ):
        load_repo_config(root)


def test_optional_commands_and_empty_later_arguments_remain_valid(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, '[profiles.debug]\nbuild = ["tool", ""]\ntest = []\n')

    profile = load_repo_config(root).profile()

    assert profile.configure == []
    assert profile.build == ["tool", ""]
    assert profile.test == []


def test_public_doctor_refuses_empty_executable_before_composition(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, '[profiles.debug]\nbuild = ["   "]\n')
    doctor = _public_doctor()

    repository = doctor.check_repository(root)
    assert repository.state == doctor.BLOCKED
    assert repository.observed is not None
    assert "profiles.debug.build executable must be a non-empty string" in repository.observed


def _public_doctor():
    """The one readiness command: `.\\local-code-agent.ps1 doctor` (scripts/doctor.py)."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "doctor.py"
    spec = importlib.util.spec_from_file_location("doctor_for_config_tests", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["doctor_for_config_tests"] = module
    spec.loader.exec_module(module)
    return module
