from __future__ import annotations

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
                stats=CallStats(
                    total_s=0.0, ttft_s=0.0, prompt_tokens=1,
                    completion_tokens=8, streamed=True,
                ),
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


def test_real_prose_row_is_known_noncompliance_not_unknown(tmp_path):
    case = next(case for case in CASES if case.name == "clean-build")
    row = run_case(
        case, ModelConfig(), tmp_path / "negative", auto_approve=True,
        rehearse=True, attempt=0, condition="skill",
    )
    assert row.get("error") is None
    assert row["submission_mode"] == "prose"
    assert row["claim_ok"] is False
    assert row["cited_correctly"] is None

    endpoints = row_endpoints(row)
    assert endpoints["engineering_correct"] is True
    assert endpoints["contract_compliant"] is False
    assert endpoints["verified_completion"] is False
