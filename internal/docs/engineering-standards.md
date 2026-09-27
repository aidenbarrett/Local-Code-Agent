# Engineering standards

Status: binding for every change to `main`. This document is the one owner of the
language standards. `AGENTS.md` points here; nothing else restates them.

The bar is the C++ lineage: **Stroustrup** (type-safe abstraction at zero cost, the Core
Guidelines), **Stepanov** (generic code written against precise requirements), **Meyers**
(interfaces that are easy to use correctly and hard to use incorrectly), **Alexandrescu**
(policy-based design, making illegal states unrepresentable at compile time) and
**Sutter** (exception safety, correct concurrency, "correct first, then fast"). Python
and Rust code meets the equivalent bar in its own language.

## The rule that decides most arguments

**Prove it before it runs; check it where it enters.** Push every guarantee that can be
known before execution into the type system, the compiler or a static checker. What can
only be known at runtime (model output, files, sockets, environment, other processes) is
validated once, at the trust boundary where it enters, into a typed value. Past that
boundary, code relies on the type and does not re-check.

"Everything compile time" therefore means: no guarantee is left to runtime that a type,
`constexpr`, `Final`, an exhaustive match or a checker could have given. It does not mean
deleting boundary validation. A model can emit any bytes; a type annotation does not
stop it.

A rule without a check is a wish. Every rule below names the check that enforces it; a
rule marked **(review)** has no mechanical check yet and is enforced in review until one
exists.

## Shared rules (every language)

1. **One owner per decision.** One type, one validator, one authority per fact. See
   `AGENTS.md`. (review)
2. **Make illegal states unrepresentable.** Closed sets are enums, not strings. States
   that cannot coexist are distinct types or variants, not a struct of optionals.
   (mypy `strict`, exhaustiveness; C++ `std::variant`; Rust enums)
3. **Immutable by default.** Mutation is the exception and is local. (Python frozen
   dataclasses and `Final`; C++ `const`/`constexpr`; Rust default)
4. **No silent failure.** Errors carry a typed reason and propagate or are handled with
   intent. No blanket catch that swallows. (ruff `BLE`, `S110`; C++ `[[nodiscard]]`,
   `bugprone-*`; Rust `#[must_use]`, `clippy::unwrap_used` outside tests)
5. **No injection surface.** Processes are started from argv lists, never a shell
   string. No `eval`, no unsafe deserialisation of untrusted data, no string-built SQL.
   (ruff `S`; clang-tidy `cert-*`; Rust `cargo-deny`)
6. **Resources are owned.** Every file, process, lock, handle and temporary directory has
   exactly one owner that releases it on every path, including failure. (Python context
   managers; C++ RAII, no owning raw pointers; Rust `Drop`)
7. **Speed from the design, not from tricks.** Pick the right complexity first; do not
   copy what can be borrowed or viewed; bound every loop over external input. Measure
   before micro-optimising, with the harness in `internal/perf/`. (review)
8. **Concurrency is structured.** Shared mutable state is guarded by one named lock or
   not shared. No data race is acceptable, "benign" or not. (TSan for C++; `Send`/`Sync`
   for Rust; review for Python)

## Python

Authorities: the typing specification (PEPs 484, 544, 591, 604, 681, 695), mypy strict
mode, and the Core Guidelines translated where Python has an equivalent.

| Rule | Equivalent of | Check |
|---|---|---|
| Every function is fully annotated; no implicit `Any` | Stroustrup: statically type-safe interfaces | mypy `strict` |
| `Optional` is handled before use | Core Guidelines: never dereference a possibly-null pointer | mypy `union-attr`, `index` |
| `Any` does not leak past the boundary that produced it | Stroustrup: no type-unsafe casts | mypy `no-any-return`, `warn_return_any` |
| Closed sets are `Enum`/`Literal`; branches over them end in `assert_never` | Alexandrescu: compile-time exhaustiveness | mypy `strict`, `warn_unreachable` |
| Outcomes that carry different data are distinct types in a union, matched exhaustively; never one class of flags and optionals | Alexandrescu: illegal states unrepresentable | mypy `assert_never`, `union-attr` |
| Boundary validators take `object` (or `Mapping[str, object]`) and return the typed value; an `isinstance` check on an already-typed parameter is dead code | Stroustrup: validate at the interface, trust the type inside | mypy `redundant-expr`, `unreachable` |
| Platform-specific code is guarded by `sys.platform` (which the checker understands), not `os.name` | compile-time platform selection, like `#if` | mypy on `linux` and `win32` |
| Value types are `@dataclass(frozen=True, slots=True)`; constants are `Final` | Meyers: prefer `const`; Stroustrup: value semantics | mypy (`Final` reassignment), review |
| Generic code is written against a `Protocol`, not a concrete class | Stepanov: concepts before algorithms | mypy, review |
| No boolean positional parameters; use keyword-only or an enum | Meyers: interfaces hard to use incorrectly | ruff `FBT001`, `FBT002` |
| No blind `except`; no `except: pass` | Sutter: handle or propagate, never swallow | ruff `BLE`, `S110` |
| `assert` is never a runtime check (removed under `-O`) | Core Guidelines I.6/E.12: express preconditions in the contract | ruff `S101` |
| `subprocess` with argv lists and explicit `check`/return-code handling | CERT: no command injection | ruff `S6xx`, `PLW1510` |
| Mutable defaults and loop-variable capture are banned | Meyers: avoid surprising semantics | ruff `B006`, `B023` |
| Timezone-aware datetimes only | correctness at the boundary | ruff `DTZ` |
| Function size and complexity budgets | Sutter/Stroustrup: small, single-purpose functions | ruff `PLR0911`, `PLR0912`, `PLR0913`, `PLR0915` |
| `# type: ignore[code]` and `# noqa: CODE` name the code and give the reason on the line | honesty about exceptions | mypy `ignore-without-code`, ruff `RUF100`, review |

Configuration lives in `pyproject.toml` (`[tool.mypy]`, `[tool.ruff]`). Checker versions
are pinned in the `lint` extra because a different version reports different findings.
mypy runs for both `linux` and `win32` so platform-guarded code is checked on both sides.
Third-party packages are treated as opaque (`Any`) so results are identical on every
machine; values from them are validated where they enter our code.

## C++

Authorities: the C++ Core Guidelines (Stroustrup, Sutter), *Elements of Programming* and
the STL's concept-first design (Stepanov), *Effective Modern C++* (Meyers), *Modern C++
Design* (Alexandrescu), *Exceptional C++* and the GotW series (Sutter), and the CERT C++
rules for security.

- **Standard:** C++20 or later. Constraints are expressed as `concept`s, not SFINAE or
  comments.
- **Compile time first:** `constexpr`/`consteval` for anything computable before run;
  `static_assert` for every layout, size and ABI assumption; `enum class` over integers;
  strong types over bare `int`/`std::string` for identifiers and units.
- **Ownership:** RAII for every resource. No owning raw pointers, no naked `new`/`delete`
  (`std::unique_ptr`, containers, `std::span`/`std::string_view` for non-owning views).
- **Interfaces:** `[[nodiscard]]` on every function whose result matters, `noexcept` where
  it is true, `const` by default, explicit single-argument constructors, rule of zero.
- **Errors:** exceptions or `std::expected` at a documented boundary; never both styles
  in one interface. Exception-safety guarantee stated for every mutating operation.
- **Plugin/ABI boundaries** are `extern "C"`, versioned, fixed-width and
  `static_assert`ed; no C++ types or exceptions cross them.
- **Build gates (required CI):** `-Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror`
  (MSVC `/W4 /WX /permissive-`); clang-tidy with `cppcoreguidelines-*`, `bugprone-*`,
  `performance-*`, `cert-*`, `modernize-*` and warnings as errors; ASan+UBSan and TSan
  test runs.
- **Hardening:** `-D_FORTIFY_SOURCE=3`, `-D_GLIBCXX_ASSERTIONS`, `-fstack-protector-strong`,
  `-fstack-clash-protection`, PIE and full RELRO (`-Wl,-z,relro,-z,now`); MSVC
  `/guard:cf /sdl /DYNAMICBASE /HIGHENTROPYVA`.
- **Speed:** move rather than copy, reserve before growth, no allocation in hot loops
  that a view avoids; changes to a hot path come with a harness measurement.

Scope: product C++ only. `internal/benchmark_fixture/` holds deliberately broken sample
projects that the agent is asked to repair; they are test data and exempt.

Where it is enforced today: `internal/native_endpoint/`.
- `CMakeLists.txt` (`lca_warnings`, `lca_hardening`) sets the flags above.
- Its CI (`native-endpoint.yml`) builds with warnings as errors, including the sanitizer
  builds, and runs clang-tidy over every compiled `src/` file with
  `internal/native_endpoint/.clang-tidy`.
- Every disabled check and every `NOLINT` names its reason.
- The C plugin ABI header (`include/lca/`) stays plain C11 and is exempt from the C++
  modernisation checks.
- Tests are held to the compiler and sanitizer gates, not clang-tidy.
- Anything a plugin writes into a buffer the endpoint owns is read back bounded by that
  buffer's size. A plugin is not trusted to terminate its strings.

## Rust

There is no Rust in the product yet. The first crate lands with these gates in the same
PR, or it does not land.

Authorities: the Rust API Guidelines, the Rustonomicon (for why `unsafe` is avoided),
Clippy's pedantic lint set and the RustSec advisory database.

- `#![forbid(unsafe_code)]` in every crate. An FFI crate that must use `unsafe` confines
  it to one module, documents each invariant with a `// SAFETY:` comment and runs under
  Miri in CI.
- `cargo clippy --all-targets -- -D warnings -D clippy::pedantic`, with
  `clippy::unwrap_used` and `clippy::expect_used` denied outside tests.
- `cargo fmt --check`; `cargo deny check` (advisories, licences, bans, sources);
  `cargo audit`.
- Newtypes for identifiers and units; `#[must_use]` on results that matter; errors via
  typed enums (`thiserror`), never `String`.
- Release profile keeps `overflow-checks = true` and sets `panic = "abort"` only where
  unwinding cannot cross an FFI boundary.

## Existing code: the ratchet

`main` did not start at this bar. The findings that existed when this standard landed
(strict mypy on two platforms plus the ruff rule set, about 1,100) are recorded per
checker, file and code in `internal/static-standards-baseline.json`; its `total` is the
current debt.

`internal/devtools/check_static_standards.py` runs in the fast gates on every PR:

- a file may not gain a finding, and trading one code for another is still a gain;
- a file not in the baseline must be clean, so **new code meets the full standard from
  its first commit**;
- when findings are fixed the gate fails until the baseline is lowered with
  `python internal/devtools/check_static_standards.py --update`, so fixed debt cannot
  return;
- `--update` never raises a count. Raising one is a hand-edited baseline change that has
  to survive review.
- counts belong to the whole tree, so a PR is only mergeable on a green run against the
  current `main`. After another PR merges, rebase and rerun before merging; a green run
  against an older base proves nothing about the combination.

Burn-down is done file by file, owner by owner, with the same behavioural test discipline
as any other change: fixing a type error that hid a bug gets a regression test for the
bug.

Run it locally (Linux or WSL, from the repository root):

```text
python -m pip install -e ".[dev,lint]"
python internal/devtools/check_static_standards.py
```
