---
name: fix-build-failure
description: >
  Fixes C++ compilation and link errors and proves the build succeeds. Use when
  asked to fix, repair or make the build compile, resolve compilation errors,
  or get the build green. Edits source, rebuilds, and reports the verified
  result.
tier: cheap
escalation: allowed
verification:
  required: true
tools: [build_target, read_log_chunk, read_file, search_text, find_definition, propose_patch, apply_patch]
---

# Goal

Make the build succeed with the smallest change that fixes the cause, and prove
it with a full build that passes.

# Workflow

1. Run `build_target` to get current diagnostics. Do not work from memory of an
   earlier run.
2. Take the FIRST error. Ignore the rest until it is resolved; C++ diagnostics
   cascade.
3. Read the source: `read_file` with a range of roughly 15 lines either side of
   the reported line.
4. If the message names a symbol, use `find_definition` and `search_text` to
   find where it is declared and where else it is used.
5. State the cause in one sentence before proposing anything.
6. Propose a minimal fix with `propose_patch`, then `apply_patch` it. Fix the
   cause, not the symptom. Do not reformat, rename, or "tidy" surrounding code.
7. Rebuild with `build_target` and no target argument. A build of one target
   proves that target only; the contract is that the project builds.
8. If a new first error appears, return to step 2. One fix, one rebuild, repeat.
9. The project is fixed when a full build passes against the tree as it
   stands after the last edit, and not before.

# Link errors

`undefined reference` and `unresolved external symbol` are not errors in the
file the linker names. Search for the declaration, then for a definition. The
usual causes are a missing definition, a signature that drifted from the
declaration, or a source file missing from the CMake target.

# Guardrails

- One fix at a time. Rebuild between fixes. Never build before applying the
  patch you just proposed; the build will only show you the same error.
- Never suppress a diagnostic to make it go away: no casts to silence a real
  type error, no `#pragma` to hide a warning, no removing a `static_assert`.
- Never widen an interface to fix a caller unless the request asked for it.
- Never edit a test to make the build pass.
- If the fix is not obvious from the evidence, say so and report what you found.
  A wrong confident patch costs more than an honest "I need more context".
