from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORCH = ROOT / "internal/local_agent/agent/orchestrator.py"
TEST = ROOT / "internal/tests/unit/test_build_summary_gate_contract.py"
INSTRUMENT = ROOT / "internal/INSTRUMENT.json"
WORKFLOW = ROOT / ".github/workflows/apply-build-summary-gate.yml"

text = ORCH.read_text(encoding="utf-8")
old = '''        answer = ""\n        nudged = False\n\n        while True:\n'''
new = '''        answer = ""\n        nudged = False\n        task_lower = task.lower()\n        build_summary_mode = (\n            skill is not None\n            and skill.name == "repo-navigation"\n            and "build" in task_lower\n            and ("repository" in task_lower or "repo" in task_lower)\n        )\n\n        while True:\n'''
if text.count(old) != 1:
    raise SystemExit(f"build-summary mode insertion expected once, got {text.count(old)}")
text = text.replace(old, new)

old = '''                ctx.append(\n                    ctxmod.tool_result_message(\n                        call.id,\n                        call.name,\n                        outcome.to_json(tool_result_max_bytes),\n                    )\n                )\n                if stop:\n                    halt = True\n                    break\n'''
new = '''                ctx.append(\n                    ctxmod.tool_result_message(\n                        call.id,\n                        call.name,\n                        outcome.to_json(tool_result_max_bytes),\n                    )\n                )\n                if build_summary_mode and call.name == "repo_info" and outcome.ok:\n                    # For a high-level repository build summary, repo_info already\n                    # contains the configured build/test profile. Do not leave the\n                    # small model a broad discovery surface after sufficient\n                    # evidence exists: narrow deterministically to reporting only.\n                    toolset = ["submit_answer"]\n                    state.toolset = list(toolset)\n                    schemas = self.registry.schemas(toolset)\n                    ctx.append(\n                        {\n                            "role": "user",\n                            "content": (\n                                "The repository build/test profile from repo_info is "\n                                "sufficient for this high-level build summary. Finish "\n                                "now by calling submit_answer with a concise summary "\n                                "grounded in that repo_info result. Do not call another "\n                                "discovery tool."\n                            ),\n                        }\n                    )\n                    self.observer(\n                        "toolset",\n                        {"tools": toolset, "reason": "repo_info sufficient for build summary"},\n                    )\n                if stop:\n                    halt = True\n                    break\n'''
if text.count(old) != 1:
    raise SystemExit(f"post-tool narrowing insertion expected once, got {text.count(old)}")
ORCH.write_text(text, encoding="utf-8")

TEST.write_text('''from pathlib import Path\n\n\nREPO = Path(__file__).resolve().parents[3]\n\n\ndef test_repo_build_summary_is_deterministically_narrowed_after_repo_info():\n    source = (REPO / "internal" / "local_agent" / "agent" / "orchestrator.py").read_text(encoding="utf-8")\n    assert 'skill.name == "repo-navigation"' in source\n    assert 'build_summary_mode' in source\n    assert 'call.name == "repo_info"' in source\n    assert 'and outcome.ok' in source\n    assert 'toolset = ["submit_answer"]' in source\n    assert 'repo_info sufficient for build summary' in source\n    assert 'Do not call another discovery tool.' in source\n''', encoding="utf-8")

instrument = json.loads(INSTRUMENT.read_text(encoding="utf-8"))
old_base = instrument["base_prompt_sha256"]
old_outcome = instrument["outcome_contract_sha256"]
sys.path.insert(0, str(ROOT / "internal"))
from local_agent import provenance as p
new_source = p.source_sha256()
new_base = p.base_prompt_sha256()
new_outcome = p.outcome_contract_sha256()
if new_base != old_base:
    raise SystemExit(f"base prompt identity moved unexpectedly: {old_base} -> {new_base}")
if new_outcome != old_outcome:
    raise SystemExit(f"outcome contract identity moved unexpectedly: {old_outcome} -> {new_outcome}")
instrument["source_sha256"] = new_source
INSTRUMENT.write_text(json.dumps(instrument, indent=2) + "\n", encoding="utf-8")
print(f"source_sha256={new_source}")
print(f"base_prompt_sha256={new_base}")
print(f"outcome_contract_sha256={new_outcome}")

# One-shot scaffolding must not survive into the proposed tree.
Path(__file__).unlink()
if WORKFLOW.exists():
    WORKFLOW.unlink()
