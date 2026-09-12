from __future__ import annotations

import re
from pathlib import Path

root = Path(__import__('sys').argv[1]).resolve()

# Extend cross-platform build evidence tests after the content-bound core patch.
p = root / 'tests/unit/test_cross_platform_evidence.py'
text = p.read_text(encoding='utf-8')
append = '''\n\ndef test_deleted_build_input_is_stale(tmp_path: Path):
    source = tmp_path / "src" / "thing.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int x;\\n", encoding="utf-8")
    record = BuildRecord(
        profile="debug", at_ns=1,
        source_hashes={"src/thing.cpp": _sha(source.read_bytes())},
    )
    source.unlink()
    assert _stale_sources(tmp_path, "build", record) == ["src/thing.cpp"]


def test_ancestor_named_build_does_not_disable_staleness(tmp_path: Path):
    root = tmp_path / "build" / "worktree"
    source = root / "src" / "thing.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int x;\\n", encoding="utf-8")
    record = BuildRecord(
        profile="debug", at_ns=1,
        source_hashes={"src/thing.cpp": _sha(b"int y;\\n")},
    )
    assert _stale_sources(root, "build-dir", record) == ["src/thing.cpp"]
'''
if 'test_deleted_build_input_is_stale' not in text:
    text += append
p.write_text(text, encoding='utf-8', newline='\n')

# Endpoint tests now carry oracle tamper and manifest-backed attempt declarations.
p = root / 'tests/unit/test_endpoint_analysis.py'
text = p.read_text(encoding='utf-8')
old = '         outcome="pass", oracle_ok=True, disagreement=False, invented=None):'
new = '         outcome="pass", oracle_ok=True, disagreement=False, invented=None, tampered=False):'
if text.count(old) != 1:
    raise SystemExit('endpoint _row signature moved')
text = text.replace(old, new)
old = '        "verification_disagreement": disagreement,\n        "scope_violation": scope,'
new = '        "verification_disagreement": disagreement,\n        "oracle_tampered": tampered,\n        "scope_violation": scope,'
if text.count(old) != 1:
    raise SystemExit('endpoint _row body moved')
text = text.replace(old, new)
helper_marker = '\n\ndef test_engineering_correctness_is_not_claim_compliance():'
helper = '''\n\ndef _declared(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault((row["condition"], row["case"]), []).append(row["attempt"])
    return {key: max(values) + 1 for key, values in grouped.items() if values}
'''
if helper not in text:
    text = text.replace(helper_marker, helper + helper_marker, 1)
text = text.replace('analyse(rows)', 'analyse(rows, _declared(rows))')
old_block = '''def test_invalid_replacement_yields_three_valid_draws_without_counting_invalid():
    rows = _three("link-error", "narrow", 2)
    rows.insert(1, _row("link-error", "narrow", counted=False, engineering=False, attempt=99))
    result = analyse(rows, _declared(rows))["by_condition"]["narrow"]["tasks"]["link-error"]
    assert result["engineering_correct"]["decision"] == "pass"
    assert result["engineering_correct"]["valid_draws"] == 3
'''
new_block = '''def test_invalid_replacement_yields_three_valid_draws_without_counting_invalid():
    rows = [
        _row("link-error", "narrow", engineering=True, attempt=0),
        _row("link-error", "narrow", counted=False, engineering=False, attempt=1),
        _row("link-error", "narrow", engineering=True, attempt=2),
        _row("link-error", "narrow", engineering=False, attempt=3),
    ]
    result = analyse(rows, _declared(rows))["by_condition"]["narrow"]["tasks"]["link-error"]
    assert result["engineering_correct"]["decision"] == "pass"
    assert result["engineering_correct"]["valid_draws"] == 3
'''
if text.count(old_block) != 1:
    raise SystemExit('invalid replacement test moved')
text = text.replace(old_block, new_block)
text = text.replace('collection continued after the third valid draw',
                    'collection continued after the third decision draw')
append = '''\n\ndef test_oracle_tampering_is_terminal_nonreplaceable_model_behaviour():
    rows = [
        _row("link-error", "control", attempt=0, tampered=True),
        _row("link-error", "control", attempt=1, tampered=True),
        _row("link-error", "control", attempt=2),
    ]
    out = analyse(rows, _declared(rows))["by_condition"]["control"]
    cell = out["tasks"]["link-error"]
    assert cell["engineering_correct"]["decision"] == "fail"
    assert cell["contract_compliant"]["decision"] == "fail"
    assert cell["verified_completion"]["decision"] == "fail"
    assert cell["attempts"]["oracle_tampered_attempts"] == 2
    assert cell["attempts"]["invalid_attempts"] == 0
    assert out["oracle_tampered_attempts"] == 2


def test_deleted_attempt_is_detected_from_manifest_count():
    rows = [
        _row("link-error", "narrow", engineering=True, attempt=0),
        _row("link-error", "narrow", engineering=True, attempt=2),
        _row("link-error", "narrow", engineering=True, attempt=3),
    ]
    cell = analyse(rows, {("narrow", "link-error"): 4})["by_condition"]["narrow"]["tasks"]["link-error"]
    assert cell["attempts"]["protocol_violation"] is True
    assert any("attempt sequence incomplete" in reason for reason in cell["attempts"]["protocol_violation_reasons"])


def test_renumbered_attempts_are_detected():
    rows = [
        _row("link-error", "narrow", attempt=17),
        _row("link-error", "narrow", attempt=4),
        _row("link-error", "narrow", attempt=900),
    ]
    cell = analyse(rows, {("narrow", "link-error"): 3})["by_condition"]["narrow"]["tasks"]["link-error"]
    assert cell["attempts"]["protocol_violation"] is True
'''
if 'test_oracle_tampering_is_terminal_nonreplaceable_model_behaviour' not in text:
    text += append
p.write_text(text, encoding='utf-8', newline='\n')

# Collector persists its declared attempt count.
p = root / 'tests/unit/test_repeat_collection.py'
text = p.read_text(encoding='utf-8')
if 'test_checkpoint_records_attempt_count_for_each_case' not in text:
    text += '''\n\ndef test_checkpoint_records_attempt_count_for_each_case(tmp_path):
    import json
    case = SimpleNamespace(name="case-a")
    out = tmp_path / "rows.json"
    run_all(
        [case], 3, lambda c, attempt: _row(c, attempt, True),
        out, {"condition": "narrow"}, echo=lambda *_: None, max_attempts=5,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["attempts_declared"] == {"case-a": 3}
'''
p.write_text(text, encoding='utf-8', newline='\n')

# Real run_case rows must grade under the endpoint schema.
p = root / 'tests/integration/test_endpoint_row_contract.py'
p.write_text('''from __future__ import annotations

import pytest

from evaluation.endpoints import row_endpoints
from evaluation.run_evaluation import CASES, ModelConfig, run_case


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_real_run_case_rows_grade_under_every_endpoint(case, tmp_path):
    row = run_case(
        case, ModelConfig(), tmp_path / case.name, auto_approve=True,
        rehearse=True, attempt=0, condition="skill",
    )
    assert row.get("error") is None
    endpoints = row_endpoints(row)
    for name in ("engineering_correct", "contract_compliant", "verified_completion", "efficiency"):
        assert endpoints[name] is not None, (case.name, name, row)
''', encoding='utf-8', newline='\n')

# Compat shim must fail loudly for unsupported features and implement delenv.
p = root / 'tests/unit/test_compat_runner_contract.py'
p.write_text('''from __future__ import annotations

import os
import pytest
from measurement import run_test_suite as compat


def test_monkeypatch_delenv_matches_pytest_shape(monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_COMPAT_PROBE", "1")
    monkeypatch.delenv("LOCAL_AGENT_COMPAT_PROBE")
    assert "LOCAL_AGENT_COMPAT_PROBE" not in os.environ
    monkeypatch.delenv("LOCAL_AGENT_COMPAT_PROBE", raising=False)


def test_compat_mark_factory_rejects_unknown_marks():
    with pytest.raises(AttributeError):
        getattr(compat._MarkFactory(), "usefixtures")


def test_compat_fixture_rejects_unimplemented_keywords():
    with pytest.raises(TypeError):
        compat._fixture(autouse=True)
''', encoding='utf-8', newline='\n')
