# UX-first root refactor plan

## Purpose

This refactor exists to make Local Code Agent feel like a product when somebody opens the repository for the first time.

The first impression is now a first-class engineering requirement.

This branch is **demo-focused and UX-first**. A new user should not need to understand the research harness, experiment layout, measurement internals, package structure, or provenance machinery before they can install the project, talk to a model, run Local Code Agent, or launch a demo.

The target experience is deliberately simple:

```text
install.ps1
chat.ps1
local-code-agent.ps1
README.md
QUICKSTART.md
demo/
internal/
```

The visible root should communicate:

1. install it
2. chat with a local model
3. use Local Code Agent
4. run a demo
5. read the quickstart if needed

Everything else is implementation detail.

This is not cosmetic tidying. The repository itself is part of the demo. The first `dir`, the first command, the first error message, and the first successful interaction all contribute to whether the system feels deliberate, understandable and impressive.

## Non-negotiable UX principles

### 1. First impressions are the first priority

If a first-time user has to ask what a root-level directory is for, the root is too noisy.

If a first-time user has to know an internal Python path to start the product, the facade has failed.

If the first command produces a warning, a stack trace, a raw implementation path, a research term, or a dead-end instruction, that is a demo defect even if the underlying code is technically correct.

### 2. One obvious path for each user intent

The user-facing surface should be:

```powershell
.\install.ps1
.\chat.ps1
.\local-code-agent.ps1
.\demo\...
```

A normal user should not be sent directly to `measurement/`, `evaluation/`, `scripts/`, `local_agent/`, or another internal directory.

### 3. Human-facing output is product output

Terminal output should use plain language.

Prefer:

- `OpenVINO Model Server`
- `Model server ready`
- `Agent connection verified`
- `C++ build/test passed`
- `Time to first token`
- `Generation speed`

Avoid exposing internal terms unless they genuinely help the user:

- `OVMS` without expansion
- `fixture`
- `qualification`
- `typed registry`
- `harness`
- `generation 2`
- raw implementation paths

### 4. The user should never have to repair our orchestration manually

If `chat.ps1` requires a compatible local server, chat should either start it automatically through the existing deterministic serving controller or present one clear action that the root facade owns.

The current behaviour is not acceptable for the demo: `chat.ps1 qwen3-8b-npu` reports that no server is running and tells the user to invoke `scripts\demo-accelerator.ps1` manually. That makes the headline chat path depend on knowing an internal demo script.

### 5. Deterministic control remains non-negotiable

UX simplification must not weaken the architecture.

The controller still owns:

- server configuration
- policy
- approved tools
- verification
- experimental state
- evidence
- escalation decisions

Wrappers may simplify the interface. They must not create a second source of truth.

## Current chat defect: fix before judging the refactor

The current screenshot exposes two separate problems.

### A. Python `SyntaxWarning`

`scripts/chat.py` contains a normal Python docstring with Windows examples such as:

```text
.\chat.ps1
```

Python interprets the backslash sequence and emits:

```text
SyntaxWarning: invalid escape sequence '\c'
```

This warning appears before the product UI and immediately damages first impression quality.

**Status on this branch:** the known warning sites in `scripts/chat.py` and `scripts/capabilities.py` have been converted to raw docstrings. A repository-wide regression test now compiles every active Python file with `SyntaxWarning` promoted to an error. Frozen historical files under `experiments/` are deliberately excluded from mutation and from the active-source gate.

The warning-cleanliness test is a permanent acceptance criterion for this refactor. Any newly introduced invalid escape sequence or other Python `SyntaxWarning` in active code must fail CI before it can reach a user-facing terminal.

### B. Chat does not provide a one-command experience

Current behaviour:

```powershell
.\chat.ps1 qwen3-8b-npu
```

can end with:

```text
Model server not running
Start one with:
.\scripts\demo-accelerator.ps1 -Device NPU -Seconds 5 -KeepServer
```

That is not a finished user journey.

Target behaviour for the demo:

```powershell
.\chat.ps1 qwen3-8b-npu
```

should be sufficient.

If the required server is already running, reuse it.

If no compatible server is running, start the selected model/device through the **existing serving controller**, wait for readiness, then enter chat.

Do not duplicate serving policy in `chat.py` or `chat.ps1`. Reuse the existing profile/model/device resolution and serving controller path.

If automatic startup cannot be completed safely, the fallback must still remain root-level and obvious, for example:

```powershell
.\local-code-agent.ps1 start-model qwen3-8b-npu
```

The user should never be told to understand or invoke an internal script as part of the normal flow.

## Desired final root

The intended human-facing root is:

```text
install.ps1
chat.ps1
local-code-agent.ps1
README.md
QUICKSTART.md
demo/
internal/
```

Repository metadata such as `.gitignore`, `.gitattributes`, `.github/`, `pyproject.toml` and the instrument identity file may remain where required by tooling, but the visible product surface should be dominated by the files above rather than implementation directories.

## Internal layout

The current implementation-oriented directories should move beneath one clearly non-user-facing directory:

```text
internal/
    local_agent/
    measurement/
    evaluation/
    scripts/
    tests/
    experiments/
    benchmark_fixture/
    docs/
    skills/
```

This exact shape may be adjusted if Python packaging or frozen-evidence constraints require it, but the UX goal does not change: one implementation container instead of nine unrelated root directories.

## Critical provenance warning

This move is **not** a cosmetic rename from the experiment instrument's perspective.

The current provenance surface includes behaviour-affecting files under paths such as:

```text
local_agent/**
measurement/**
evaluation/**
benchmark_fixture/**
skills/**
```

Moving those files changes paths and therefore must be treated as an intentional instrument/source-generation change.

Do not move them and accidentally leave provenance globs pointing at old locations. That could cause behaviour-affecting code to fall outside the measured source surface while the identity still appears valid.

The refactor branch must therefore explicitly:

1. update provenance path definitions for the new layout
2. update `INSTRUMENT.json` in the same coherent change
3. recompute all current identities
4. prove the new source identity covers every behaviour-affecting file
5. preserve frozen historical experiment content exactly
6. never rewrite or reinterpret generation-1 evidence using the new paths

Historical tags remain the authoritative way to reproduce historical layouts.

## Frozen experiment handling

`experiments/` contains frozen evidence and must not be silently transformed.

If it is moved under `internal/experiments/`:

- file bytes must remain unchanged
- frozen artifact hashes must remain unchanged where hashes are content-based
- documentation must clearly state that the relocation is repository organisation only
- historical tags must remain untouched
- any code that discovers frozen artifacts by path must be updated and regression-tested
- no old result may be re-scored or re-labelled merely because the directory moved

If these invariants cannot be guaranteed cheaply, leave frozen experiment artifacts at their current path for this refactor and accept one additional root directory rather than damaging provenance.

UX is the priority, but reproducibility still wins over cosmetic purity.

## `demo/` design

`demo/` is the public demonstration surface.

It should contain long, explicit names that explain value without prior repo knowledge.

Recommended initial files:

```text
demo/
    run-qwen-on-npu.ps1
    run-qwen-on-gpu.ps1
    run-qwen-on-cpu.ps1
    show-stale-test-rejection.ps1
    run-complete-local-code-agent-demo.ps1
```

These should be thin wrappers around known-good implementation commands.

Do not move or rewrite validated execution logic merely to make the folder look cleaner.

The wrapper owns presentation and discoverability. The existing controller owns execution.

### Demo naming rule

Names must answer **why would I run this?**

Avoid vague names such as:

```text
agent.ps1
trust.ps1
accelerator.ps1
full.ps1
```

Prefer names that require no explanation.

## `install.ps1` design

`install.ps1` should be a thin, friendly root entrypoint over the existing workstation preparation logic.

It must not become a second installer stack.

Expected behaviour:

```powershell
.\install.ps1
```

should:

1. explain what it is going to validate or install
2. reuse the existing deterministic/bootstrap implementation
3. print human-readable progress
4. finish with a simple readiness summary
5. point directly to the next user action

The success path should end with something like:

```text
LOCAL CODE AGENT

Installation and system validation complete.

Next:
  .\chat.ps1 qwen3-8b-npu
  .\local-code-agent.ps1 capabilities
  .\demo\run-qwen-on-npu.ps1
```

Do not expose a giant internal experiment command on the installation success screen.

## `chat.ps1` design

`chat.ps1` is a headline capability, not a helper script.

The first-time experience must be boringly obvious.

```powershell
.\chat.ps1
```

lists friendly model choices.

```powershell
.\chat.ps1 qwen3-8b-npu
```

starts or reuses the NPU model server and enters chat.

The user should see:

```text
LOCAL CODE AGENT
Local Model Chat

Model    Qwen3-8B (INT4)
Device   NPU
Backend  OpenVINO Model Server
Status   Ready

You >
```

No Python warnings. No internal paths. No manual serving command. No stack trace.

## `local-code-agent.ps1` design

The root agent entrypoint must remain the one obvious route into controlled repository work.

With no arguments it should explain the distinction succinctly:

```text
Chat gives direct access to the model.
Local Code Agent gives the model controlled repository access,
restricted tools, procedural skills and independent verification.
```

User-facing commands should favour explicit names and plain language.

The facade may translate these names into existing internal CLI commands. It must not duplicate controller policy.

## Root-directory cleanup strategy

The reorganisation must be implemented in stages on this branch.

### Phase 0: remove demo-warning noise

Before any structural move:

- eliminate Python `SyntaxWarning` output from user-facing scripts
- add the repository-wide active-Python warning-cleanliness regression
- keep frozen historical experiment files unchanged

This creates a clean baseline before path churn begins.

### Phase 1: additive facade

Before moving any current implementation path:

- add `install.ps1`
- add `demo/` wrappers
- make root commands work end-to-end
- make chat one-command
- add regression tests
- physically rehearse on the Panther Lake workstation

The old paths remain intact during this phase.

This gives us a known-good UX before structural churn.

### Phase 2: internal relocation on branch only

Move implementation directories beneath `internal/` in one coherent change or a small sequence of tightly controlled commits.

Update:

- Python imports / package discovery
- root wrappers
- PowerShell and shell scripts
- test paths
- CI workflow paths
- docs
- fixture paths
- skill discovery
- measurement paths
- packaging configuration
- provenance globs
- instrument identity
- any hardcoded repository-root assumptions

Do not merge halfway through this phase.

### Phase 3: exhaustive verification

Run all available validation before considering merge.

At minimum:

- Linux pytest
- native Windows pytest
- serving CI
- warning-cleanliness gate
- provenance identity checks
- historical/frozen artifact integrity checks
- root-command smoke tests
- PowerShell wrapper tests
- `chat.ps1` list
- `chat.ps1 qwen3-8b-npu`
- NPU accelerator demo
- GPU accelerator demo if practical
- CPU accelerator demo if practical
- capabilities
- trust-boundary demo
- workstation one-shot validation

No result is called verified until the relevant checks are actually green.

### Phase 4: physical first-impression rehearsal

On the real Windows Panther Lake machine:

1. clone/switch to the refactor branch
2. run `dir`
3. judge the root visually before reading docs
4. run `README` / `QUICKSTART` flow exactly as written
5. run `install.ps1` or its check-only form as appropriate
6. run `chat.ps1`
7. start NPU chat from a cold server state
8. ask one normal question
9. exit cleanly
10. run Local Code Agent capabilities
11. run a controlled repository task
12. run the NPU demo
13. run stale-test rejection
14. inspect every visible warning, path, stack trace, internal term or unnecessary implementation detail

Anything embarrassing in those first minutes is a release blocker for the demo branch.

## Warning-cleanliness gate

The repository now treats Python warning noise as a first-impression defect.

The active Python tree must compile with `SyntaxWarning` promoted to an error. This specifically catches Windows-path literals such as `\c`, `\l` and similar invalid escape sequences before they leak into PowerShell output.

The check excludes frozen historical artifacts under `experiments/` because historical experiment bytes must not be silently repaired with newer instrumentation. If a historical file contains a warning, preserve it and reproduce it through its historical tag rather than mutating evidence.

This warning-cleanliness gate should remain after the root refactor and should move with the active test suite if tests relocate under `internal/`.

## Regression expectations

Every changed contract gets a regression test.

Examples:

- root chat script has no invalid escape warnings
- root capabilities command has no invalid escape warnings
- all active `.py` files compile warning-clean
- root chat starts or reuses the requested server
- chat never instructs normal users to invoke an internal path
- `demo/` wrappers point at the correct implementation
- root entrypoints survive the internal move
- provenance includes all moved behaviour-affecting files
- frozen experiment bytes are unchanged
- README and QUICKSTART commands all exist
- Windows and Linux path handling remain valid

## First-impression acceptance checklist

The branch is not ready to merge until a new user can open the repo and answer these questions without help:

- How do I install it?
- How do I talk to the local model?
- How do I use the coding agent?
- How do I see what it can do?
- How do I run the NPU demo?
- How do I see independent verification?

And the answers must all begin from root-level files or the `demo/` directory.

There must be no:

- warning before the product output
- unexplained internal path
- dead command
- stack trace on a normal failure
- prerequisite hidden until after failure
- raw research terminology in the first-run path
- required manual server choreography
- ambiguous demo filename

## Explicit non-goals

Do not use this refactor as an excuse to:

- alter frozen experiment semantics
- re-score historical runs
- broaden model tool access
- add arbitrary shell access
- build a new serving framework
- replace the deterministic controller
- invent a second configuration system
- change benchmark methodology
- claim performance results that were not measured

## Merge rule

This branch should stay isolated until the whole first-run experience has been physically rehearsed.

Do not merge merely because imports compile and CI is green.

For this refactor, **UX is part of correctness**.

The merge bar is:

1. deterministic/instrument integrity preserved
2. regression tests green
3. authoritative CI green
4. physical Windows workstation flow green
5. no warning or internal-path leakage in first-run output
6. root directory makes immediate sense to somebody who knows nothing about the repository
7. demo looks intentional and impressive

If the system is technically correct but looks confusing in the first five minutes, the refactor is not finished.
