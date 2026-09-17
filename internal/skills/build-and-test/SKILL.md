---
name: build-and-test
description: >
  Builds configured C++ targets and runs selected tests. Use when asked to
  compile, build, run tests, reproduce a failure, or verify that a change is
  sound.
tier: cheap
escalation: allowed
verification:
  required: true
tools: [repo_info, configure_project, build_target, list_tests, run_test, read_file, read_log_chunk]
---

# Goal

Complete the requested build or test operation and report verified evidence of
the result: the command, the exit code, and the diagnostics.

# Workflow

1. Call `repo_info` if you do not already know the profiles and permissions.
2. Never invent a build command. `build_target` and `run_test` already carry the
   configured command line.
3. Build the narrowest thing that answers the question. Pass `target` when the
   request names one.
4. If the build fails, stop building and hand over to `diagnose-build-failure`.
5. If the build succeeds, run the tests. Pass `name_filter` when the request
   names a specific test, rather than running the whole suite.
6. If a test fails, hand over to `diagnose-test-failure`.
7. Report: command, exit code, error count, warning count, failing test names.

# Reading logs

Tool results are already reduced. Only call `read_log_chunk` when you need the
lines around a diagnostic that the summary did not give you. Use the `log_line`
field of a diagnostic as the centre of a small window, typically 20 lines each
side.

# Guardrails

- Never report success without an exit code of 0 in a tool result.
- Never guess at test names. Use `list_tests`.
- Never delete or clean the build directory. There is no tool for it.
- If a run times out, say so explicitly. A timeout is not a failure diagnosis.
