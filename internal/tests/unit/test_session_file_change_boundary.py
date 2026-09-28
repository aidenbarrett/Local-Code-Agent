from types import SimpleNamespace

import pytest

from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import RouteAction, decide_route


class NoModel:
    def chat(self, *args, **kwargs):
        raise AssertionError("This request must not reach the conversation model")


def gateway(root):
    calls = []

    def run(task, **kwargs):
        calls.append((task, kwargs))
        return TaskResult("task", TaskOutcome.FAIL, "candidate attempted", False)

    controller = SimpleNamespace(repo=SimpleNamespace(root=root), run=run)
    return ConversationGateway(NoModel(), controller, EventBuffer("s")), calls


@pytest.mark.parametrize("text", [
    "update me on the build", "update everything", "add a header file for the widget",
    "create a playlist", "edit my notes", "add the file to the list",
    "create a file", "update the documentation", "create an example about src/foo.cpp",
])
def test_prose_is_not_a_deterministic_file_change(text):
    assert decide_route(text, active_repo_count=1).action == RouteAction.MODEL_FALLBACK


@pytest.mark.parametrize("text", [
    "add the file to git", "edit my commit message", "update everything and push it",
    "update the file and commit it", "update the submodules", "update git",
    "create a file called x.cpp and push it", "edit x.cpp then delete y.cpp",
    "update x.cpp and remove old.cpp", "add x.cpp and git add it",
    "change: update x.cpp and commit it", "change: add x.cpp then push it",
])
def test_unsupported_clause_wins_without_partial_work(text, tmp_path):
    app, calls = gateway(tmp_path)
    assert decide_route(text, active_repo_count=1).action == RouteAction.REFUSE
    assert "No task was run" in app.turn(text)
    assert calls == []


@pytest.mark.parametrize("text", [
    "create a file called ../../etc/x",
    r"create a file called ..\..\etc\x",
    r"Can you create a C++ file called aiden101.cpp and add a print inside it? Save this file here: C:\Users\aidenbar\Downloads\\",
    r'create a file called x.cpp. Save here: "C:\Users\Aiden Barrett\Downloads\"',
    r"edit C:\Users\aidenbar\Downloads\x.cpp",
    r"create a file called \\server\share\x.cpp",
    "change: add ../../outside.cpp",
    "create a file called ~/Downloads/x.cpp",
    r"create a file called C:outside.cpp",
])
def test_external_paths_are_refused_before_model_or_task(text, tmp_path):
    app, calls = gateway(tmp_path)
    answer = app.turn(text)
    assert "outside path" in answer
    assert str(tmp_path) in answer
    assert "No task was run" in answer
    assert calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("text", [
    "create a file called src/x.cpp", "create aiden101.cpp",
    "please add a header file named src/widget.h", "edit README.md to clarify setup",
    'create a file called "src/my file.cpp"', "modify the file .gitignore",
    "update src/git.cpp", "create src/push.cpp", "create src/commit.cpp",
    "edit src/remove.cpp", "change: remove old.cpp",
])
def test_contained_targets_keep_the_original_request(text, tmp_path):
    app, calls = gateway(tmp_path)
    assert "candidate attempted" in app.turn(text)
    assert len(calls) == 1
    assert text in calls[0][0]


def test_absolute_contained_path_and_sibling_prefix(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    app, calls = gateway(root)
    assert "candidate attempted" in app.turn(f"create {root / 'src/x.cpp'}")
    assert "outside path" in app.turn(f"create {tmp_path / 'repo-other/x.cpp'}")
    assert len(calls) == 1


def test_symlink_escape_is_refused(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    try:
        (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"host cannot create symlinks: {exc}")
    app, calls = gateway(root)
    assert "outside path" in app.turn("create escape/outside.cpp")
    assert calls == []


def test_explicit_work_cannot_bypass_destination_check(tmp_path):
    app, calls = gateway(tmp_path)
    assert "outside path" in app.turn("create ../../outside.cpp", explicit_mode="work")
    assert calls == []


def test_missing_root_cannot_grant_file_change_authority():
    app, calls = gateway(None)
    assert "cannot establish the active repository" in app.turn("create x.cpp")
    assert calls == []
