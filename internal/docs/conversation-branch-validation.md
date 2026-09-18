# Conversation branch validation

Date: 2026-09-18. Base commit: `26fd55f0fc9705801e104d26afee377dac5db077`.
The working snapshot's 222 blobs were compared against the live GitHub tree:
every blob matched before editing. Remote branch commits use that real parent,
not the temporary local snapshot commit used for diff inspection.

## Local checks performed

Host: Linux x86_64, Python 3.12.14, real pytest 9.1.1. Existing pure-Python test
dependencies were made available through PYTHONPATH in this managed environment.

After the review fixes, real pytest: **255 passed, 1 skipped** across the
following explicit set (the earlier prototype run was 245 passed, 1 skipped):

```text
internal/tests/unit/test_session_gateway.py
internal/tests/unit/test_gateway_review_contracts.py
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

Run using `python -m pytest -q -o addopts=''` followed by those paths. The original
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

## Review integration

The supplied review bundle was checked against the prototype before integration.
Its reported 684-test result is the reviewer's result, not a locally reproduced
full-suite result. Our explicit subset above includes nine new review regressions;
those nine also passed the offline compatibility runner. Behavior checks replace
source-string checks for budgets, blocked-event projection and interrupt handling.
A real-controller test now uses the actual `submit_answer.evidence_ids` field
instead of the ignored `evidence` key and asserts unchanged canonical IDs across
two distinct task namespaces.

Budget sizing now distinguishes characters from serialized UTF-8 bytes. The
four-character token conversion is a heuristic; the review's exact 25% occupancy
claim is not established. History retains task prose but excludes the appended
controller verdict/evidence block. An over-budget exchange is recorded before
returning, with a refusal event; bounded in-memory eviction is still possible.

Evidence IDs are canonical `name:index` values, paired structurally with `task_id`.
The self-check, TaskResult, terminal event and tests agree on that convention.
The event ring locks allocation and reads, serializes synchronous sink delivery,
and records UTC wall time. Concurrent duplicate allocation was not reproduced in
the earlier code; this is an explicit synchronization contract, not a claimed
reproduction. A slow-sink regression checks concurrent snapshots and delivery order.

Only source_sha256 changes in this runtime follow-up. Neither agent/context.py
nor any of the hashed prompt functions was edited. The supplied INSTRUMENT.json
was not copied; the declaration was recomputed from the actual combined tree.
The preceding design commit adds the proposed 19-variant event schema and design
only. JSON syntax, local references and unique vocabulary were checked; full JSON
Schema validation and implementation conformance are future gates.

## Second review: design-contract guard

Based on remote head `6ebca648acc1088d72acbe17309f67ab719a7130`.
The supplied ZIP contains four test functions, despite the review reporting five.
The integrated guard has seven tests: strict JSON/identity structure, exhaustive
unique kind branches, reviewed required envelope/payload fields, closed object
shapes, local reference resolution, prototype/schema disjointness, and the
README's explicit design-only boundary. Dynamic worker names use the AST table;
unaccounted dynamic `.emit` expressions fail instead of being silently omitted.

Linux x86_64 / Python 3.12.14: real pytest **7 passed**, offline compatibility
runner **7 passed** for `internal/tests/unit/test_session_contract_schema.py`.
Eight isolated mutations were rejected: malformed JSON, duplicate kind, missing
kind, deleting a payload property and its required entry together, dangling ref,
literal name overlap, dynamic projection overlap, and an unknown dynamic emitter.
No production files were mutated during these checks.

This is structural drift protection, not full JSON Schema validation or runtime
conformance. A name inventory cannot establish producer authority or lifecycle
semantics. The migration gate now states that explicitly. No dependency, runtime
behavior, schema bytes or identity declaration changed. All three identities were
recomputed and match. The reviewer's 691-test result was not rerun here; targeted
checks are sufficient for this tests-and-documentation change. No CI polling.
