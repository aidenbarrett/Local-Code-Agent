---
name: diagnose-build-failure
description: >
  Diagnoses C++ compiler and linker failures from structured build diagnostics
  and explains the cause. Read-only: it finds and explains, it does not edit.
  Use when asked to find, locate, explain or diagnose a compilation error,
  undefined reference, unresolved external symbol, or a template or overload
  resolution error, why the build is broken, or why it fails at link time
  with a missing or undefined symbol.
tier: cheap
escalation: allowed
verification:
  required: true
tools: [build_target, read_log_chunk, read_file, search_text, find_definition]
---

# Goal

Explain why the build failed, in terms of the actual code: which file, which
line, which symbol, and what is wrong with it. Stop there. This skill does not
change any file. If the task asks for a fix, that is a different skill.

# Workflow

1. Run `build_target` to get current diagnostics. Do not work from memory of an
   earlier run.
2. Take the FIRST error. Ignore the rest until it is understood; C++ diagnostics
   cascade.
3. Read the source: `read_file` with a range of roughly 15 lines either side of
   the reported line.
4. If the message names a symbol, use `find_definition` and `search_text` to
   find where it is declared and where else it is used.
5. State the cause in one or two sentences: file, line, symbol, what is wrong.
6. Stop when the cause is established. The task is the explanation, not a
   repair.

# Link errors

`undefined reference` and `unresolved external symbol` are not errors in the
file the linker names. Search for the declaration, then for a definition. The
usual causes are a missing definition, a signature that drifted from the
declaration, or a source file missing from the CMake target. Name the symbol
and say where its definition belongs.

# Guardrails

- Do not modify any file. You have no patch tools and should not want them.
- Do not run the build more than twice. The first run gives the diagnostics;
  a second is only for confirming a reading of them. Reading the source is what
  moves a diagnosis forward, not another build.
- If the cause is not clear from the evidence, say so and report what you
  found. An honest "the error is at X, cause unclear" beats a confident guess.
