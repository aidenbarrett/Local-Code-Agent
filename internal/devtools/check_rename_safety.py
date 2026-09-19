#!/usr/bin/env python3
"""Read-only pre-flight audit for a rename or move. Answers §5 of the refactor plan.

For each identifier or path, report whether renaming it would move a frozen
contract axis, whether it is a wire constant, whether anything imports it by bare
name, and which configuration or scripts name its path.

The distinction that matters, and the one a grep cannot make: an identifier
appearing as a **code reference** inside a hashed input forces that input to be
edited, which moves the hash. The same word in a comment, a docstring, or bound as
a local variable forces nothing. Two renames in this project were mis-assessed in
both directions by treating those as equivalent.

WHAT A CLEAN RUN DOES NOT MEAN
------------------------------
A zero exit says one thing only: **no contract-axis obstacle was detected**. It is
not a statement that the refactor is valid, and it must never be quoted as one.
Specifically, it does not prove:

* that the tree still imports. Measured case: moving `agent/orchestrator.py` to
  `worker/worker_loop.py` behind a re-export left `outcome_contract_sha256`
  byte-identical while the package raised ImportError, because that module
  relative-imports its siblings. This tool reads bytes and never imports anything.
* that behaviour is unchanged. It does not run a single test.
* that non-Python references were updated. Markdown, PowerShell, YAML and JSON are
  reported through substring matching, which both over- and under-reports. The wire
  guard in `test_wire_constants_are_frozen.py` is the real protection there, and it
  exists because that class of escape has already happened twice.

It is fail-closed: non-zero on a detected hard blocker, and non-zero on any input it
could not analyse, so an unparsable file can never read as a pass.

Usage, from the repository root, Windows PowerShell or WSL alike:
    python internal/devtools/check_rename_safety.py
    python internal/devtools/check_rename_safety.py --markdown > preflight.md
    python internal/devtools/check_rename_safety.py oracle Orchestrator

Exit codes:
    0  no axis obstacle detected
    2  a hard blocker: a hashed input uses the name in an expression
    3  an input could not be analysed, so the answer is unknown
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path


# --------------------------------------------------------------------------
# What the instrument covers, read from the implementation rather than restated
# --------------------------------------------------------------------------

# Anything an analysis could not read. Non-empty means the run is inconclusive, and
# an inconclusive run exits non-zero: a file this tool cannot parse is the one most
# likely to be hiding the reference that ends a generation.
UNANALYSABLE: list[str] = []


def load_provenance(repo: Path):
    internal = repo / "internal"
    if str(internal) not in sys.path:
        sys.path.insert(0, str(internal))
    from local_agent import provenance  # noqa: E402

    return provenance


# The two generation axes, as the implementation defines their inputs.
OUTCOME_WHOLE_FILES = (
    "internal/evaluation/task_contracts.py",
    "internal/evaluation/oracle.py",
    "internal/evaluation/endpoints.py",
)
OUTCOME_FUNCTIONS = ("prepare", "establish", "_error_row", "run_case", "run_all")
OUTCOME_EVALUATOR = "internal/evaluation/run_evaluation.py"

PROMPT_MODULE = "internal/local_agent/agent/context.py"
PROMPT_MEMBERS = ("build_system_message", "build_skill_message", "tool_result_message")
PROMPT_CLASS_MODULE = "internal/local_agent/agent/contracts.py"
PROMPT_CLASS = "Orchestrator"
PROMPT_METHODS = ("_execute", "_accept_answer")

# Frozen by PR #60. A rename must never produce one of these as a side effect.
WIRE_CONSTANTS = {
    "lca.session.events", "urn:lca:session:events:1", "durable",
    "artifact.recorded", "conversation.delta", "endpoint.state_changed",
    "fault.reported", "route.proposed", "route.resolved", "session.opened",
    "task.admitted", "task.cancel_requested", "task.closed", "task.state_changed",
    "task.verdict", "telemetry.policy", "telemetry.sample", "tool.finished",
    "tool.started", "turn.recorded", "watch.run_recorded", "watch.state_changed",
}

CONFIG_FILES = (
    ".local-agent.toml", "pyproject.toml", "internal/INSTRUMENT.json",
    "internal/tests/conftest.py",
)
CONFIG_GLOBS = (".github/workflows/*.yml", "*.ps1", "internal/*.ps1", "demo/*.ps1")


# --------------------------------------------------------------------------
# Code references, as opposed to prose or local bindings
# --------------------------------------------------------------------------

def code_references(source: str, where: str = "<fragment>") -> set[str]:
    """Names used as code: loads, attributes, import targets. Not strings or comments."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        UNANALYSABLE.append(f"{where}: cannot parse ({exc})")
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.update(node.name.split("."))
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.update(node.module.split("."))
    return found


_NESTED_SCOPES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
)


def locally_bound(node: ast.AST) -> set[str]:
    """Names bound in THIS scope. Deliberately does not descend into nested scopes.

    The earlier version used `ast.walk`, which descends into everything, so a binding
    in a nested function was treated as shadowing a reference in the enclosing one:

        def outer():
            print(oracle)        # a real dependency on the module
            def inner():
                oracle = 1       # binds inner's local, shadows nothing out here

    That reported no free reference to `oracle` and would have cleared a rename that
    breaks a hashed function body. A false negative here is the worst output this tool
    can produce, because it is the one that gets believed.

    Where it is imprecise it over-reports. A reference that appears only inside a
    nested function which binds the name itself is still counted against the enclosing
    scope, and a walrus binding inside a comprehension is not counted as a binding at
    all. Both directions of that error produce a spurious dependency, never a missed
    one.
    """
    bound: set[str] = set()
    declared_elsewhere: set[str] = set()

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = node.args
        for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            bound.add(argument.arg)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)

    # A scope node contributes its own body; any other node is scanned as one
    # statement. Checked explicitly rather than by looking for a `.body` attribute,
    # because `ast.If` has one too and treating it as a scope would silently drop its
    # `orelse` branch.
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                         ast.ClassDef, ast.Lambda)):
        body = node.body if isinstance(node.body, list) else [node.body]
    else:
        body = [node]
    stack: list[ast.AST] = list(body)
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.Global, ast.Nonlocal)):
            # `global oracle; oracle = x` writes the module-level name. That is a
            # dependency on it, not a shadow of it.
            declared_elsewhere.update(current.names)
            continue
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(current.name)   # the def/class name itself binds here
            continue                  # its body is a separate scope
        if isinstance(current, (ast.Lambda, ast.ListComp, ast.SetComp,
                                ast.DictComp, ast.GeneratorExp)):
            continue
        if isinstance(current, ast.Name) and isinstance(current.ctx, (ast.Store, ast.Del)):
            bound.add(current.id)
        elif isinstance(current, ast.alias):
            bound.add((current.asname or current.name).split(".")[0])
        stack.extend(ast.iter_child_nodes(current))

    return bound - declared_elsewhere


def free_references(source: str, name: str, where: str = "<file>") -> list[str]:
    """Scopes where `name` is used as code and is NOT bound in that scope.

    A parameter called `base` or a local called `endpoints` shadows a module of the
    same name, so renaming the module forces no edit. Treating those as dependencies
    produces false blockers, and a false blocker is worse than none: it talks you
    out of a rename that was always safe.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        UNANALYSABLE.append(f"{where}: cannot parse ({exc})")
        return []
    hits: list[str] = []
    module_level_bound = {
        n for node in tree.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for n in locally_bound(node)
    }
    module_refs = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        module_refs |= code_references(ast.unparse(node))
    if name in module_refs and name not in (module_level_bound - _import_bound(tree, name)):
        hits.append("module level")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        segment = ast.get_source_segment(source, node) or ast.unparse(node)
        if name in code_references(segment) and name not in locally_bound(node):
            hits.append(f"in {node.name}")
    return hits


def _import_bound(tree: ast.AST, name: str) -> set[str]:
    """An import of the name IS the dependency, not a shadow of it."""
    for node in ast.walk(tree):
        if isinstance(node, ast.alias) and (node.asname or node.name).split(".")[0] == name:
            return {name}
    return set()


def referenced_only_by_import(source: str, name: str) -> bool:
    """True when every module-level use of `name` is an import statement.

    That distinction decides the remedy, and it is measured rather than assumed.
    If a hashed file only *imports* the thing being renamed, its bytes can be left
    alone: leave a re-export at the old import path, or bind the new name to the old
    one with `as`. If the hashed bytes *use* the name in an expression, nothing can
    preserve them and the rename ends the generation.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        UNANALYSABLE.append(f"remedy check: cannot parse ({exc})")
        return False
    import_nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    imports_it = any(name in code_references(ast.unparse(n)) for n in import_nodes)
    if not imports_it:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == name:
            return False
        if isinstance(node, ast.Attribute) and node.attr == name:
            return False
    return True


def bound_by_import_at_module_level(source: str, name: str) -> bool:
    """True when the evaluator binds `name` with an import at module level.

    Only five function bodies of run_evaluation.py are hashed, not the whole file,
    so a module-level `import new_module as old_name` keeps those bodies byte-exact.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        UNANALYSABLE.append(f"evaluator binding check: cannot parse ({exc})")
        return False
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for alias in node.names:
            if (alias.asname or alias.name).split(".")[0] == name:
                return True
    return False


def scoped_bodies(path: Path, names: tuple[str, ...], cls: str | None = None):
    """Yield the named functions, and record it as inconclusive if any is not found.

    The axis hashes a fixed list of members. If one of them is renamed, moved or
    duplicated, this tool would otherwise silently audit fewer inputs than the hash
    actually covers, and report a clean result from an incomplete inspection.
    """
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        UNANALYSABLE.append(f"{path.name}: cannot parse ({exc})")
        return
    body = tree.body
    if cls is not None:
        holders = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls]
        if len(holders) != 1:
            UNANALYSABLE.append(
                f"{path.name}: expected exactly one class {cls!r}, found {len(holders)}")
            return
        body = holders[0].body

    seen: list[str] = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            seen.append(node.name)
            yield node.name, node, ast.get_source_segment(text, node) or ""

    where = f"{path.name}" + (f".{cls}" if cls else "")
    for missing in [n for n in names if n not in seen]:
        UNANALYSABLE.append(
            f"{where}: declared hashed member {missing!r} was not found, so the audit "
            f"inspected fewer inputs than the hash covers")
    for name in {n for n in seen if seen.count(n) > 1}:
        UNANALYSABLE.append(f"{where}: {name!r} is defined more than once")


# --------------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------------

def in_hashed_surface(repo: Path, provenance, rel: str) -> bool:
    keys = {provenance._key(p) for p in provenance._files()}
    return rel in keys


def bare_name_importers(repo: Path, module: str) -> list[str]:
    """Imports that work only because a directory is on sys.path."""
    hits: list[str] = []
    pattern = re.compile(rf"^\s*(?:from\s+{re.escape(module)}\s+import|import\s+{re.escape(module)})\b")
    for path in (repo / "internal").rglob("*.py"):
        if "__pycache__" in path.parts or "experiments" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.match(line):
                hits.append(f"{path.relative_to(repo)}:{number}")
    return hits


def config_references(repo: Path, needle: str) -> list[str]:
    hits: list[str] = []
    candidates = [repo / name for name in CONFIG_FILES]
    for glob in CONFIG_GLOBS:
        candidates.extend(repo.glob(glob))
    for path in candidates:
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if needle in line:
                hits.append(f"{path.relative_to(repo)}:{number}")
    return hits


def axis_membership(current: str) -> list[str]:
    """How this physical file itself feeds an axis, if it does.

    Kept separate from the reference analysis on purpose. `endpoints.py` contains no
    reference to the identifier `endpoints`, so the occurrence columns say "no" for it,
    and an earlier version of this table therefore told a reader that endpoints.py was
    outside the outcome contract. Its bytes ARE the outcome contract. Both facts matter
    and they are different questions:

        is this file an input to the axis?          -> editing it at all moves the hash
        does the identifier occur inside an input?  -> renaming it forces such an edit
    """
    facts: list[str] = []
    if current in OUTCOME_WHOLE_FILES:
        facts.append("outcome_contract: whole file")
    if current == OUTCOME_EVALUATOR:
        facts.append("outcome_contract: " + ", ".join(OUTCOME_FUNCTIONS))
    if current == PROMPT_MODULE:
        facts.append("base_prompt: " + ", ".join(PROMPT_MEMBERS))
    if current == PROMPT_CLASS_MODULE:
        facts.append(f"base_prompt: {PROMPT_CLASS}." + f", {PROMPT_CLASS}.".join(PROMPT_METHODS))
    return facts


def audit(repo: Path, provenance, identifier: str, current: str, proposed: str) -> dict:
    """One row of the pre-flight table."""
    last = identifier.rsplit(".", 1)[-1]
    row = {
        "identifier": identifier, "current": current, "proposed": proposed,
        "source": in_hashed_surface(repo, provenance, current) if current.endswith((".py", ".sh", ".toml")) else None,
        "axis_input": axis_membership(current),
        "outcome": [], "prompt": [], "wire": [], "bare": [], "config": [],
        "remedy": [], "hard": [],
    }

    for rel in OUTCOME_WHOLE_FILES:
        path = repo / rel
        if not path.is_file():
            UNANALYSABLE.append(f"{rel}: declared outcome-contract input is missing")
            continue
        text = path.read_text(encoding="utf-8")
        for scope in free_references(text, last, where=rel):
            site = f"code ref in whole-file {Path(rel).name} ({scope})"
            row["outcome"].append(site)
            if referenced_only_by_import(text, last):
                row["remedy"].append(
                    f"{site}: leave a re-export at the old import path so the hashed "
                    f"file is not edited")
            else:
                row["hard"].append(site)

    evaluator = repo / OUTCOME_EVALUATOR
    if not evaluator.is_file():
        UNANALYSABLE.append(f"{OUTCOME_EVALUATOR}: declared evaluator is missing")
    if evaluator.is_file():
        evaluator_text = evaluator.read_text(encoding="utf-8")
        aliasable = bound_by_import_at_module_level(evaluator_text, last)
        for name, node, segment in scoped_bodies(evaluator, OUTCOME_FUNCTIONS):
            if last in code_references(segment) and last not in locally_bound(node):
                site = f"code ref in run_evaluation.{name}"
                row["outcome"].append(site)
                if aliasable:
                    row["remedy"].append(
                        f"{site}: bind the new name to `{last}` in the module-level "
                        f"import; only the five function bodies are hashed")
                else:
                    row["hard"].append(site)

    prompt = repo / PROMPT_MODULE
    if not prompt.is_file():
        UNANALYSABLE.append(f"{PROMPT_MODULE}: declared base-prompt input is missing")
    if prompt.is_file():
        for name, node, segment in scoped_bodies(prompt, PROMPT_MEMBERS):
            if last in code_references(segment) and last not in locally_bound(node):
                row["prompt"].append(f"code ref in context.{name}")
                row["hard"].append(f"code ref in context.{name}")
    prompt_class = repo / PROMPT_CLASS_MODULE
    if not prompt_class.is_file():
        UNANALYSABLE.append(
            f"{PROMPT_CLASS_MODULE}: declared base-prompt input is missing")
    if prompt_class.is_file():
        try:
            for name, node, segment in scoped_bodies(prompt_class, PROMPT_METHODS, cls=PROMPT_CLASS):
                if last in code_references(segment) and last not in locally_bound(node):
                    row["prompt"].append(f"code ref in {PROMPT_CLASS}.{name}")
                    row["hard"].append(f"code ref in {PROMPT_CLASS}.{name}")
        except StopIteration:
            row["prompt"].append(f"{PROMPT_CLASS} not found; hash will raise")
            row["hard"].append(f"{PROMPT_CLASS} not found; hash will raise")

    row["wire"] = sorted(c for c in WIRE_CONSTANTS if last and last in c)
    row["bare"] = bare_name_importers(repo, last)
    needle = Path(current).name if current.endswith((".py", ".sh")) else current
    row["config"] = config_references(repo, needle)
    return row


# Every rename the plan proposes: identifier, current path, proposed name.
PROPOSALS = [
    # Decided: keep the physical name. The rename is provable but the hidden alias
    # it would need is less readable than the term it replaces. Explained in
    # evaluation/README.md instead. Row kept so the axis dependency stays visible.
    ("oracle",            "internal/evaluation/oracle.py",              "unchanged (decided)"),
    ("endpoints",         "internal/evaluation/endpoints.py",           "success_metrics.py (PR J)"),
    ("task_contracts",    "internal/evaluation/task_contracts.py",      "unchanged"),
    ("run_evaluation",    "internal/evaluation/run_evaluation.py",      "runner.py (PR J)"),
    ("Orchestrator",      "internal/local_agent/agent/contracts.py",    "TaskWorker (PR E)"),
    # Decided: do not move the package. A naive move ends generation 2; a shim
    # preserves the axis but the module relative-imports its siblings, so it is a
    # whole-package migration for directory aesthetics. Row kept as a tripwire.
    ("agent.orchestrator","internal/local_agent/agent/orchestrator.py", "unchanged (decided)"),
    ("contracts",         "internal/local_agent/agent/contracts.py",    "worker/task_worker.py (PR E)"),
    ("Verdict",           "internal/local_agent/agent/policy.py",       "PolicyAction (PR E)"),
    ("Decision",          "internal/local_agent/agent/policy.py",       "PolicyDecision (PR E)"),
    ("Outcome",           "internal/local_agent/agent/outcome.py",      "WorkerOutcome (PR E)"),
    ("context",           "internal/local_agent/agent/context.py",      "prompt_context.py (PR E)"),
    ("models",            "internal/local_agent/llm/models.py",         "inference/protocol.py (PR D)"),
    ("router",            "internal/local_agent/llm/router.py",         "inference/model_tiers.py (PR D)"),
    ("client",            "internal/local_agent/llm/client.py",         "inference/client.py (PR D)"),
    ("base",              "internal/local_agent/tools/base.py",         "tool_types.py etc (PR D)"),
    ("runner",            "internal/local_agent/tools/runner.py",       "process_runner.py (PR D)"),
    ("testing",           "internal/local_agent/tools/testing.py",      "testing_tools.py (PR D)"),
    ("events",            "internal/local_agent/session/events.py",     "event_buffer.py (PR F1)"),
    ("controller",        "internal/local_agent/session/controller.py", "task_controller.py (PR F1)"),
    ("gateway",           "internal/local_agent/session/gateway.py",    "conversation_gateway.py (PR F1)"),
    ("service",           "internal/local_agent/session/service.py",    "session_event_service.py (PR F1)"),
    ("storage",           "internal/local_agent/session/storage.py",    "session_store.py (PR F1)"),
    ("selfcheck",         "internal/local_agent/session/selfcheck.py",  "self_check.py (PR F1)"),
    ("ProductOutcome",    "internal/local_agent/session/contracts.py",  "TaskOutcome (PR F2)"),
    ("serve",             "internal/measurement/serve.py",              "serving/serve.py (PR H)"),
    ("qualify_server",    "internal/measurement/qualify_server.py",     "serving/validate_model_server.py (PR H)"),
    ("benchmark_model",   "internal/measurement/benchmark_model.py",    "benchmarking/ (PR H)"),
    ("run_benchmark_suite","internal/measurement/run_benchmark_suite.py","benchmarking/ (PR H)"),
    ("machine_telemetry", "internal/measurement/machine_telemetry.py",  "measurement/ (PR H)"),
    ("run_test_suite",    "internal/measurement/run_test_suite.py",     "devtools/ (PR H)"),
    ("rescore_dataset",   "internal/measurement/rescore_dataset.py",    "analysis/ (PR H)"),
    ("chat",              "internal/scripts/chat.py",                   "chat/ (PR I)"),
    ("chat_persona",      "internal/scripts/chat_persona.py",           "chat/ (PR I)"),
    ("stamp_package",     "internal/scripts/stamp_package.py",          "packaging/ (PR I)"),
    ("stdio",             "internal/local_agent/rpc/stdio.py",          "quarantine only (PR G)"),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("identifiers", nargs="*", help="limit the audit to these")
    parser.add_argument(
        "--probe", action="append", default=[], metavar="NAME[=PATH]",
        help="audit an identifier that is not in the proposal table, for a rename "
             "nobody has written down yet. Repeatable.")
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[2],
        help="repository root; defaults to this script's own checkout")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    provenance = load_provenance(repo)
    rows = [p for p in PROPOSALS if not args.identifiers or p[0] in args.identifiers]
    for probe in args.probe:
        name, _, path = probe.partition("=")
        rows.append((name, path or "(not a file)", "ad-hoc probe"))
    audited = [audit(repo, provenance, *r) for r in rows]

    def verdict(row) -> str:
        if row["hard"]:
            return "BLOCKED: axis cannot be preserved"
        if row["outcome"] or row["prompt"]:
            return "CONDITIONAL: preserve the binding"
        return "safe"

    if args.markdown:
        print("| Identifier/path | Current location | Proposed | In `source_sha256`? "
              "| This file IS an axis input | Name occurs in `base_prompt` input | "
              "Name occurs in `outcome_contract` input | Wire value? | Bare-name import? "
              "| Config/script ref? | Verdict |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        for row in audited:
            mark = {"safe": "safe",
                    "CONDITIONAL: preserve the binding": "**conditional**",
                    "BLOCKED: axis cannot be preserved": "**BLOCKED**"}[verdict(row)]
            print(f"| `{row['identifier']}` | `{row['current']}` | {row['proposed']} "
                  f"| {'yes' if row['source'] else 'no'} "
                  f"| {'; '.join(row['axis_input']) or 'no'} "
                  f"| {'YES' if row['prompt'] else 'no'} "
                  f"| {'YES' if row['outcome'] else 'no'} | {'YES' if row['wire'] else 'no'} "
                  f"| {len(row['bare'])} | {len(row['config'])} | {mark} |")
        if UNANALYSABLE:
            print()
            print("> **Inconclusive.** " + "; ".join(dict.fromkeys(UNANALYSABLE)))
            return 3
        return 2 if any(r["hard"] for r in audited) else 0

    blocked = [r for r in audited if verdict(r) != "safe"]
    print(f"{'identifier':22s} {'verdict':26s} {'src':4s} {'bare':5s} {'cfg':4s} proposed")
    print("-" * 112)
    for row in audited:
        print(f"{row['identifier']:22s} {verdict(row):26s} "
              f"{'yes' if row['source'] else 'no':4s} {len(row['bare']):<5d} {len(row['config']):<4d} {row['proposed']}")
        for fact in row["axis_input"]:
            print(f"{'':22s}  this file IS an axis input <- {fact}")
        for site in row["hard"]:
            print(f"{'':22s}  HARD BLOCKER <- {site}")
        for note in row["remedy"]:
            print(f"{'':22s}  remedy <- {note}")
        for site in row["bare"][:3]:
            print(f"{'':22s}  bare-name import <- {site}")
        for site in row["config"][:3]:
            print(f"{'':22s}  config/script <- {site}")
        if row["wire"]:
            print(f"{'':22s}  wire constants containing this name <- {row['wire']}")
    print()
    if UNANALYSABLE:
        print()
        print("INCONCLUSIVE, so this run fails closed:")
        for note in dict.fromkeys(UNANALYSABLE):
            print(f"  {note}")

    hard = [r for r in audited if r["hard"]]
    conditional = [r for r in audited if not r["hard"] and (r["outcome"] or r["prompt"])]
    print(f"{len(hard)} of {len(audited)} renames cannot preserve a frozen axis: "
          f"{sorted(r['identifier'] for r in hard)}")
    print(f"{len(conditional)} of {len(audited)} touch an axis but can preserve it: "
          f"{sorted(r['identifier'] for r in conditional)}")
    print()
    print("A zero exit means no axis obstacle was DETECTED. It does not prove the tree "
          "imports,\nthat behaviour is unchanged, or that non-Python references were "
          "updated. See the\nmodule docstring.")
    if UNANALYSABLE:
        return 3
    return 2 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
