---
name: implement-change
description: >
  Implements a requested source change in C++ code (a new function, a behaviour
  change, a refactor the user asked for) and proves the whole project still
  builds and its full test suite passes. Edits and creates source files.
tier: cheap
escalation: allowed
verification:
  required: true
tools: [read_file, list_files, search_text, find_definition, git_diff, propose_patch, propose_file, apply_patch, configure_project, build_target, run_test, read_log_chunk]
---

# Goal

Make exactly the change the user asked for, and prove it with a full build and a
full, unfiltered test run that pass against the tree as it stands after your
last edit.

# Workflow

1. Restate the requested change in one sentence. If it is ambiguous in a way that
   would change what you write, say so in your answer instead of guessing.
2. Find where the change belongs: `search_text`, `find_definition`, `read_file`.
   Read the surrounding code and the existing tests for that area before editing.
3. Make the smallest change that does what was asked. Use `propose_patch` for
   existing files and `propose_file` for new ones, then `apply_patch` each one.
4. If the change adds or alters behaviour, add or extend a test that exercises it,
   in the style of the existing tests. If a new test file is needed, register it
   in the build the way the existing tests are registered.
5. Build with `build_target` and no target argument. Fix what you broke.
6. Run `run_test` with no filter. The contract is the whole suite.
7. If anything fails, read the evidence, fix, rebuild, rerun. One change at a time.
8. You are done when a full unfiltered `run_test` passes after your last edit.

# Guardrails

- Do only what was asked. No drive-by reformatting, renames or tidy-ups.
- Never weaken, skip, delete or gut an existing test to make the suite pass.
- Never suppress a diagnostic to make it go away.
- Do not touch the build directory, the run directory or `.git`.
- If you cannot make the change safely, say what you found and stop. An honest
  "not done" is worth more than a confident wrong change.
