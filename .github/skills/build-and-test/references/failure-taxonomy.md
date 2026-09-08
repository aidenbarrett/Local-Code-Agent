# Failure taxonomy

Classify before investigating. The class determines which evidence is worth
gathering, and gathering the wrong evidence is how a small model burns its
whole context.

| Class | Signal | First move |
|---|---|---|
| configure failure | `CMake Error` lines, no compiler diagnostics | read the configure log, check the profile |
| compile error | `error:` with file and line | read the source around the line |
| link error | `undefined reference`, `LNK2019` | find the declaration, then look for the missing definition or library |
| test assertion | `Assertion ... failed`, `(Failed)` in ctest | read the test body, then the code under test |
| crash | `SIGSEGV`, `Segmentation fault`, sanitizer output | look for lifetime, bounds or null dereference near the last frame |
| timeout | `(Timeout)`, killed after the configured limit | look for an unbounded loop or a wait with no deadline |
| flake | passes on rerun with no change | do not "fix" it; report it as non-deterministic with both run ids |

## Ordering rule

Fix the first compile error before looking at the rest. C++ error cascades:
diagnostics two through twenty are frequently artefacts of the first one.

## Link errors specifically

A link error is almost never in the file the linker names. Look for:

- a function declared in a header and never defined
- a definition whose signature drifted from the declaration (const, reference,
  namespace, template arguments)
- a source file missing from the CMake target
- an `inline` or `static` specifier that changed linkage
