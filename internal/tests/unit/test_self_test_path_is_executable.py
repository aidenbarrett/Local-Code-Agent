"""The agent can still test its own checkout.

`.local-agent.toml` points the agent at itself. Its `profiles.python.test` command
names a script by path, and that path is the only thing standing between the agent and
the ability to verify its own work. Three other places name the same script:
`internal/tests/conftest.py` and `.github/workflows/tests.yml`.

The failure this exists to prevent: a later PR moves the runner, updates the imports,
and every unit test stays green, because no unit test runs the configured command.
The agent then reports that it cannot test, or worse reports a build success it never
verified, and nothing in CI says a word.

So this does not assert that a string was updated. It resolves the command through the
agent's own config loader and **executes** it, against a temporary directory holding
one passing and one failing test, and asserts it distinguishes them. A command that
cannot be resolved, cannot start, or cannot tell a failure from a pass fails here.

Deliberately not run against `internal/tests`: a test that runs the whole suite from
inside the suite is a fork bomb with extra steps. The temp directory is the point.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INTERNAL = ROOT / "internal"
sys.path.insert(0, str(INTERNAL))

from local_agent.config import load_repo_config  # noqa: E402

# Long enough for a two-case run on a cold work laptop, short enough that a hang is a
# failure rather than a stuck job. The configured policy timeout is for real builds.
RUN_TIMEOUT_SECONDS = 180

PASSING = """
def test_it_passes():
    assert 1 + 1 == 2
"""

FAILING = """
def test_it_fails():
    assert 1 + 1 == 3
"""


@pytest.fixture(scope="module")
def config():
    return load_repo_config(ROOT)


@pytest.fixture
def probe_dir(tmp_path):
    """A system-temp test directory, deliberately outside the repository.

    The compatibility runner must be able to execute explicitly supplied test paths
    that are not children of the checkout. Keeping this probe outside the repository
    prevents the self-test invariant from depending on a hidden path restriction and
    exercises the same external-path support as the dedicated runner regression.
    """
    return tmp_path


@pytest.fixture(scope="module")
def test_command(config) -> list[str]:
    command = config.profile().test
    assert command, (
        ".local-agent.toml defines no test command for the default profile, so the "
        "agent cannot verify its own work")
    return command


def _repo_relative_paths(command: list[str]) -> list[str]:
    """Arguments that look like paths into this repository."""
    return [
        argument for argument in command[1:]
        if ("/" in argument or argument.endswith((".py", ".sh", ".ps1")))
        and not argument.startswith("-")
        and "import " not in argument
    ]


def test_the_configured_interpreter_is_a_python(test_command):
    interpreter = Path(test_command[0]).name.lower()
    assert interpreter.startswith("python"), (
        f"the configured test command starts with {test_command[0]!r}. This test "
        "substitutes the running interpreter to execute it, which is only honest while "
        "the configured command is a Python one. If that changes deliberately, this "
        "assertion is the place to say so.")


def test_every_path_in_the_test_command_exists(test_command):
    missing = [p for p in _repo_relative_paths(test_command) if not (ROOT / p).exists()]
    assert not missing, (
        "the configured repository test command names paths that do not exist. The "
        "agent cannot test its own checkout, and no other unit test would notice:\n  "
        + "\n  ".join(missing))


def test_every_path_in_the_build_command_exists(config):
    command = config.profile().build
    missing = [p for p in _repo_relative_paths(command) if not (ROOT / p).exists()]
    assert not missing, (
        "the configured build command names paths that do not exist:\n  "
        + "\n  ".join(missing))
    inline = " ".join(command)
    if "compileall" in inline:
        # Quoted repository paths inside an inline `python -c` program. Extracted by
        # pattern rather than by splitting on spaces, because the real command is
        # `compileall.compile_dir('internal/local_agent', quiet=1)` and a naive split
        # yields `internal/local_agent',` which exists nowhere and fails for the wrong
        # reason.
        targets = re.findall(r'''['"](internal/[^'"]+)['"]''', inline)
        absent = [t for t in targets if not (ROOT / t).exists()]
        assert not absent, (
            "the build command compiles directories that do not exist, so a green "
            f"build would prove nothing:\n  " + "\n  ".join(absent))


def _invoke(test_command: list[str], target: Path) -> subprocess.CompletedProcess:
    """Run the configured command with its test path replaced by `target`.

    `sys.executable` replaces the configured interpreter name so the subprocess is the
    interpreter running this suite. Everything after argv[0] is the configured command,
    which is the part a move would break.
    """
    paths = _repo_relative_paths(test_command)
    argv = [sys.executable]
    for argument in test_command[1:]:
        argv.append(str(target) if paths and argument == paths[-1] else argument)
    if not paths:
        argv.append(str(target))
    return subprocess.run(
        argv, cwd=ROOT, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS)


def test_the_configured_test_command_runs_and_reports_a_pass(test_command, probe_dir):
    (probe_dir / "test_passing_case.py").write_text(PASSING)
    result = _invoke(test_command, probe_dir)
    assert result.returncode == 0, (
        "the configured repository test command could not run a trivial passing test.\n"
        f"argv: {test_command}\nstdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-2000:]}")
    assert "1 passed" in result.stdout, (
        f"the command ran but did not report the passing case:\n{result.stdout[-2000:]}")


def test_the_configured_test_command_reports_a_failure(test_command, probe_dir):
    """A runner that exits 0 on a failing test is worse than no runner."""
    (probe_dir / "test_failing_case.py").write_text(FAILING)
    result = _invoke(test_command, probe_dir)
    assert result.returncode != 0, (
        "the configured repository test command exited 0 with a failing test in the "
        f"tree. Every 'tests passed' the agent reports is then worthless.\n"
        f"stdout:\n{result.stdout[-2000:]}")


def _configured_script(test_command: list[str]) -> str:
    script = next(
        (p for p in _repo_relative_paths(test_command) if p.endswith(".py")), None)
    assert script, f"no script path in the configured test command: {test_command}"
    return script.replace("\\", "/")


def test_ci_executes_the_same_runner_path_the_agent_is_configured_with(test_command):
    """CI and the agent must run the same file, compared by path and not by basename.

    An earlier version of this test compared `Path(script).name`. That passes a move
    from `internal/measurement/run_test_suite.py` to `internal/devtools/run_test_suite.py`
    with the workflow left pointing at the old path, because the basename is unchanged
    and the workflow still contains the word. The workflow would then fail on a file
    that no longer exists, and this test would have said the paths agreed.

    So: compare the exact repository-relative path, and find it in a line the workflow
    actually executes, not merely somewhere in the file.
    """
    workflow = ROOT / ".github" / "workflows" / "tests.yml"
    if not workflow.is_file():
        pytest.skip("no tests workflow in this checkout")
    script = _configured_script(test_command)

    executed = [
        line.strip() for line in workflow.read_text(encoding="utf-8").splitlines()
        if "run:" in line and script.rsplit("/", 1)[-1] in line
    ]
    assert executed, (
        f"no executed step in .github/workflows/tests.yml runs {script}. Either CI does "
        "not run the configured self-test runner at all, or a move left the workflow "
        "behind.")
    mismatched = [line for line in executed if script not in line]
    assert not mismatched, (
        f"CI executes a different path than the agent is configured with.\n"
        f"  .local-agent.toml: {script}\n  workflow:          "
        + "\n                     ".join(mismatched))


def test_no_other_file_names_a_stale_path_for_the_runner(test_command):
    """Documentation and fixtures may mention the runner; they may not mention it wrongly.

    Deliberately weaker than the workflow assertion above. `conftest.py` only mentions
    the runner in a docstring, so requiring it to name the runner would be asserting a
    runtime invariant that does not exist. What can be asserted is that every mention
    anywhere is a suffix of the current path: `measurement/run_test_suite.py` is fine
    while the file lives there, and becomes a failure the moment it moves.
    """
    script = _configured_script(test_command)
    basename = script.rsplit("/", 1)[-1]
    pattern = re.compile(r"[\w./\\-]*" + re.escape(basename))

    candidates = [
        INTERNAL / "tests" / "conftest.py",
        ROOT / ".github" / "workflows" / "tests.yml",
        ROOT / "AGENTS.md",
        INTERNAL / "README.md",
        *sorted(ROOT.glob("*.ps1")),
        *sorted(INTERNAL.glob("*.ps1")),
    ]
    stale: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for fragment in pattern.findall(line):
                fragment = fragment.replace("\\", "/").lstrip("./")
                if fragment != basename and not script.endswith(fragment):
                    stale.append(f"{path.relative_to(ROOT)}:{number} names {fragment!r}")
    assert not stale, (
        f"these name a path for the self-test runner that is not {script}. A move "
        f"updated the configuration and left these behind:\n  " + "\n  ".join(stale))
