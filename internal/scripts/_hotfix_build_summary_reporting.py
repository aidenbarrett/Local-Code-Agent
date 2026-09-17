from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORCH = ROOT / "internal/local_agent/agent/orchestrator.py"
TEST = ROOT / "internal/tests/unit/test_build_summary_gate_contract.py"
INSTRUMENT = ROOT / "internal/INSTRUMENT.json"
WORKFLOW = ROOT / ".github/workflows/apply-build-summary-reporting.yml"

text = ORCH.read_text(encoding="utf-8")
old = '''                                "The repository build/test profile from repo_info is "
                                "sufficient for this high-level build summary. Finish "
                                "now by calling submit_answer with a concise summary "
                                "grounded in that repo_info result. Do not call another "
                                "discovery tool."
'''
new = '''                                "The repo_info result is sufficient for this high-level "
                                "build summary. This task asks how the repository is "
                                "configured, not whether its build or tests currently pass. "
                                "Finish now by calling submit_answer with claim='diagnosis' "
                                "and cite the repo_info tool result. Describe the configured "
                                "profile name and build/test command arguments literally. "
                                "Do not infer target languages, target types, build systems, "
                                "or successful execution beyond repo_info. Do not call another "
                                "discovery tool."
'''
if text.count(old) != 1:
    raise SystemExit(f"reporting message replacement expected once, got {text.count(old)}")
text = text.replace(old, new)
ORCH.write_text(text, encoding="utf-8")

TEST.write_text('''import inspect\n\nfrom local_agent.agent.orchestrator import Orchestrator\n\n\ndef test_repo_build_summary_is_deterministically_narrowed_after_repo_info():\n    source = inspect.getsource(Orchestrator._run_once)\n    assert "build_summary_mode" in source\n    assert "submit_answer" in source\n    assert "repo_info sufficient for build summary" in source\n    assert "claim='diagnosis'" in source\n    assert "command arguments literally" in source\n    assert "Do not infer target languages" in source\n    assert "successful execution beyond repo_info" in source\n    assert "Do not call another " in source\n    assert "discovery tool." in source\n''', encoding="utf-8")

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

Path(__file__).unlink()
if WORKFLOW.exists():
    WORKFLOW.unlink()
