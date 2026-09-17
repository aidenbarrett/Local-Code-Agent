---
name: repo-navigation
description: >
  Finds and explains code in an unfamiliar C++ repository. Use when asked where
  something lives, what a class or function does, how a subsystem is wired
  together, how the repository builds, or to explain existing code.
tier: cheap
escalation: allowed
tools: [repo_info, list_files, read_file, search_text, find_definition]
---

# Goal

Answer questions about the repository from the repository, with file and line
references where the claim depends on file contents, in as few tool calls as the
answer allows.

# Choose the shortest workflow for the question

## Repository setup or build summary

1. Call `repo_info` first. Its configured build profile is authoritative for the
   commands Local Code Agent will actually run.
2. Use `list_files` to locate build/configuration files such as `CMakeLists.txt`,
   `.local-agent.toml`, presets, or build scripts.
3. `read_file` only the small set of files needed to explain the build structure.
4. Answer. Do not crawl every source/test file when the question is only how the
   repository builds.

## Symbol or subsystem question

1. Use `find_definition` for a named C++ symbol, or `search_text` for a content
   pattern when no plain symbol is available.
2. `read_file` with a line range around each useful hit. Do not read whole files
   speculatively.
3. Follow the language structure as needed: definition, header, callers, tests.
4. Answer with file and line for each code-dependent claim.

# Tool semantics that matter

- `list_files` searches **filenames/paths**. Use it for extensions or names such
  as `*.cpp`, `*.h`, `CMakeLists.txt`, or `*test*`.
- `search_text` searches **file contents**, not filenames. Its `pattern` is a
  regular expression applied to text inside files. Use its `glob` argument to
  restrict which filenames are searched, for example `glob="*.cpp"`.
- After a zero-match `search_text`, do not keep retrying equivalent regexes. Switch
  to `list_files`, read a known file, or broaden the content search once.

# Search patterns that pay off in C++

- Declaration: `find_definition` with the bare identifier.
- Callers: `search_text` with `\bname\s*\(` and `glob="*.cpp"`.
- Header/implementation split: use `list_files` for the stem in `*.h` / `*.cpp`,
  then read the relevant files.
- Build membership: search the file name as **content** in `CMakeLists.txt`, or
  read the relevant `CMakeLists.txt` directly.
- Intended behaviour: use `list_files` to locate tests, then search their contents
  for the type or function name.

# Guardrails

- Never describe code you have not read in this session.
- Never state a line number you have not seen in a tool result.
- If the answer is not in the repository, say that instead of inferring it from
  the name of something.
- If a search strategy returns no useful evidence twice, change strategy instead
  of spending more calls paraphrasing the same search.
