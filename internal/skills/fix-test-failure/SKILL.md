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
2. `read_file` the test around the reported line. Write down, in one line each:
   the exact expression the failing assertion checks, and the value the test
   expects it to have at that point.
3. List every function of the code under test that the test calls before or
   inside that assertion, in call order. Use `find_definition` for each one and
   `read_file` its body. Read those functions, not the whole repository: the
   fault is almost always in one of them.
4. Trace the test by hand. For each call, in order, write the state the object
   is in and the value the function returns, using the code exactly as written,
   not as it was probably meant. Keep going until you reach the first call
   whose actual value differs from what the test expects. That line is the
   fault. Say which line, what it returns, and what it should return.
5. Decide, explicitly: is the test wrong, or is the code wrong? Almost always the
   code. Say which and why in one sentence.
6. Propose a minimal fix to the line you found with `propose_patch`, then
   `apply_patch` it. If after step 4 you still cannot see the fault, propose your
   best minimal fix to the most suspicious line anyway rather than reading more
   files: the rebuild and rerun will tell you whether you were right.
7. Rebuild with `build_target` and no target argument. A source edit is not in
   the binary until you build; rerunning the old binary tells you nothing.
8. Run `run_test` with no filter. The contract is the whole suite, not one test.
9. If it still fails, return to step 2 with the new evidence. One fix, one
   rebuild, one rerun.
10. The suite is fixed when a full unfiltered run passes against the tree as
   it stands after the last edit, and not before.

# Guardrails

- Never change a test's expected value to make it pass unless you have shown the
  expectation itself was wrong, and say so in the answer.
- Never delete, skip, disable or gut a test. A test that passes because it no
  longer asserts anything is not a fix; it is a cover-up, and it will be caught.
- Never report the suite green without a full `run_test` with exit code 0 after
  your last edit.
- One fix, one rebuild, one rerun, one report.
