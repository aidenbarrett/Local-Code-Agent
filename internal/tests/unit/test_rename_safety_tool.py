"""Permanent regressions for the rename pre-flight tool.

The tool's whole value is that its answer is believed. It was believed twice while
wrong: once because it counted prose and local bindings as dependencies, and once
because `ast.walk` let a binding inside a nested function shadow a reference in the
enclosing one. Scratch experiments found both. Scratch experiments do not run again.

So every claim the tool makes is pinned here: the two exit codes that make it
fail-closed, the shadowing rules in both directions, the import-only case that turns a
blocker into a preserved binding, and the completeness checks that stop it reporting a
clean result from an inspection that silently covered fewer inputs than the hash does.

The exit-code cases run the tool as a subprocess against a synthetic repository, not
against this one. A fresh interpreter is the only honest way to assert an exit code, and
a purpose-built tree means each case mutates exactly one thing.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "internal" / "devtools" / "check_rename_safety.py"

EXIT_CLEAN = 0
EXIT_HARD_BLOCKER = 2
EXIT_INCONCLUSIVE = 3


def _load_tool():
    spec = importlib.util.spec_from_file_location("check_rename_safety", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tool():
    """A fresh import per test: the module accumulates UNANALYSABLE at module level."""
    return _load_tool()


def test_the_tool_exists_where_the_documentation_says_it_does():
    assert TOOL.is_file(), (
        f"{TOOL.relative_to(ROOT)} is missing. AGENTS.md and internal/README.md both "
        "tell a contributor to run it before any rename.")


# --------------------------------------------------------------------------
# Scope analysis. Each case is a rename that either does or does not force an edit.
# --------------------------------------------------------------------------

def test_a_binding_in_a_nested_function_does_not_shadow_the_enclosing_scope(tool):
    """The false negative that shipped. This is the case that matters most.

    `oracle` is a real dependency of `outer`. The assignment inside `inner` binds
    inner's local and shadows nothing outside it. Reporting no free reference here
    would clear a rename that breaks a hashed function body.
    """
    source = (
        "def outer():\n"
        "    print(oracle)\n"
        "    def inner():\n"
        "        oracle = 1\n"
    )
    assert tool.free_references(source, "oracle") == ["in outer"]


def test_a_local_in_the_same_scope_does_shadow(tool):
    source = "def outer():\n    oracle = 1\n    print(oracle)\n"
    assert tool.free_references(source, "oracle") == []


def test_a_parameter_shadows(tool):
    """`def _files_under(base: Path, ...)` is why `tools/base.py` was not blocked."""
    source = "def _files_under(base, pattern):\n    return base.glob(pattern)\n"
    assert tool.free_references(source, "base") == []


def test_a_comprehension_target_does_not_shadow_the_enclosing_scope(tool):
    source = "def outer():\n    xs = [oracle for oracle in range(3)]\n    print(oracle)\n"
    assert tool.free_references(source, "oracle") == ["in outer"]


def test_a_lambda_parameter_does_not_shadow_the_enclosing_scope(tool):
    source = "def outer():\n    f = lambda oracle: oracle\n    print(oracle)\n"
    assert tool.free_references(source, "oracle") == ["in outer"]


def test_a_class_body_assignment_does_not_shadow_the_enclosing_scope(tool):
    source = "def outer():\n    class C:\n        oracle = 1\n    print(oracle)\n"
    assert tool.free_references(source, "oracle") == ["in outer"]


def test_a_global_declaration_is_a_dependency_not_a_shadow(tool):
    """`global oracle; oracle = 1` writes the module-level name, it does not hide it."""
    source = "def outer():\n    global oracle\n    oracle = 1\n    print(oracle)\n"
    assert tool.free_references(source, "oracle") == ["in outer"]


def test_an_import_inside_a_scope_shadows(tool):
    source = "def outer():\n    import oracle\n    print(oracle)\n"
    assert tool.free_references(source, "oracle") == []


def test_a_word_in_a_comment_or_string_is_not_a_reference(tool):
    source = (
        'def outer():\n'
        '    """Mentions oracle in prose."""\n'
        '    # oracle again\n'
        '    return "oracle"\n'
    )
    assert tool.free_references(source, "oracle") == []


def test_an_else_branch_binding_is_not_lost(tool):
    """`ast.If` also has a `.body`; treating it as a scope would drop `orelse`."""
    source = "def outer():\n    if x:\n        pass\n    else:\n        oracle = 1\n    print(oracle)\n"
    assert tool.free_references(source, "oracle") == []


def test_an_unparsable_fragment_is_recorded_rather_than_ignored(tool):
    assert tool.free_references("def broken(:\n", "oracle", where="fake.py") == []
    assert any("fake.py" in note for note in tool.UNANALYSABLE)


# --------------------------------------------------------------------------
# End-to-end exit codes, against a synthetic repository
# --------------------------------------------------------------------------

_PROVENANCE = '''
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HASHED = (("internal/evaluation", "*.py"), ("internal/local_agent", "*.py"))
_HASHED_FILES = ()
_SKIP = {"__pycache__"}


def _key(path):
    return path.relative_to(_ROOT).as_posix()


def _files():
    found = []
    for folder, pattern in _HASHED:
        base = _ROOT / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob(pattern)):
            if path.is_file() and not _SKIP.intersection(path.parts):
                found.append(path)
    return found
'''

_EVALUATOR = '''
import oracle


def prepare(workdir, scenario):
    return oracle.snapshot(workdir)


def establish(x):
    return x


def _error_row(x):
    return {"error": x}


def run_case(case):
    return oracle.verify(case)


def run_all(cases):
    return [run_case(c) for c in cases]
'''

_CONTRACTS = '''
class Orchestrator:
    def _execute(self, call):
        return call

    def _accept_answer(self, answer):
        return answer
'''

_CONTEXT = '''
def build_system_message(x):
    return x


def build_skill_message(x):
    return x


def tool_result_message(x):
    return x
'''


@pytest.fixture
def fake_repo(tmp_path):
    """A minimal tree with every input the tool declares, and nothing else.

    Writing it out is also a statement of what the tool requires: if a future change
    adds a declared axis input, this fixture stops satisfying it and these tests fail,
    which is the correct direction for that failure.
    """
    root = tmp_path / "repo"
    (root / "internal" / "local_agent" / "agent").mkdir(parents=True)
    (root / "internal" / "evaluation").mkdir(parents=True)

    (root / "internal" / "local_agent" / "__init__.py").write_text("")
    (root / "internal" / "local_agent" / "provenance.py").write_text(_PROVENANCE)
    (root / "internal" / "local_agent" / "agent" / "__init__.py").write_text("")
    (root / "internal" / "local_agent" / "agent" / "context.py").write_text(_CONTEXT)
    (root / "internal" / "local_agent" / "agent" / "contracts.py").write_text(_CONTRACTS)

    evaluation = root / "internal" / "evaluation"
    (evaluation / "task_contracts.py").write_text("WIDGET_CASES = ()\n")
    (evaluation / "oracle.py").write_text("def snapshot(p):\n    return p\n")
    (evaluation / "endpoints.py").write_text("SUCCESS = frozenset({'pass'})\n")
    (evaluation / "run_evaluation.py").write_text(_EVALUATOR)
    return root


def _run(repo: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--repo", str(repo), *arguments],
        capture_output=True, text=True, timeout=120)


def test_a_clean_tree_exits_zero(fake_repo):
    result = _run(fake_repo, "--probe", "widget")
    assert result.returncode == EXIT_CLEAN, result.stdout + result.stderr


def test_an_expression_use_inside_a_hashed_whole_file_is_a_hard_blocker(fake_repo):
    """Nothing preserves this. The rename would end the generation."""
    endpoints = fake_repo / "internal" / "evaluation" / "endpoints.py"
    endpoints.write_text(endpoints.read_text() + "\n_probe = widget.SETTING\n")
    result = _run(fake_repo, "--probe", "widget")
    assert result.returncode == EXIT_HARD_BLOCKER, result.stdout + result.stderr
    assert "HARD BLOCKER" in result.stdout


def test_an_import_only_reference_is_conditional_with_a_named_remedy(fake_repo):
    """A hashed file that only imports the thing can be left alone behind a re-export."""
    contracts = fake_repo / "internal" / "evaluation" / "task_contracts.py"
    contracts.write_text("from local_agent.agent.widget import Thing\n" + contracts.read_text())
    result = _run(fake_repo, "--probe", "widget")
    assert result.returncode == EXIT_CLEAN, result.stdout + result.stderr
    assert "CONDITIONAL" in result.stdout
    assert "re-export" in result.stdout


def test_an_unparsable_axis_input_is_inconclusive(fake_repo):
    (fake_repo / "internal" / "evaluation" / "oracle.py").write_text("def broken(:\n")
    result = _run(fake_repo, "--probe", "widget")
    assert result.returncode == EXIT_INCONCLUSIVE, result.stdout + result.stderr
    assert "INCONCLUSIVE" in result.stdout


def test_a_missing_axis_input_is_inconclusive(fake_repo):
    (fake_repo / "internal" / "evaluation" / "endpoints.py").unlink()
    result = _run(fake_repo, "--probe", "widget")
    assert result.returncode == EXIT_INCONCLUSIVE, result.stdout + result.stderr
    assert "missing" in result.stdout


def test_a_missing_hashed_function_is_inconclusive(fake_repo):
    """The axis covers five named function bodies. Four is not four fifths of an answer."""
    evaluator = fake_repo / "internal" / "evaluation" / "run_evaluation.py"
    evaluator.write_text(_EVALUATOR.replace("def run_all(cases):", "def run_every(cases):"))
    result = _run(fake_repo, "--probe", "widget")
    assert result.returncode == EXIT_INCONCLUSIVE, result.stdout + result.stderr
    assert "run_all" in result.stdout


def test_a_missing_hashed_method_is_inconclusive(fake_repo):
    contracts = fake_repo / "internal" / "local_agent" / "agent" / "contracts.py"
    contracts.write_text(_CONTRACTS.replace("def _accept_answer", "def _accept"))
    result = _run(fake_repo, "--probe", "widget")
    assert result.returncode == EXIT_INCONCLUSIVE, result.stdout + result.stderr
    assert "_accept_answer" in result.stdout


def test_a_missing_hashed_class_is_inconclusive(fake_repo):
    contracts = fake_repo / "internal" / "local_agent" / "agent" / "contracts.py"
    contracts.write_text(_CONTRACTS.replace("class Orchestrator:", "class TaskWorker:"))
    result = _run(fake_repo, "--probe", "widget")
    assert result.returncode == EXIT_INCONCLUSIVE, result.stdout + result.stderr


def test_a_duplicated_hashed_function_is_inconclusive(fake_repo):
    """Two definitions of the same name: the hash takes one, the audit read the other."""
    evaluator = fake_repo / "internal" / "evaluation" / "run_evaluation.py"
    evaluator.write_text(_EVALUATOR + "\n\ndef run_all(cases):\n    return []\n")
    result = _run(fake_repo, "--probe", "widget")
    assert result.returncode == EXIT_INCONCLUSIVE, result.stdout + result.stderr
    assert "more than once" in result.stdout


def test_markdown_mode_fails_closed_too(fake_repo):
    """A table pasted into a PR body must not be a way to launder a blocker."""
    endpoints = fake_repo / "internal" / "evaluation" / "endpoints.py"
    endpoints.write_text(endpoints.read_text() + "\n_probe = widget.SETTING\n")
    result = _run(fake_repo, "--markdown", "--probe", "widget")
    assert result.returncode == EXIT_HARD_BLOCKER, result.stdout + result.stderr


def test_the_table_reports_a_file_that_is_itself_an_axis_input(fake_repo):
    """`endpoints.py` contains no reference to the word `endpoints`.

    The occurrence columns therefore say no, and an earlier version of this table read
    as "endpoints.py is outside the outcome contract". Its bytes are the outcome
    contract. The two questions get two columns.
    """
    result = _run(fake_repo, "--markdown", "--probe",
                  "endpoints=internal/evaluation/endpoints.py")
    assert result.returncode == EXIT_CLEAN, result.stdout + result.stderr
    row = next(line for line in result.stdout.splitlines() if "`endpoints`" in line)
    assert "outcome_contract: whole file" in row, row
