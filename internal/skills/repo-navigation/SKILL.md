---
name: repo-navigation
description: >
  Finds and explains code in an unfamiliar C++ repository. Use when asked where
  something lives, what a class or function does, how a subsystem is wired
  together, or to explain existing code.
tier: cheap
escalation: allowed
tools: [repo_info, list_files, read_file, search_text, find_definition]
---

# Goal

Answer questions about the code from the code, with file and line references, in
as few tool calls as the answer allows.

# Workflow

1. `search_text` or `find_definition` first. Locate before reading.
2. `read_file` with a line range around each hit. Do not read whole files
   speculatively.
3. Follow the structure the language already gives you, in this order: the
   symbol's definition, its header, its callers, its tests. The tests are
   usually the fastest description of intended behaviour in the repository.
4. Answer with file and line for every claim.

# Search patterns that pay off in C++

- Declaration: `find_definition` with the bare identifier.
- Callers: `search_text` with `\bname\s*\(` restricted to `*.cpp`.
- Header/implementation split: search the same stem in `*.h` and `*.cpp`.
- Build membership: `search_text` for the file name in `CMakeLists.txt`.
- Intended behaviour: search the test directory for the type's name.

# Guardrails

- Never describe code you have not read in this session.
- Never state a line number you have not seen in a tool result.
- If the answer is not in the repository, say that instead of inferring it from
  the name of something.
