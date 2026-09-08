---
name: fix-test-failure
description: >
  Fixes a failing, crashing or hanging C++ test by correcting the code under
  test, then proves it with a full test run. Use when asked to fix, repair or
  make a test pass, or to get the suite green. Edits source, rebuilds, reruns,
  and reports the verified result.
tier: cheap
escalation: allowed
verification:
  required: true
tools: [run_test, read_log_chunk, read_file, search_text, find_definition, git_diff, configure_project, build_target, propose_patch, apply_patch]
---

# Goal

Make the failing test pass by fixing the fault, and prove it with a full,
unfiltered `run_test` that exits clean after the last edit.

# Workflow

1. Run `run_test` once. The result already contains the failing test's name,
   the assertion text, and the file and line it fired on. Do not run it again
   until something has changed.
2. Read the test body around the reported line. Then read the code under test.
3. Decide, explicitly: is the test wrong, or is the code wrong? Almost always the
   code. Say which and why in one sentence.
4. Propose a minimal fix with `propose_patch`, then `apply_patch` it.
5. Rebuild with `build_target` and no target argument. A source edit is not in
   the binary until you build; rerunning the old binary tells you nothing.
6. Run `run_test` with no filter. The contract is the whole suite, not one test.
7. If it still fails, return to step 2 with the new evidence. One fix, one
   rebuild, one rerun.
8. The suite is fixed when a full unfiltered run passes against the tree as
   it stands after the last edit, and not before.

# Guardrails

- Never change a test's expected value to make it pass unless you have shown the
  expectation itself was wrong, and say so in the answer.
- Never delete, skip, disable or gut a test. A test that passes because it no
  longer asserts anything is not a fix; it is a cover-up, and it will be caught.
- Never report the suite green without a full `run_test` with exit code 0 after
  your last edit.
- One fix, one rebuild, one rerun, one report.
