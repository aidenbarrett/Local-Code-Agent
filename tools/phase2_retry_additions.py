from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected one target, found {text.count(old)}")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


root = Path(__import__("sys").argv[1]).resolve()

# The content-bound source scanner uses Path at runtime.
p = root / "local_agent/tools/testing.py"
text = p.read_text(encoding="utf-8")
if "from pathlib import Path\n" not in text:
    if text.count("import re\n") != 1:
        raise SystemExit("testing.py import anchor moved")
    text = text.replace("import re\n", "import re\nfrom pathlib import Path\n", 1)
p.write_text(text, encoding="utf-8", newline="\n")

# A missing/renumbered attempt makes the decision itself indeterminate. Extra
# collection after a complete first-three decision set remains reportable but
# cannot rewrite that already-complete set.
p = root / "evaluation/endpoints.py"
text = p.read_text(encoding="utf-8")
replace_once(
    p,
    '''    reasons: list[str] = []
    bad = any(not isinstance(v, int) or isinstance(v, bool) for v in attempts)
''',
    '''    reasons: list[str] = []
    integrity_reasons: list[str] = []
    bad = any(not isinstance(v, int) or isinstance(v, bool) for v in attempts)
''',
)
text = p.read_text(encoding="utf-8")
text = text.replace(
    '''    if bad:
        reasons.append("attempt index missing or non-integer")
    if duplicate:
        reasons.append("duplicate attempt index")
    if not isinstance(attempts_declared, int) or isinstance(attempts_declared, bool):
        reasons.append("attempt count missing from run manifest")
    elif not 0 <= attempts_declared <= MAX_ATTEMPTS_PER_TASK_CONDITION:
        reasons.append("declared attempt count outside bounded protocol")
''',
    '''    if bad:
        reasons.append("attempt index missing or non-integer")
        integrity_reasons.append("attempt index missing or non-integer")
    if duplicate:
        reasons.append("duplicate attempt index")
        integrity_reasons.append("duplicate attempt index")
    if not isinstance(attempts_declared, int) or isinstance(attempts_declared, bool):
        reasons.append("attempt count missing from run manifest")
        integrity_reasons.append("attempt count missing from run manifest")
    elif not 0 <= attempts_declared <= MAX_ATTEMPTS_PER_TASK_CONDITION:
        reasons.append("declared attempt count outside bounded protocol")
        integrity_reasons.append("declared attempt count outside bounded protocol")
''',
    1,
)
text = text.replace(
    '''        if observed != expected:
            reasons.append(
                f"attempt sequence incomplete: declared {expected}, observed {observed}"
            )
''',
    '''        if observed != expected:
            message = f"attempt sequence incomplete: declared {expected}, observed {observed}"
            reasons.append(message)
            integrity_reasons.append(message)
''',
    1,
)
text = text.replace(
    '''    invalid_rows = [row for row in window if row.get("counted") is not True]
''',
    '''    if integrity_reasons:
        selected = []

    invalid_rows = [row for row in window if row.get("counted") is not True]
''',
    1,
)
text = text.replace(
    '''        "protocol_violation": bool(reasons),
        "protocol_violation_reasons": reasons,
''',
    '''        "decision_integrity_ok": not integrity_reasons,
        "decision_integrity_reasons": integrity_reasons,
        "protocol_violation": bool(reasons),
        "protocol_violation_reasons": reasons,
''',
    1,
)
p.write_text(text, encoding="utf-8", newline="\n")

# The provenance regression must reconstruct the expanded raw-byte contract,
# including setup/error paths, not the old run_case/run_all-only definition.
p = root / "tests/unit/test_provenance.py"
text = p.read_text(encoding="utf-8")
start = text.index("def test_outcome_contract_covers_tasks_oracle_grader_and_endpoint_policy():")
replacement = '''def test_outcome_contract_covers_tasks_oracle_grader_and_endpoint_policy():
    source = provenance.outcome_contract_sha256()
    assert source != "unavailable-no-source"

    evaluator = REPO / "evaluation" / "run_evaluation.py"
    function_names = ("prepare", "establish", "_error_row", "run_case", "run_all")
    functions = {
        name: provenance._function_source(evaluator, name)
        for name in function_names
    }
    assert all(body is not None for body in functions.values())
    assert "required_ok" in functions["run_case"]
    assert "claim_ok" in functions["run_case"]
    assert "scope_violation" in functions["run_case"]
    assert "verification_disagreement" in functions["run_case"]
    assert "attempts_declared" in functions["run_all"]

    endpoints = (REPO / "evaluation" / "endpoints.py").read_bytes()
    assert b"ENGINEERING_CONTRACTS" in endpoints
    assert b"VALID_DRAWS_PER_TASK_CONDITION = 3" in endpoints
    assert b"MAX_ATTEMPTS_PER_TASK_CONDITION = 5" in endpoints

    parts: list[tuple[str, bytes]] = [
        ("evaluation/task_contracts.py", (REPO / "evaluation" / "task_contracts.py").read_bytes()),
        ("evaluation/oracle.py", (REPO / "evaluation" / "oracle.py").read_bytes()),
        ("evaluation/endpoints.py", endpoints),
    ]
    for name in function_names:
        body = functions[name]
        assert body is not None
        parts.append((f"evaluation.run_evaluation.{name}", body.encode("utf-8")))

    def digest(items):
        out = hashlib.sha256()
        for label, body in items:
            out.update(label.encode())
            out.update(b"\\0")
            out.update(body)
            out.update(b"\\0")
        return out.hexdigest()

    assert digest(parts) == source
    mutated = list(parts)
    mutated[2] = (mutated[2][0], mutated[2][1] + b"\\n# endpoint-policy mutation\\n")
    assert digest(mutated) != source
'''
text = text[:start] + replacement + "\n"
p.write_text(text, encoding="utf-8", newline="\n")

# Missing-attempt evidence is a hard integrity failure, not merely a warning.
p = root / "tests/unit/test_endpoint_analysis.py"
text = p.read_text(encoding="utf-8")
old = '''    assert cell["attempts"]["protocol_violation"] is True
    assert any("attempt sequence incomplete" in reason for reason in cell["attempts"]["protocol_violation_reasons"])


def test_renumbered_attempts_are_detected():
'''
new = '''    assert cell["attempts"]["protocol_violation"] is True
    assert cell["attempts"]["decision_integrity_ok"] is False
    assert cell["engineering_correct"]["decision"] == "indeterminate"
    assert any("attempt sequence incomplete" in reason for reason in cell["attempts"]["protocol_violation_reasons"])


def test_renumbered_attempts_are_detected():
'''
if text.count(old) != 1:
    raise SystemExit("deleted-attempt regression anchor moved")
text = text.replace(old, new)
old = '''    assert cell["attempts"]["protocol_violation"] is True
'''
# Only strengthen the final renumbered-attempt assertion, identified from the tail.
pos = text.rfind(old)
if pos == -1:
    raise SystemExit("renumbered-attempt assertion moved")
text = text[:pos] + old + '''    assert cell["attempts"]["decision_integrity_ok"] is False
    assert cell["engineering_correct"]["decision"] == "indeterminate"
''' + text[pos + len(old):]
p.write_text(text, encoding="utf-8", newline="\n")
