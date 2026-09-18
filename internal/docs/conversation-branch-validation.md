# Conversation branch validation

Date: 2026-09-18. Base commit: `26fd55f0fc9705801e104d26afee377dac5db077`.
The working snapshot's 222 blobs were compared against the live GitHub tree:
every blob matched before editing. Remote branch commits use that real parent,
not the temporary local snapshot commit used for diff inspection.

## Local checks performed

Host: Linux x86_64, Python 3.12.14, real pytest 9.1.1. Existing pure-Python test
dependencies were made available through PYTHONPATH in this managed environment.

Real pytest: **245 passed, 1 skipped** across the following explicit set:

```text
internal/tests/unit/test_session_gateway.py
internal/tests/unit/test_session_selfcheck.py
internal/tests/integration/test_session_python_check.py
internal/tests/unit/test_chat_persona.py
internal/tests/unit/test_chat_server_orchestration.py
internal/tests/unit/test_chat_context.py
internal/tests/unit/test_user_facing_entrypoints.py
internal/tests/unit/test_policy_and_skills.py
internal/tests/unit/test_provenance.py
internal/tests/unit/test_no_syntax_warnings.py
internal/tests/unit/test_context_budget_guard.py
internal/tests/unit/test_rpc.py
internal/tests/unit/test_run_task_ui.py
internal/tests/unit/test_runtime_agnostic.py
```

Run using `python -m pytest -q -o addopts=''` followed by those paths. The new
session tests account for 32 tests and include real compileall/pytest subprocesses
in small Git checkouts for both pass and fail outcomes. Worker tests use controlled
model responses through the real hardened controller and repository tools.

Compatibility runner (not the authoritative result) separately passed:

| File | Passed |
|---|---:|
| `test_session_gateway.py` | 18 |
| `test_session_selfcheck.py` | 12 |
| `test_session_python_check.py` | 2 |
| `test_chat_persona.py` | 11 |

Also checked Python compilation, session CLI help, diff whitespace, regenerated
fixture equality, unchanged frozen experiment files and all three declared
instrument identities.

## What is not established

- The full C++ suite was not run locally. CMake was absent from PATH; a preexisting
  copied binary was truncated and crashed, and package retrieval was unavailable.
- Windows execution, PowerShell startup, real NPU inference, natural-language
  proposal reliability, two-endpoint residency and hardware counters were not run.
- These tests do not prove an autonomous edit/test loop. V0 source mutations are
  deliberately disabled; the workspace/approval implementations are future work.
- Exact-head GitHub CI must be checked on the draft PR. Creating a branch or
  passing this targeted subset is not a claim that native CI is green.

## Baseline failure repaired

The baseline's Linux/Windows/compatibility jobs failed at
`test_chat_temperature_override_does_not_mutate_named_profile`. Its mock replaced
`converse`, which now owns `_ensure_server`, but expected both startup temperature
observations. The repaired test uses real `converse`, mocks only server startup,
client creation and empty input, and retains `[0.7, 0.7]` plus unchanged preset
assertions. No production temperature/persona behaviour was changed.

## Provenance boundary

New product Python files move `source_sha256`; `internal/INSTRUMENT.json` is updated
in the same change. The base prompt and outcome-contract hashes remain unchanged.
No frozen data, historical tag, measurement scoring or CI workflow was edited.
Future measurements of this product flow require an explicit new experiment
definition and frozen generation; do not mix these operational checks into old rows.
