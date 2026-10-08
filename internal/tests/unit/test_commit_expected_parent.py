"""A candidate commit is published only onto the parent it was built from (#453).

The private tree is ``parent`` plus exactly the candidate paths. Publishing it after the
branch moved would make it the child of newer user work while carrying the older
contents, silently reverting that work in the new HEAD. The built commit is verified
before anything names it, and publication is one ``update-ref`` of the branch from
``parent`` that LCA's reference-transaction hook lets through only while HEAD still
names that branch. Anything that moved the branch or HEAD first makes it refuse with no
ref changed.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

import pytest

from local_agent.session import commit_index_hook
from local_agent.session.workspaces import CommitDrifted, Committed, CommitRefused
from test_commit_literal_paths import _apply, _committed_paths, _git, _repo


def _state(user: Path) -> tuple[str, ...]:
    return (
        _git(user, "rev-parse", "HEAD"),
        _git(user, "ls-files", "--stage"),
        _git(user, "status", "--porcelain=v1", "--untracked-files=all"),
        (user / "a.txt").read_text(encoding="utf-8"),
        (user / "b.txt").read_text(encoding="utf-8"),
    )


def _is(step: str, args: tuple[str, ...]) -> bool:
    """``update-ref`` means the publication, not the hook probe that precedes it."""
    if not args or args[0] != step:
        return False
    return step != "update-ref" or "--stdin" not in args


def _user_commits_b_before(manager, user: Path, step: str, monkeypatch) -> list[str]:
    real_git = manager._git
    concurrent: list[str] = []

    def race(cwd, *args, **kwargs):
        if _is(step, args) and not concurrent:
            (user / "b.txt").write_text("user committed work\n", encoding="utf-8")
            _git(user, "add", "b.txt")
            _git(user, "commit", "-qm", "user concurrent commit")
            concurrent.append(_git(user, "rev-parse", "HEAD").strip())
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", race)
    return concurrent


@pytest.mark.parametrize("step", ["commit-tree", "update-ref"])
def test_a_concurrent_user_commit_is_never_published_over(tmp_path, monkeypatch, step):
    """Astra's #453 interleaving, at the last moment before each publication step."""
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    concurrent = _user_commits_b_before(manager, user, step, monkeypatch)

    done = manager.commit_applied(task, user, "candidate")

    assert concurrent, "the interleaving never ran"
    assert isinstance(done, CommitRefused), done
    assert "moved or HEAD changed while the commit was being prepared" in done.reason
    head = _git(user, "rev-parse", "HEAD").strip()
    assert head == concurrent[0], "a ref advanced past the user's commit"
    assert _git(user, "show", "HEAD:b.txt") == "user committed work\n"
    assert (user / "b.txt").read_text(encoding="utf-8") == "user committed work\n"
    assert (user / "a.txt").read_text(encoding="utf-8") == "candidate\n"
    record = json.loads((manager.workspaces_root / f"{task}.applied.json").read_text(
        encoding="utf-8"))
    assert "committed" not in record
    assert not list(manager.workspaces_root.glob(".commit-index-*"))
    assert not _git(user, "for-each-ref", "refs/lca-probe/"), "the hook probe left a ref"


def test_a_refused_publication_leaves_the_users_state_byte_exact(tmp_path, monkeypatch):
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    real_git = manager._git
    seen: list[tuple[str, ...]] = []

    def race(cwd, *args, **kwargs):
        if _is("update-ref", args) and not seen:
            (user / "b.txt").write_text("user committed work\n", encoding="utf-8")
            _git(user, "add", "b.txt")
            _git(user, "commit", "-qm", "user concurrent commit")
            seen.append(_state(user))
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", race)
    done = manager.commit_applied(task, user, "candidate")
    assert isinstance(done, CommitRefused), done
    assert _state(user) == seen[0], "the refusal changed the user's HEAD, index or files"


def test_the_refused_candidate_commits_cleanly_on_retry(tmp_path, monkeypatch):
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    concurrent = _user_commits_b_before(manager, user, "update-ref", monkeypatch)
    assert isinstance(manager.commit_applied(task, user, "candidate"), CommitRefused)
    monkeypatch.undo()

    done = manager.commit_applied(task, user, "candidate")

    assert isinstance(done, Committed), done
    assert _git(user, "rev-parse", "HEAD~1").strip() == concurrent[0]
    assert _committed_paths(user) == ["a.txt"]
    assert _git(user, "show", "HEAD:b.txt") == "user committed work\n"


@pytest.mark.parametrize("move", ["switch", "detach"])
def test_a_concurrent_branch_change_moves_no_ref(tmp_path, monkeypatch, move):
    """Astra's probe: a switch to another branch at the same commit, or a detach, at the
    publication boundary. The captured branch's CAS alone would still succeed; the
    hook's HEAD binding refuses it, so no ref moves at all."""
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    branch = _git(user, "symbolic-ref", "HEAD").strip()
    parent = _git(user, "rev-parse", "HEAD").strip()
    real_git = manager._git
    moved: list[str] = []

    def change_branch(cwd, *args, **kwargs):
        if _is("update-ref", args) and not moved:
            if move == "switch":
                _git(user, "switch", "-q", "-c", "elsewhere")
            else:
                _git(user, "switch", "-q", "--detach")
            moved.append(move)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", change_branch)
    index_before = _git(user, "ls-files", "--stage")
    refs_before = _git(user, "for-each-ref")
    done = manager.commit_applied(task, user, "candidate")

    assert moved
    assert isinstance(done, CommitRefused), done
    assert _git(user, "rev-parse", branch).strip() == parent
    assert _git(user, "rev-parse", "HEAD").strip() == parent
    expected_refs = set(refs_before.splitlines())
    if move == "switch":
        expected_refs.add(f"{parent} commit\trefs/heads/elsewhere")
    assert set(_git(user, "for-each-ref").splitlines()) == expected_refs
    assert _git(user, "ls-files", "--stage") == index_before


def test_candidate_bytes_changed_before_private_staging_move_no_ref(tmp_path, monkeypatch):
    """Astra's second probe: the user edits a candidate file just before LCA stages it.
    The built commit is checked before publication, so those bytes never reach HEAD."""
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    head = _git(user, "rev-parse", "HEAD")
    real_git = manager._git
    edited: list[str] = []

    def user_edits(cwd, *args, **kwargs):
        if args and args[0] == "add" and kwargs.get("env_extra") and not edited:
            (user / "a.txt").write_text("concurrent user bytes\n", encoding="utf-8")
            edited.append("a.txt")
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", user_edits)
    done = manager.commit_applied(task, user, "candidate")

    assert edited
    assert isinstance(done, CommitDrifted), done
    assert done.drifted == ("a.txt",)
    assert _git(user, "rev-parse", "HEAD") == head
    assert (user / "a.txt").read_text(encoding="utf-8") == "concurrent user bytes\n"


def test_the_reference_transaction_hook_reconciles_inside_publication(tmp_path, monkeypatch):
    """The controller's own pass is a no-op here: the hook already advanced the index."""
    import local_agent.session.workspaces as workspaces_module

    calls: list[Path] = []

    def observe(transaction: Path) -> tuple[str, ...]:
        calls.append(transaction)
        return ()

    monkeypatch.setattr(workspaces_module, "reconcile_and_report", observe)
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    done = manager.commit_applied(task, user, "candidate")
    assert isinstance(done, Committed), done
    assert calls, "the controller pass did not run"
    assert _git(user, "rev-parse", ":a.txt") == _git(user, "rev-parse", "HEAD:a.txt")


def test_the_hook_ignores_ref_updates_that_are_not_its_publication(tmp_path):
    """Even with HEAD on the named commit, only the exact ref update it was prepared for
    lets the hook touch the index."""
    user = _repo(tmp_path, {"a.txt": "old a\n"})
    parent = _git(user, "rev-parse", "HEAD").strip()
    branch = _git(user, "symbolic-ref", "HEAD").strip()
    expected = subprocess.run(["git", "ls-files", "--stage", "-z", "--", "a.txt"],  # noqa: S607
                              cwd=user, capture_output=True, check=True).stdout
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=user,  # noqa: S607
                          input=b"other\n", capture_output=True, check=True).stdout.strip()
    target = b"100644 " + blob + b" 0\ta.txt\0"
    workspaces = tmp_path / "ws-hook"
    workspaces.mkdir()
    live = Path(_git(user, "rev-parse", "--git-path", "index").strip())
    transaction = commit_index_hook.prepare_transaction(
        workspaces, user, live if live.is_absolute() else user / live,
        commit_index_hook.Publication(ref=branch, parent=parent, commit=parent),
        (("a.txt", expected, target),),
    )
    index_before = _git(user, "ls-files", "--stage")
    for unrelated in (f"{parent} {parent} refs/heads/elsewhere\n", "", "garbage\n"):
        assert commit_index_hook.main([str(transaction), "committed"], updates=unrelated) == 0
        assert _git(user, "ls-files", "--stage") == index_before
    # Control: the matching update does advance it, so the refusals above are not vacuous.
    assert commit_index_hook.main([str(transaction), "committed"],
                                  updates=f"{parent} {parent} {branch}\n") == 0
    assert _git(user, "ls-files", "--stage") != index_before


def test_the_prepared_hook_admits_only_its_branch_move_through_head(tmp_path):
    """Publication is ``update-ref HEAD``: Git reports the branch HEAD resolved to (and,
    on some versions, HEAD's own log line). Only this branch's move is admitted."""
    user = _repo(tmp_path, {"a.txt": "old a\n"})
    parent = _git(user, "rev-parse", "HEAD").strip()
    branch = _git(user, "symbolic-ref", "HEAD").strip()
    new = "1" * 40
    workspaces = tmp_path / "ws-prepared"
    workspaces.mkdir()
    live = Path(_git(user, "rev-parse", "--git-path", "index").strip())
    transaction = commit_index_hook.prepare_transaction(
        workspaces, user, live if live.is_absolute() else user / live,
        commit_index_hook.Publication(ref=branch, parent=parent, commit=new), (),
    )
    bound = Path(str(transaction) + commit_index_hook.BOUND_SUFFIX)
    for refused in (
        f"{parent} {new} HEAD\n",                                   # HEAD was detached
        f"{parent} {new} refs/heads/elsewhere\n",                   # HEAD named another
        f"{parent} {new} refs/heads/elsewhere\n{parent} {new} HEAD\n",
        f"{parent} {new} {branch}\n{parent} {new} refs/heads/extra\n",
        f"{parent} {'2' * 40} {branch}\n",                          # another commit
        "",
    ):
        assert commit_index_hook.main([str(transaction), "prepared"], updates=refused) == 1
        assert not bound.exists(), refused
    for admitted in (f"{parent} {new} {branch}\n",                  # Git 2.51
                     f"{parent} {new} HEAD\n{parent} {new} {branch}\n"):  # Git 2.43
        bound.unlink(missing_ok=True)
        assert commit_index_hook.main([str(transaction), "prepared"], updates=admitted) == 0
        assert bound.is_file()


def test_a_plain_uncontended_commit_is_published(tmp_path):
    """Acceptance (Astra, c24d4c5): the ordinary path must commit on every Git."""
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    parent = _git(user, "rev-parse", "HEAD").strip()
    done = manager.commit_applied(task, user, "candidate")
    assert isinstance(done, Committed), done
    assert _git(user, "rev-parse", "HEAD~1").strip() == parent
    assert _git(user, "symbolic-ref", "HEAD").strip() == f"refs/heads/{done.branch}"
    assert _committed_paths(user) == ["a.txt"]
    assert _git(user, "diff", "--cached", "--name-only") == ""
    assert not _git(user, "for-each-ref", "refs/lca-probe/")


@pytest.mark.parametrize(("key", "value"), [
    ("commit.cleanup", "invalid"),
    ("commit.gpgsign", "invalid"),
])
def test_configuration_git_commit_rejects_is_refused_not_normalised(tmp_path, key, value):
    """Astra (03:38Z): an invalid cleanup must not become stripspace, and an invalid
    signing setting must never become an unsigned commit."""
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    _git(user, "config", key, value)
    before = _state(user)
    done = manager.commit_applied(task, user, "candidate")
    assert isinstance(done, CommitRefused), done
    assert _state(user) == before


def test_without_a_runnable_binding_hook_nothing_is_published(tmp_path, monkeypatch):
    """The HEAD binding lives in the hook, so a Git or host that does not run it (Git
    before 2.28, no sh) must refuse, not publish unbound."""
    import local_agent.session.workspaces as workspaces_module

    monkeypatch.setattr(workspaces_module, "hook_environment", lambda _transaction: {})
    user = _repo(tmp_path, {"a.txt": "old a\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    head = _git(user, "rev-parse", "HEAD")
    done = manager.commit_applied(task, user, "candidate")
    assert isinstance(done, CommitRefused), done
    assert "reference-transaction hook" in done.reason
    assert _git(user, "rev-parse", "HEAD") == head
    assert not list(manager.workspaces_root.glob(".commit-index-*"))


def _linked_worktree(tmp_path: Path) -> Path:
    main = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "feature", str(linked))
    _git(linked, "config", "user.email", "t@example.invalid")
    _git(linked, "config", "user.name", "t")
    return linked


def test_a_linked_worktree_publishes_on_its_own_branch(tmp_path):
    user = _linked_worktree(tmp_path)
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    parent = _git(user, "rev-parse", "HEAD").strip()
    done = manager.commit_applied(task, user, "candidate")
    assert isinstance(done, Committed), done
    assert done.branch == "feature"
    assert _git(user, "rev-parse", "refs/heads/feature~1").strip() == parent
    assert _git(user, "rev-parse", "HEAD").strip() == done.commit
    # The main worktree's checked-out branch is another ref and is not moved.
    assert _git(tmp_path / "user", "rev-parse", "HEAD").strip() == parent


@pytest.mark.parametrize("move", ["switch", "detach"])
def test_a_branch_change_in_a_linked_worktree_moves_no_ref(tmp_path, monkeypatch, move):
    user = _linked_worktree(tmp_path)
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    parent = _git(user, "rev-parse", "HEAD").strip()
    real_git = manager._git
    moved: list[str] = []

    def change_branch(cwd, *args, **kwargs):
        if _is("update-ref", args) and not moved:
            _git(user, "switch", "-q", *(["-c", "elsewhere"] if move == "switch"
                                         else ["--detach"]))
            moved.append(move)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", change_branch)
    done = manager.commit_applied(task, user, "candidate")
    assert moved
    assert isinstance(done, CommitRefused), done
    assert _git(user, "rev-parse", "refs/heads/feature").strip() == parent
    assert _git(user, "rev-parse", "HEAD").strip() == parent


_MESSAGE = "\n  subject  \n# a comment line\n\n\n body text \n------------------------ >8 ------------------------\nbelow scissors\n\n"


def _git_commit_message(tmp_path: Path, cleanup: str | None) -> str:
    """What plain ``git commit -F -`` records for ``_MESSAGE`` under ``cleanup``."""
    control = tmp_path / f"control-{cleanup}"
    control.mkdir()
    _git(control, "init", "-q")
    config = [] if cleanup is None else ["-c", f"commit.cleanup={cleanup}"]
    subprocess.run(  # noqa: S603, S607 - fixed git argv
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *config,
         "commit", "-q", "--allow-empty", "-F", "-"],
        cwd=control, input=_MESSAGE, text=True, check=True, capture_output=True,
    )
    return _git(control, "cat-file", "commit", "HEAD").split("\n\n", 1)[1]


@pytest.mark.parametrize("cleanup", [None, "default", "strip", "whitespace", "verbatim",
                                     "scissors"])
def test_the_recorded_message_matches_git_commit_for_every_accepted_cleanup(
    tmp_path, cleanup,
):
    """Restored and widened (Astra, 1e924e5): commit-tree stores its input verbatim, so
    each accepted commit.cleanup mode must record exactly what ``git commit -F`` does."""
    (tmp_path / "lca").mkdir()
    user = _repo(tmp_path / "lca", {"a.txt": "old a\n"})
    manager, task = _apply(tmp_path / "lca", user, {"a.txt": "candidate\n"})
    if cleanup is not None:
        _git(user, "config", "commit.cleanup", cleanup)
    done = manager.commit_applied(task, user, _MESSAGE)
    assert isinstance(done, Committed), done
    recorded = _git(user, "cat-file", "commit", "HEAD").split("\n\n", 1)[1]
    assert recorded == _git_commit_message(tmp_path, cleanup)


def test_an_empty_message_is_refused_before_anything_is_created(tmp_path):
    user = _repo(tmp_path, {"a.txt": "old a\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    before = _state_one(user)
    refused = manager.commit_applied(task, user, " \n\n \n")
    assert isinstance(refused, CommitRefused), refused
    assert _state_one(user) == before


def _state_one(user: Path) -> tuple[str, ...]:
    return (_git(user, "rev-parse", "HEAD"), _git(user, "ls-files", "--stage"),
            _git(user, "status", "--porcelain=v1", "--untracked-files=all"))


_PAUSING_HOOK = '''
import importlib.util, pathlib, sys, time
control = pathlib.Path({control!r})
spec = importlib.util.spec_from_file_location("lca_hook_under_test", {real!r})
hook = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hook          # dataclasses resolve their module by name
spec.loader.exec_module(hook)
transaction, state = sys.argv[1], sys.argv[2]
updates = sys.stdin.read()
if state == "prepared" and "refs/heads/" in updates:
    (control / "paused").write_text(updates, encoding="utf-8")
    deadline = time.monotonic() + 60
    while not (control / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
raise SystemExit(hook.main([transaction, state], updates=updates))
'''


@pytest.mark.parametrize("move", ["switch", "detach"])
def test_no_branch_change_can_cross_a_live_prepared_publication(tmp_path, monkeypatch, move):
    """Astra (1e924e5): pause the real publication inside its prepared hook, with Git's
    locks held, and try to switch or detach. Git must refuse on HEAD's lock; once
    released, exactly the intended branch moves and HEAD still names it."""
    import threading

    import local_agent.session.workspaces as workspaces_module

    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    branch = _git(user, "symbolic-ref", "HEAD").strip()
    parent = _git(user, "rev-parse", "HEAD").strip()
    refs_before = set(_git(user, "for-each-ref").splitlines())
    control = tmp_path / "control"
    control.mkdir()
    pausing = tmp_path / "pausing_hook.py"
    pausing.write_text(_PAUSING_HOOK.format(control=str(control),
                                            real=commit_index_hook.__file__),
                       encoding="utf-8")
    real_environment = workspaces_module.hook_environment

    def pausing_environment(transaction: Path) -> dict[str, str]:
        env = real_environment(transaction)
        env["LCA_COMMIT_INDEX_HOOK"] = str(pausing).replace("\\", "/")
        return env

    monkeypatch.setattr(workspaces_module, "hook_environment", pausing_environment)
    outcome: list[object] = []
    worker = threading.Thread(
        target=lambda: outcome.append(manager.commit_applied(task, user, "candidate")))
    worker.start()
    try:
        deadline = time.monotonic() + 60
        while not (control / "paused").exists():
            assert time.monotonic() < deadline, "publication never reached prepared"
            assert worker.is_alive(), outcome
            time.sleep(0.02)
        crossing = subprocess.run(  # noqa: S603, S607 - fixed git argv
            ["git", "switch", "-q", *(["-c", "elsewhere"] if move == "switch"
                                      else ["--detach"])],
            cwd=user, capture_output=True, text=True, check=False,
        )
        assert crossing.returncode != 0, "a branch change crossed the live publication"
        # files backend: HEAD.lock exists; reftable: the whole stack is locked.
        assert "lock" in crossing.stderr, crossing.stderr
    finally:
        (control / "release").write_text("go", encoding="utf-8")
        worker.join(60)
    assert not worker.is_alive()
    done = outcome[0]
    assert isinstance(done, Committed), done
    assert _git(user, "symbolic-ref", "HEAD").strip() == branch
    assert _git(user, "rev-parse", branch).strip() == done.commit
    assert _git(user, "rev-parse", f"{done.commit}~1").strip() == parent
    expected = {
        line if not line.endswith(f"\t{branch}") else f"{done.commit} commit\t{branch}"
        for line in refs_before
    }
    refs_after = set(_git(user, "for-each-ref").splitlines())
    if move == "switch":
        # On the files backend the user's own `switch -c` creates its branch before it
        # fails on HEAD.lock; that ref is the user's, at the parent, not LCA's.
        refs_after.discard(f"{parent} commit\trefs/heads/elsewhere")
    assert refs_after == expected
