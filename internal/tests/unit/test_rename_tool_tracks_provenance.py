"""The rename pre-flight's idea of the contract axes must match provenance's.

`check_rename_safety.py` declares the axis inputs in its own constants:
`OUTCOME_WHOLE_FILES`, `OUTCOME_FUNCTIONS`, `OUTCOME_EVALUATOR`, `PROMPT_MODULE`,
`PROMPT_MEMBERS`, `PROMPT_CLASS_MODULE`, `PROMPT_CLASS`, `PROMPT_METHODS`. Restating
them is what makes the tool readable and independently reviewable, and it is also a
staleness trap: if `provenance.py` starts hashing a sixth evaluator function and the
tool is not updated, the tool inspects yesterday's contract and announces today's rename
clean. Fail-closed against the wrong contract is not fail-closed.

Two halves, because the two axes are built differently.

The outcome axis resolves its inputs by path, so it can be tested **behaviourally**
against a synthetic tree: mutate each declared input and the hash must move; mutate
something not declared and it must not. That proves the tool's list is exactly the
implementation's, not merely a subset of it.

The base-prompt axis resolves its inputs by package import and `inspect.getsource`, so
redirecting it at a synthetic tree is not possible. It is checked **structurally**
instead, against the semantic labels provenance hashes, which are the same strings PR #60
froze.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INTERNAL = ROOT / "internal"
TOOL = INTERNAL / "devtools" / "check_rename_safety.py"
sys.path.insert(0, str(INTERNAL))

from local_agent import provenance  # noqa: E402


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("check_rename_safety", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# Structural: the labels provenance hashes enumerate what the tool declares
# --------------------------------------------------------------------------

def _function_ast(name: str) -> ast.FunctionDef:
    tree = ast.parse((INTERNAL / "local_agent" / "provenance.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, f"expected exactly one {name} in provenance.py, got {len(found)}"
    return found[0]


def _string_constants(node: ast.AST) -> set[str]:
    return {
        child.value for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _fstring_suffix_names(node: ast.AST, prefix: str) -> set[str]:
    """Names hashed under an f-string label such as f"evaluation.run_evaluation.{name}"."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.JoinedStr):
            literal = "".join(
                part.value for part in child.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str))
            if literal.startswith(prefix):
                names.add(literal)
    return names


def test_the_tool_declares_the_same_outcome_whole_files_as_provenance(tool):
    declared = {Path(p).name for p in tool.OUTCOME_WHOLE_FILES}
    hashed = {
        Path(value).name for value in _string_constants(_function_ast("outcome_contract_sha256"))
        if value.endswith(".py")
    }
    hashed.discard(Path(tool.OUTCOME_EVALUATOR).name)
    assert declared == hashed, (
        "the pre-flight tool and provenance.outcome_contract_sha256 disagree about which "
        f"files are whole-file inputs.\n  tool:       {sorted(declared)}\n"
        f"  provenance: {sorted(hashed)}\n"
        "Update internal/devtools/check_rename_safety.py in the same commit as the "
        "provenance change, or the tool audits a contract that no longer exists.")


def test_the_tool_declares_the_same_evaluator_as_provenance(tool):
    literals = _string_constants(_function_ast("outcome_contract_sha256"))
    assert Path(tool.OUTCOME_EVALUATOR).name in literals, (
        f"provenance no longer names {tool.OUTCOME_EVALUATOR} as the evaluator whose "
        "function bodies are hashed.")


def test_provenance_labels_the_evaluator_functions_the_tool_expects(tool):
    """The `evaluation.run_evaluation.<name>` label is the axis's own naming of them."""
    prefix = "evaluation.run_evaluation."
    labels = _fstring_suffix_names(_function_ast("outcome_contract_sha256"), prefix)
    assert labels == {prefix}, (
        "provenance no longer labels the selected evaluator functions with "
        f"{prefix!r}; the tool's OUTCOME_FUNCTIONS list is keyed to that shape.")


def test_the_tool_declares_the_same_base_prompt_members_as_provenance(tool):
    labels = _string_constants(_function_ast("base_prompt_sha256"))
    context_members = {
        label.split(".", 1)[1] for label in labels if label.startswith("context.")
    }
    contract_methods = {
        label.split(".", 1)[1] for label in labels if label.startswith("contracts.")}

    assert context_members == set(tool.PROMPT_MEMBERS), (
        "the pre-flight tool and provenance.base_prompt_sha256 disagree about which "
        f"context members are hashed.\n  tool:       {sorted(tool.PROMPT_MEMBERS)}\n"
        f"  provenance: {sorted(context_members)}")
    assert contract_methods == set(tool.PROMPT_METHODS), (
        "the pre-flight tool and provenance.base_prompt_sha256 disagree about which "
        f"methods are hashed.\n  tool:       {sorted(tool.PROMPT_METHODS)}\n"
        f"  provenance: {sorted(contract_methods)}")


def test_the_tool_declares_the_same_base_prompt_modules_as_provenance(tool):
    """The two modules are reached by import, so compare the import statements."""
    node = _function_ast("base_prompt_sha256")
    imported = {
        (n.module, tuple(a.name for a in n.names))
        for n in ast.walk(node) if isinstance(n, ast.ImportFrom)
    }
    modules = {module for module, _ in imported if module}
    assert ".agent".lstrip(".") in {m.lstrip(".") for m in modules} or "agent" in modules, (
        f"provenance no longer imports the agent package for the prompt axis: {modules}")

    assert Path(tool.PROMPT_MODULE).stem in {
        name for _, names in imported for name in names} | {
        part for module in modules for part in module.split(".")}, (
        f"provenance no longer hashes {tool.PROMPT_MODULE}")

    assert any("contracts" in (module or "") for module in modules), (
        f"provenance no longer hashes members of {tool.PROMPT_CLASS_MODULE}")
    assert tool.PROMPT_CLASS in {name for _, names in imported for name in names}, (
        f"provenance no longer hashes members of the class {tool.PROMPT_CLASS!r}; the "
        "tool would look for a class that is not the one being hashed.")


# --------------------------------------------------------------------------
# Behavioural: mutate each declared input, and something not declared
# --------------------------------------------------------------------------

_EVALUATOR = '''
def prepare(workdir, scenario):
    return workdir


def establish(x):
    return x


def _error_row(x):
    return {"error": x}


def run_case(case):
    return case


def run_all(cases):
    return [run_case(c) for c in cases]


def not_a_declared_input(x):
    return x
'''


@pytest.fixture
def synthetic_root(tmp_path, monkeypatch):
    """A tree the outcome axis can be pointed at, with provenance's real code."""
    evaluation = tmp_path / "internal" / "evaluation"
    evaluation.mkdir(parents=True)
    (evaluation / "task_contracts.py").write_text("CASES = ()\n")
    (evaluation / "oracle.py").write_text("def snapshot(p):\n    return p\n")
    (evaluation / "endpoints.py").write_text("SUCCESS = frozenset({'pass'})\n")
    (evaluation / "run_evaluation.py").write_text(_EVALUATOR)
    (evaluation / "not_an_axis_input.py").write_text("HELPER = 1\n")
    monkeypatch.setattr(provenance, "_ROOT", tmp_path)
    return tmp_path


def _outcome(root: Path) -> str:
    value = provenance.outcome_contract_sha256()
    assert value != "unavailable-no-source", (
        "the synthetic tree does not satisfy provenance's declared inputs, so this test "
        "is not measuring anything. It needs updating alongside the axis.")
    return value


def test_every_declared_whole_file_input_actually_moves_the_outcome_axis(tool, synthetic_root):
    for relative in tool.OUTCOME_WHOLE_FILES:
        path = synthetic_root / relative
        before = _outcome(synthetic_root)
        original = path.read_text()
        path.write_text(original + "\n# mutated\n")
        after = _outcome(synthetic_root)
        path.write_text(original)
        assert after != before, (
            f"{relative} is declared a whole-file outcome input by the pre-flight tool, "
            "and editing it does not move outcome_contract_sha256. The tool is auditing "
            "a file the axis no longer covers.")


def _with_mutated_body(source: str, name: str) -> str:
    """Insert a statement at the top of one function's body, leaving its signature alone.

    The signature is deliberately untouched: `_function_source` selects by name, so
    editing the `def` line would prove only that renaming a function moves the hash,
    which is a different claim.
    """
    lines = source.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith(f"def {name}("):
            return "".join(lines[:index + 1] + ["    _mutation = 1\n"] + lines[index + 1:])
    raise AssertionError(f"def {name}( not found in the synthetic evaluator")


def test_every_declared_evaluator_function_actually_moves_the_outcome_axis(tool, synthetic_root):
    evaluator = synthetic_root / tool.OUTCOME_EVALUATOR
    original = evaluator.read_text()
    before = _outcome(synthetic_root)
    for name in tool.OUTCOME_FUNCTIONS:
        evaluator.write_text(_with_mutated_body(original, name))
        after = _outcome(synthetic_root)
        evaluator.write_text(original)
        assert after != before, (
            f"{name} is declared a hashed evaluator function by the pre-flight tool, and "
            "editing its body does not move outcome_contract_sha256. The tool is auditing "
            "a function the axis no longer covers.")


def test_something_not_declared_does_not_move_the_outcome_axis(tool, synthetic_root):
    """The tool's list must be exactly the axis's, not a subset of it.

    A tool that declared four of five functions would pass the test above and still
    clear a rename that breaks the fifth.
    """
    evaluator = synthetic_root / tool.OUTCOME_EVALUATOR
    before = _outcome(synthetic_root)

    original = evaluator.read_text()
    evaluator.write_text(original.replace(
        "def not_a_declared_input(x):\n    return x\n",
        "def not_a_declared_input(x):\n    _mutation = 1\n    return x\n"))
    assert _outcome(synthetic_root) == before, (
        "editing a function the tool does not declare moved the outcome axis, so "
        "provenance hashes more of the evaluator than OUTCOME_FUNCTIONS lists.")
    evaluator.write_text(original)

    sibling = synthetic_root / "internal" / "evaluation" / "not_an_axis_input.py"
    sibling.write_text("HELPER = 2\n")
    assert _outcome(synthetic_root) == before, (
        "editing an evaluation file the tool does not declare moved the outcome axis, so "
        "provenance covers more whole files than OUTCOME_WHOLE_FILES lists.")
