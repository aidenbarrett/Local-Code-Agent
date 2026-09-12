from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected one target, found {text.count(old)}")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


root = Path(__import__("sys").argv[1]).resolve()

# The real-row contract test must actually use the structured completion path,
# otherwise cited_correctly legitimately remains None on verification tasks.
p = root / "tests/integration/test_endpoint_row_contract.py"
p.write_text('''from __future__ import annotations

import json
import pytest

from evaluation.endpoints import row_endpoints
from evaluation.run_evaluation import CASES, ModelConfig, RehearsalClient, run_case
from local_agent.llm.models import CallStats, ChatResponse, ToolCall


class StructuredRehearsalClient:
    """Exercise real run_case rows and finish through submit_answer."""

    def __init__(self, claim: str) -> None:
        self.base = RehearsalClient()
        self.claim = claim
        self.submitted = False

    def chat(self, messages, tools=None, max_tokens=None):
        response = self.base.chat(messages, tools=tools, max_tokens=max_tokens)
        if response.wants_tools:
            return response
        if not self.submitted:
            self.submitted = True
            args = json.dumps({
                "claim": self.claim,
                "summary": "structured row-contract rehearsal",
                "evidence_ids": [],
            })
            return ChatResponse(
                tool_calls=[ToolCall.from_parts("submit-contract", "submit_answer", args)],
                stats=CallStats(total_s=0.0, ttft_s=0.0, prompt_tokens=1,
                                completion_tokens=8, streamed=True),
            )
        return response


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_real_run_case_rows_grade_under_every_endpoint(case, tmp_path):
    claim = sorted(str(value) for value in case.expected_claim)[0]
    row = run_case(
        case, ModelConfig(), tmp_path / case.name, auto_approve=True,
        client=StructuredRehearsalClient(claim), attempt=0, condition="skill",
    )
    assert row.get("error") is None
    endpoints = row_endpoints(row)
    for name in (
        "engineering_technical_correct",
        "engineering_correct",
        "contract_compliant",
        "verified_completion",
        "efficiency",
    ):
        assert endpoints[name] is not None, (case.name, name, row)
''', encoding="utf-8", newline="\n")

# Existing oracle-isolation tests described the superseded free-retry policy.
p = root / "tests/integration/test_oracle_isolation.py"
replace_once(
    p,
    '''    assert row["verification_disagreement"] is True
    assert row["succeeded"] is False
    assert row["counted"] is False
''',
    '''    assert row["verification_disagreement"] is True
    assert row["succeeded"] is False
    # Pre-registered Gen2 policy: caught oracle tampering is model behaviour,
    # occupies a decision slot, and is never replaceable as infrastructure loss.
    assert row["counted"] is True
    assert row["validity"] == "oracle_tampered"
    assert row["agent_validity"] == "valid"
''',
)
replace_once(
    p,
    '''def test_ledger_excludes_tampered_runs_from_every_denominator():
    from run_evaluation import build_ledger

    honest = {"case": "a", "outcome": "pass", "succeeded": True, "counted": True,
              "validity": "valid", "oracle_tampered": False, "elapsed_s": 10}
    cheat = {"case": "b", "outcome": "pass", "succeeded": False, "counted": False,
             "validity": "valid", "oracle_tampered": True, "elapsed_s": 5}
    dead = {"case": "c", "outcome": "blocked", "succeeded": False, "counted": False,
            "validity": "invalid_server_unavailable", "oracle_tampered": False, "elapsed_s": 1}

    ledger = build_ledger([honest, cheat, dead])
    assert ledger["tasks_submitted"] == 3
    assert ledger["tasks"] == 1                 # only the honest run is a task
    assert ledger["excluded"] == 2
    assert ledger["oracle_tampered_cases"] == ["b"]
    assert ledger["invalid_cases"] == [{"case": "c", "validity": "invalid_server_unavailable"}]
    assert ledger["local_success_rate"] == 1.0  # 1/1, not 1/3 and not 2/3
    assert ledger["end_to_end_rate"] == 1.0
''',
    '''def test_ledger_keeps_tampering_as_a_failed_model_attempt():
    from run_evaluation import build_ledger

    honest = {"case": "a", "outcome": "pass", "succeeded": True, "counted": True,
              "validity": "valid", "oracle_tampered": False, "elapsed_s": 10}
    cheat = {"case": "b", "outcome": "pass", "succeeded": False, "counted": True,
             "validity": "oracle_tampered", "oracle_tampered": True, "elapsed_s": 5}
    dead = {"case": "c", "outcome": "blocked", "succeeded": False, "counted": False,
            "validity": "invalid_server_unavailable", "oracle_tampered": False, "elapsed_s": 1}

    ledger = build_ledger([honest, cheat, dead])
    assert ledger["tasks_submitted"] == 3
    assert ledger["tasks"] == 2
    assert ledger["excluded"] == 1
    assert ledger["oracle_tampered_cases"] == ["b"]
    assert ledger["invalid_cases"] == [{"case": "c", "validity": "invalid_server_unavailable"}]
    assert ledger["local_success_rate"] == 0.5
    assert ledger["end_to_end_rate"] == 0.5
''',
)

# Keep the retiring ledger internally consistent with the new validity class.
p = root / "evaluation/run_evaluation.py"
replace_once(
    p,
    '''    # Runs that may not enter a denominator: the agent edited the oracle, or the
    # run happened under a configuration nobody chose. Reported, never counted.
    excluded = [r for r in rows if r.get("counted") is False]
    tampered = [r for r in rows if r.get("oracle_tampered")]
    invalid = [r for r in rows if r.get("validity") not in (None, "valid")]
''',
    '''    # Infrastructure-invalid rows do not enter a denominator. Oracle
    # tampering is model behaviour and therefore remains a counted failed task.
    excluded = [r for r in rows if r.get("counted") is False]
    tampered = [r for r in rows if r.get("oracle_tampered")]
    invalid = [
        r for r in rows
        if r.get("validity") not in (None, "valid", "oracle_tampered")
    ]
''',
)
# The ledger was the last fail-open reader of counted.
text = p.read_text(encoding="utf-8")
text = text.replace('rows = [r for r in rows if r.get("counted", True)]',
                    'rows = [r for r in rows if r.get("counted") is True]')
p.write_text(text, encoding="utf-8", newline="\n")
