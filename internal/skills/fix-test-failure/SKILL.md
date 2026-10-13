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
3. Start with the function that produces the value in the failing assertion.
   Use `find_definition` with its required `symbol` argument, then `read_file`
   the matching implementation. If there are several definitions, use the
   object's type and surrounding declaration to select the relevant one.
   Inspect that return value before reading setup functions or their callers.
4. Trace that implementation using the code exactly as written, not as it was
   probably meant. Compare its return value with the assertion's observed and
   expected values. Follow the inputs or state-changing calls only when needed
   to explain that difference. Do not trace every earlier call in the test.
   Identify the faulty line, its actual behavior and the expected behavior.
5. Decide, explicitly: is the test wrong, or is the code wrong? Almost always the
   code. Say which and why in one sentence.
6. Propose a minimal fix to the identified fault with `propose_patch`, inspect
   the returned diff, then `apply_patch` it. If the cause is still unresolved,
   state which observation is missing; do not invent an edit to unrelated code.
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
