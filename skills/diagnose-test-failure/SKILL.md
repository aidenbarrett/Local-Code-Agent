---
name: diagnose-test-failure
description: >
  Diagnoses failing, crashing, hanging or flaky C++ tests and explains the
  defect. Read-only: it finds and explains, it does not edit. Use when a test
  fails, a test asserts, a test segfaults, a test times out, or when asked why
  the suite is red.
tier: cheap
escalation: allowed
verification:
  required: true
tools: [run_test, read_log_chunk, read_file, search_text, find_definition, git_diff]
---

# Goal

Identify which test fails, why it fails, and whether the fault is in the test or
in the code under test. Those are different answers and confusing them wastes
everybody's time. Stop there. This skill does not change any file. If the task
asks for a fix, that is a different skill.

# Workflow

1. Run `run_test` with a `name_filter` for the failing test only. Reproduce
   before theorising.
2. Read the failure evidence in the tool result: status, assertion text, crash
   markers.
3. Page the log with `read_log_chunk` around the reported `log_line` for the
   assertion message and any values it printed.
4. Read the test body. Then read the code under test.
5. Use `git_diff` to see whether the code under test changed recently. A test
   that used to pass and now fails usually points at the diff.
6. Decide, explicitly: is the test wrong, or is the code wrong?
7. State the defect in one or two sentences: file, line, the predicate that is
   wrong and what it should be.
8. Stop when the defect is established. The task is the explanation, not a
   repair.

# Classifying by status

- `Failed`: an assertion or a non-zero exit. Read the assertion.
- `SEGFAULT` / `Subprocess aborted`: a lifetime, bounds or null problem. Look at
  indexing and ownership near the last thing the test did.
- `Timeout`: a loop with no exit or a wait with no deadline. Look for the
  termination condition.
- Passes on rerun with no change: flaky. Report it as non-deterministic. Do not
  "fix" it by loosening the assertion.

# Guardrails

- Do not edit anything. You have no patch tools here, and reaching for one is a
  wrong turn, not a shortcut.
- One `run_test` at the start is the reproduction. Rerunning it changes nothing
  and tells you nothing new: the evidence you need is already in the first
  result and in the source.
- Never blame a test for being wrong without quoting the expectation and saying
  why it is wrong.
