"""Every workspace git process is an owned process tree (#415).

Workspace git used ``subprocess.run(..., timeout=...)``. On timeout that kills only the
direct child; a descendant (a commit signer and anything it started) keeps the output
pipe open, so the call then blocked until the descendant chose to exit, and the
descendant outlived the task. Now the whole tree is ended and the call returns.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from local_agent.session import workspaces
from local_agent.session.workspaces import GitWorkspaceManager, WorkspaceError

_CHILD = """\
import pathlib, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path(sys.argv[1]).write_text(str(grandchild.pid))
time.sleep(60)
"""


def _fake_git(bin_dir: Path, child: Path, pid_file: Path) -> None:
    """A ``git`` on PATH that starts a long-lived grandchild sharing its output."""
    bin_dir.mkdir()
    if os.name == "nt":
        (bin_dir / "git.cmd").write_text(
            f'@"{sys.executable}" "{child}" "{pid_file}"\r\n', encoding="utf-8")
    else:
        script = bin_dir / "git"
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{child}" "{pid_file}"\n',
                          encoding="utf-8")
        script.chmod(0o755)


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _wait_for_pid(pid_file: Path) -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text())
        time.sleep(0.05)
    raise AssertionError("the fake git never started its grandchild")


def test_a_git_tree_past_its_timeout_is_ended_and_the_call_returns(tmp_path, monkeypatch):
    child = tmp_path / "child.py"
    child.write_text(_CHILD, encoding="utf-8")
    pid_file = tmp_path / "grandchild.pid"
    _fake_git(tmp_path / "bin", child, pid_file)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(workspaces, "_GIT_TIMEOUT_S", 3)
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="test")

    started = time.monotonic()
    with pytest.raises(WorkspaceError) as refused:
        manager._git(tmp_path, "status")
    elapsed = time.monotonic() - started

    grandchild = _wait_for_pid(pid_file)
    try:
        # Old code: the grandchild held the pipe, so this took the grandchild's 60 s.
        assert elapsed < 20, elapsed
        deadline = time.monotonic() + 5
        while not _gone(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _gone(grandchild), "the git process tree outlived its timeout"
        assert "did not finish within 3 s" in str(refused.value)
    finally:
        if not _gone(grandchild):
            psutil.Process(grandchild).kill()


def test_git_output_beyond_the_limit_is_refused_not_truncated(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    monkeypatch.setattr(workspaces, "_GIT_OUTPUT_LIMIT", 8)
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="test")
    with pytest.raises(WorkspaceError, match="over the 8-byte limit"):
        manager._git(tmp_path / "repo", "version")


def test_stdin_reaches_git_and_bytes_come_back(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="test")
    done = manager._git(repo, "hash-object", "--stdin", stdin=b"hello\n")
    assert done.returncode == 0
    assert done.stdout.strip() == b"ce013625030ba8dba906f756967f9e9ca394464a"


@pytest.mark.skipif(os.name == "nt", reason="the POSIX signer script is a shell script")
def test_a_hung_commit_signer_is_ended_and_nothing_is_committed(tmp_path, monkeypatch):
    """The real case: /commit honours the user's signer, which can hang."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    child = tmp_path / "child.py"
    child.write_text(_CHILD, encoding="utf-8")
    pid_file = tmp_path / "grandchild.pid"
    signer = tmp_path / "signer"
    signer.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{child}" "{pid_file}"\n',
                      encoding="utf-8")
    signer.chmod(0o755)
    for key, value in (("user.name", "t"), ("user.email", "t@e.invalid"),
                       ("commit.gpgsign", "true"), ("gpg.format", "openpgp"),
                       ("gpg.program", str(signer))):
        subprocess.run(["git", "config", key, value], cwd=repo, check=True)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    monkeypatch.setattr(workspaces, "_GIT_TIMEOUT_S", 3)
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="test")

    started = time.monotonic()
    with pytest.raises(WorkspaceError) as refused:
        manager._git(repo, "commit", "-q", "-m", "signed")
    elapsed = time.monotonic() - started

    grandchild = _wait_for_pid(pid_file)
    try:
        assert elapsed < 20, elapsed
        deadline = time.monotonic() + 5
        while not _gone(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _gone(grandchild), "the signer's descendant outlived the commit"
        head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=repo,
                              capture_output=True, check=False)
        assert head.returncode != 0, "a commit was made"
        assert "did not finish within 3 s" in str(refused.value)
    finally:
        if not _gone(grandchild):
            psutil.Process(grandchild).kill()


# --- Review blockers on #420 (Astra 4 Oct, ChatGPT 5 Oct) ---------------------------

_ABANDONING_CHILD = """\
import pathlib, subprocess, sys
grandchild = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
pathlib.Path(sys.argv[1]).write_text(str(grandchild.pid))
"""


def test_a_git_that_exits_normally_cannot_leave_a_descendant_running(tmp_path, monkeypatch):
    """Normal exit, not timeout: git starts a 60 s grandchild and exits 0 at once.

    Old code ended strays only inside a Windows job, so on POSIX the grandchild
    outlived a successful call.
    """
    child = tmp_path / "child.py"
    child.write_text(_ABANDONING_CHILD, encoding="utf-8")
    pid_file = tmp_path / "grandchild.pid"
    _fake_git(tmp_path / "bin", child, pid_file)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="test")

    started = time.monotonic()
    try:
        done = manager._git(tmp_path, "status")
        outcome = "returned"
    except WorkspaceError:
        done = None
        outcome = "refused"
    elapsed = time.monotonic() - started

    grandchild = _wait_for_pid(pid_file)
    try:
        assert elapsed < 30, elapsed
        # Either the step succeeded with its tree confirmed ended, or it failed closed.
        assert _gone(grandchild), f"git {outcome} while its descendant was still running"
        if done is not None:
            assert done.returncode == 0
    finally:
        if not _gone(grandchild):
            psutil.Process(grandchild).kill()


def _marker_processes(marker: str) -> list[psutil.Process]:
    found = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if marker in " ".join(proc.info["cmdline"] or ()) and not _gone(proc.pid):
                found.append(proc)
        except psutil.Error:
            continue
    return found


def test_a_raising_on_spawn_callback_never_strands_the_child(tmp_path):
    from local_agent.tools.process_runner import OwnedLifecycle, run_owned

    marker = f"lca-on-spawn-{time.monotonic_ns()}"

    def boom() -> None:
        raise RuntimeError("durable record failed")

    with pytest.raises(RuntimeError, match="durable record failed"):
        run_owned(
            [sys.executable, "-c", "import time, sys; time.sleep(60)", marker],
            tmp_path,
            OwnedLifecycle(30, on_spawn=boom),
            env=dict(os.environ),
        )
    deadline = time.monotonic() + 5
    while _marker_processes(marker) and time.monotonic() < deadline:
        time.sleep(0.05)
    survivors = _marker_processes(marker)
    for proc in survivors:
        proc.kill()
    assert not survivors, "the child outlived a raising on_spawn callback"


_FLOOD = """\
import sys, time
chunk = b"x" * 65536
while True:
    sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
    sys.stderr.buffer.write(chunk); sys.stderr.buffer.flush()
"""

_SPLIT_THEN_SLEEP = """\
import sys, time
sys.stdout.buffer.write(b"o" * 600_000); sys.stdout.buffer.flush()
sys.stderr.buffer.write(b"e" * 600_000); sys.stderr.buffer.flush()
time.sleep(60)
"""


@pytest.mark.parametrize("script", [_FLOOD, _SPLIT_THEN_SLEEP], ids=["flood", "split"])
def test_output_limit_ends_the_tree_while_it_runs(tmp_path, script):
    """The limit is an execution bound on stdout+stderr together, not a post-exit check.

    Old code checked each stream separately only after exit, so a flooding helper ran
    to its timeout and two streams could each sit just under the limit.
    """
    from local_agent.tools.process_runner import (
        CommandOutputLimitError,
        OwnedLifecycle,
        run_owned,
    )

    marker = f"lca-output-limit-{time.monotonic_ns()}"
    limit = 1_000_000
    started = time.monotonic()
    with pytest.raises(CommandOutputLimitError, match="stdout and stderr together") as ended:
        run_owned(
            [sys.executable, "-c", script, marker],
            tmp_path,
            OwnedLifecycle(45, output_limit=limit),
            env=dict(os.environ),
        )
    elapsed = time.monotonic() - started
    assert elapsed < 15, elapsed
    assert ended.value.cleanup_confirmed is not None
    deadline = time.monotonic() + 5
    while _marker_processes(marker) and time.monotonic() < deadline:
        time.sleep(0.05)
    survivors = _marker_processes(marker)
    for proc in survivors:
        proc.kill()
    assert not survivors, "the writer outlived the output limit"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object accounting (#435)")
def test_a_plain_command_never_reports_a_phantom_stray(tmp_path):
    """Job accounting can lag the direct child's exit; that is not an abandoned process."""
    from local_agent.tools.process_runner import OwnedLifecycle, run_owned

    for _ in range(25):
        run = run_owned(
            [sys.executable, "-c", "print('ok')"], tmp_path, OwnedLifecycle(30),
            env=dict(os.environ),
        )
        assert run.exit_code == 0
        assert run.stray_descendants_at_exit == 0, run
        assert run.cleanup_confirmed is None, run


# --- Astra's adversarial POSIX escapes on e7ac54f (#436 review 5423223584) -----------

_ESCAPING_FIRST_ACTION = """\
import pathlib, subprocess, sys
# The command's very first effect: start a descendant in a new session, which a
# process-group kill cannot reach, and record it.
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", sys.argv[2]],
                              start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(str(grandchild.pid))
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups; Windows runs under a job")
def test_a_raising_on_spawn_runs_no_effect_at_all_not_even_an_escaping_one(tmp_path):
    """Old code ran the command before on_spawn returned, so a setsid() grandchild
    escaped the group kill when the callback then raised."""
    from local_agent.tools.process_runner import OwnedLifecycle, run_owned

    marker = f"lca-escape-{time.monotonic_ns()}"
    pid_file = tmp_path / "escaped.pid"
    child = tmp_path / "child.py"
    child.write_text(_ESCAPING_FIRST_ACTION, encoding="utf-8")

    def durable_record_fails() -> None:
        # Give an ungated child every chance to act first.
        deadline = time.monotonic() + 2
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        raise RuntimeError("durable record failed")

    try:
        with pytest.raises(RuntimeError, match="durable record failed"):
            run_owned([sys.executable, str(child), str(pid_file), marker], tmp_path,
                      OwnedLifecycle(30, on_spawn=durable_record_fails), env=dict(os.environ))
        time.sleep(0.5)
        assert not pid_file.exists(), "the command ran before its on_spawn record succeeded"
        assert not _marker_processes(marker), "a descendant escaped a raising on_spawn"
    finally:
        for proc in _marker_processes(marker):
            proc.kill()


_ESCAPING_WRITER = """\
import os, pathlib, subprocess, sys
writer = subprocess.Popen([sys.executable, "-c", '''
import os, sys, time
progress, n = sys.argv[1], 0
chunk = b"w" * 65536
while True:
    os.write(1, chunk)
    n += 1
    with open(progress, "w") as f:
        f.write(str(n))
    time.sleep(0.01)
''', sys.argv[1], sys.argv[2]], start_new_session=True)
# The parent exits at once with success; the escaped writer keeps the stdout it inherited.
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups; Windows runs under a job")
def test_an_escaped_writer_cannot_grow_the_capture_after_the_owner_returns(tmp_path):
    """Old code captured into a file the escaped writer kept growing after return."""
    from local_agent.tools.process_runner import OwnedLifecycle, run_owned

    marker = f"lca-writer-{time.monotonic_ns()}"
    progress = tmp_path / "progress"
    script = tmp_path / "parent.py"
    script.write_text(_ESCAPING_WRITER, encoding="utf-8")
    try:
        run = run_owned([sys.executable, str(script), str(progress), marker], tmp_path,
                        OwnedLifecycle(30, output_limit=8 * 1024 * 1024), env=dict(os.environ))
        assert run.exit_code == 0
        assert len(run.stdout) + len(run.stderr) <= 8 * 1024 * 1024
        deadline = time.monotonic() + 5
        while progress.exists() is False and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)
        before = progress.read_text(encoding="utf-8") if progress.exists() else "0"
        time.sleep(0.5)
        after = progress.read_text(encoding="utf-8") if progress.exists() else "0"
        # After the owner returned, the writer's next write fails (EPIPE): no growth.
        assert before == after, f"an escaped writer kept writing after return: {before} -> {after}"
    finally:
        for proc in _marker_processes(marker):
            proc.kill()


# --- Astra's round-2 review of 22f9bac (#436) -------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor numbering")
def test_a_clean_command_runs_when_the_controller_holds_high_numbered_descriptors(tmp_path):
    """select() refuses descriptors at or above FD_SETSIZE (1024); a long-lived
    controller reaches them. Old code raised ValueError for a clean command."""
    import resource

    from local_agent.tools.process_runner import OwnedLifecycle, run_owned

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 1200
    if soft < wanted:
        if hard != resource.RLIM_INFINITY and hard < wanted:
            pytest.skip(f"descriptor hard limit {hard} is below {wanted}")
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    held = []
    try:
        while True:
            fd = os.open(os.devnull, os.O_RDONLY)
            held.append(fd)
            if fd > 1031:
                break
        run = run_owned([sys.executable, "-c", "print('ok')"], tmp_path,
                        OwnedLifecycle(30, output_limit=1024), env=dict(os.environ))
        assert run.exit_code == 0
        assert run.stdout.strip() == b"ok"
    finally:
        for fd in held:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_run_command_ends_a_flooding_command_at_its_output_bound(tmp_path, monkeypatch):
    """Configured build/test commands are bounded too, with a typed result. Old code
    gave run_command no limit, so a flooding command filled controller memory."""
    from local_agent.tools import process_runner
    from local_agent.tools.tool_primitives import Reason, ToolError

    monkeypatch.setattr(process_runner, "COMMAND_OUTPUT_LIMIT", 1_000_000)
    flood = "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()\n"
    started = time.monotonic()
    with pytest.raises(ToolError) as ended:
        process_runner.run_command([sys.executable, "-c", flood], tmp_path,
                                   tmp_path / "runs", 60)
    assert time.monotonic() - started < 20
    assert ended.value.reason is Reason.OUTPUT_LIMIT
    assert "not a result" in str(ended.value)


def test_a_normal_exit_without_a_job_object_is_never_reported_as_an_owned_finish(monkeypatch):
    """Windows visible-tree fallback: the direct child exited, but its descendants
    cannot be counted, and one could keep growing the capture file. Old code passed
    silently, so workspace Git reported success."""
    from local_agent.tools import process_runner

    monkeypatch.setattr(process_runner.sys, "platform", "win32")
    tree = process_runner._Tree(job=None, containment="visible_tree")
    process_runner._end_strays(object(), tree)  # type: ignore[arg-type]
    assert tree.strays_unconfirmed is True
    assert tree.stray_descendants is None


def test_workspace_git_refuses_a_run_whose_tree_could_not_be_shown_ended(tmp_path, monkeypatch):
    from local_agent.tools.process_runner import OwnedRun

    def uncontained(argv, cwd, lifecycle, **kwargs):
        return OwnedRun(exit_code=0, stdout=b"", stderr=b"", elapsed_s=0.01, timed_out=False,
                        cancel_requested=False, cleanup_confirmed=None,
                        containment="visible_tree", stray_descendants_at_exit=None,
                        strays_unconfirmed=True)

    monkeypatch.setattr(workspaces, "run_owned", uncontained)
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="test")
    with pytest.raises(WorkspaceError, match="could not be shown ended.*visible_tree"):
        manager._git(tmp_path, "status")


# --- Astra's round-3 review of c806365 (#436): the public boundary fails closed -----


def _uncontained_success(*_args, **_kwargs):
    from local_agent.tools.process_runner import OwnedRun

    return OwnedRun(exit_code=0, stdout=b"build complete\n", stderr=b"", elapsed_s=0.01,
                    timed_out=False, cancel_requested=False, cleanup_confirmed=None,
                    containment="visible_tree", stray_descendants_at_exit=None,
                    strays_unconfirmed=True)


def test_run_command_never_returns_ok_for_a_tree_it_could_not_show_ended(tmp_path, monkeypatch):
    """Old code dropped strays_unconfirmed and returned RunOutcome.ok=True."""
    from local_agent.tools import process_runner
    from local_agent.tools.tool_primitives import Reason, ToolError

    monkeypatch.setattr(process_runner, "run_owned", _uncontained_success)
    with pytest.raises(ToolError) as refused:
        process_runner.run_command(["git", "status"], tmp_path, tmp_path / "runs", 30)
    assert refused.value.reason is Reason.CLEANUP_UNKNOWN
    assert "visible_tree" in str(refused.value)
    # The evidence is still written for the user to inspect.
    assert list((tmp_path / "runs").glob("*/command.txt"))


def test_a_configured_build_cannot_pass_on_an_unsettled_tree(sandbox, monkeypatch):
    """Composition: build_target must not certify PASS or a build stamp from it."""
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry, process_runner
    from local_agent.tools.tool_primitives import ToolError

    sandbox.scenario("clean")
    registry, _ctx, _store = build_registry(load_repo_config(sandbox.root))
    monkeypatch.setattr(process_runner, "run_owned", _uncontained_success)
    try:
        result = registry.get("build_target").handler()
    except ToolError as exc:
        assert "could not be shown ended" in str(exc)
    else:
        assert not result.ok, result
        assert result.domain_status.value != "pass", result
    from local_agent.tools.testing_tools import BUILD_STAMP

    assert not (sandbox.root / "build" / BUILD_STAMP).exists(), "a build stamp was written"


# --- Astra's round-4 review of a367b87 (#436): the verdict boundary consumes it ------


def test_public_build_on_an_unsettled_tree_is_no_verdict_cleanup_unknown(
    sandbox, tmp_path, monkeypatch,
):
    """Real durable public route, fixed configured plan, no model. Old code projected
    the typed cleanup_unknown into FAIL / missing_evidence."""
    from local_agent.session.contracts import TaskOutcome
    from local_agent.session.conversation_store import conversation, create_session, new_session
    from local_agent.tools import process_runner
    from test_public_candidate_restart import _gateway
    from test_session_hub_product_path import _load_hub

    real = process_runner.run_owned

    def unsettled(argv, cwd, lifecycle, **kwargs):
        if lifecycle.on_spawn is not None:
            lifecycle.on_spawn()  # the real durable "started" boundary
        return _uncontained_success()

    hub = _load_hub()
    sandbox.scenario("clean")
    runtime = tmp_path / "runtime"
    session = new_session("fixture", "scripted", "CPU")
    create_session(runtime, session)
    service, _ = hub._open_durable_service(runtime, session.conversation_id)
    try:
        with conversation(runtime, session.conversation_id) as opened:
            manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40)
            gateway = _gateway(sandbox.root, service, manager, opened)
            monkeypatch.setattr(process_runner, "run_owned", unsettled)
            gateway.turn("build it")
            monkeypatch.setattr(process_runner, "run_owned", real)
            result = gateway.last_result
    finally:
        service.close()

    assert result is not None
    # By value: test_session_import_boundary reloads local_agent.session, so enum
    # members from another import are equal but not identical.
    assert result.outcome.value == TaskOutcome.NO_VERDICT.value, (result.outcome, result.answer)
    assert result.reason_code == "cleanup_unknown"
    assert result.verified_at_completion is False


# --- Astra's round-5 review of aa75316 (#436): the candidate check decides ---------


def test_a_candidate_check_with_unknown_cleanup_decides_the_candidate_verdict(tmp_path):
    """The controller check ran on the changed candidate and its tree could not be shown
    ended. Old code returned the worker's earlier NO_VERDICT / missing_evidence with
    the worker as proof source, erasing the later, stronger fact."""
    from local_agent.agent.orchestrator import Outcome, RunResult
    from local_agent.agent.state import AgentState, ToolCallRecord
    from local_agent.session import task_controller

    worker_state = AgentState(task="change: x", repo_root=tmp_path)
    worker_state.mutation_epoch = 1
    worker_state.claim = "success"
    worker = RunResult(answer="changed it", state=worker_state, outcome=Outcome.PASS)

    check_state = AgentState(task="controller check", repo_root=tmp_path)
    check_state.history.append(ToolCallRecord(
        name="build_target", arguments={}, verdict="error", ok=False,
        summary="process tree could not be shown ended", execution="error",
        reason="cleanup_unknown",
    ))
    check = RunResult(answer="", state=check_state, outcome=Outcome.FAIL)

    controller = object.__new__(task_controller.TaskController)
    controller._controller_check = lambda *_args: check  # type: ignore[method-assign]
    proof = controller._prove_candidate("implement-change", worker, None, None)  # type: ignore[arg-type]

    assert proof.outcome.value == "no_verdict"
    assert proof.reason_code == "cleanup_unknown"
    assert proof.verified is False
    assert proof.proof_run is check
