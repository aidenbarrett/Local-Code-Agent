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

Fix it by making the docstring raw or escaping the backslashes correctly. Add a regression test that launches/imports the script with warnings enabled and proves no `SyntaxWarning` is emitted.

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
2. use the existing bootstrap/one-shot implementation underneath
3. fail with plain-language remediation
4. finish with a concise readiness summary
5. tell the user the next useful command, preferably `chat.ps1`

Advanced switches can forward to the existing bootstrap implementation rather than duplicating it.

## `chat.ps1` target UX

The headline first-use path should be:

```powershell
.\chat.ps1
```

which lists choices cleanly, followed by:

```powershell
.\chat.ps1 qwen3-8b-npu
```

which should result in a conversation without requiring the user to launch a separate internal server command first.

Desired flow:

```text
LOCAL CODE AGENT
Local Model Chat

Model   Qwen3-8B (INT4)
Device  NPU
Status  Starting model server...

Model server ready

You >
```

If startup takes time, show meaningful progress rather than silence or raw controller logs.

The user-facing model names remain the stable abstraction:

```text
qwen3-8b-npu
qwen3-8b-gpu
qwen3-8b-cpu
qwen3-coder-30b
```

Do not expose profile names such as `ptl-npu-8b` unless verbose/advanced output is requested.

## `local-code-agent.ps1` target UX

Running the root command with no arguments should explain, in plain English:

- what Local Code Agent adds beyond chat
- what it can currently do
- what it cannot do
- the next three or four useful commands

A user should not need argparse knowledge or implementation vocabulary.

Keep the current distinction clear:

> Chat gives direct access to the model. Local Code Agent gives the model controlled repository access, approved tools, procedural skills and independent verification.

## README and QUICKSTART requirements

After the physical move, both documents must be audited against the new tree.

No command in either file may reference an old internal path unless it is explicitly labelled advanced/internal.

README should stay capability-first.

QUICKSTART should preserve the simple progression:

```text
install / validate
-> understand capabilities
-> chat with Qwen
-> demonstrate NPU/GPU/CPU execution
-> demonstrate independent verification
```

## Packaging strategy

The Python import name `local_agent` does not need to change merely because the source directory moves.

Prefer preserving the public Python package name while changing its repository location.

Options to evaluate on the refactor branch:

1. configure setuptools package discovery from `internal/`
2. add the internal source root to the controlled script environment
3. use a conventional package-dir mapping while keeping imports as `local_agent.*`

Do **not** perform a repo-wide import rename unless there is a compelling technical reason.

The goal is repository presentation, not a Python namespace redesign.

## Migration phases

### Phase 0: freeze a before-state

Before moving anything:

- record the branch base SHA
- record current source/base-prompt/outcome-contract identities
- run the authoritative test suite on Linux and native Windows where available
- run the root entrypoints
- capture current demo output
- capture current root directory listing

This is the behaviour baseline.

### Phase 1: fix the headline chat path first

Before undertaking the large path move:

- remove the Python `SyntaxWarning`
- make `chat.ps1 qwen3-8b-npu` a complete first-use path
- add regression coverage
- physically run it on the Panther Lake workstation

Do not bury a broken headline feature beneath a repository-layout refactor.

### Phase 2: add public root facades

Add or finish:

```text
install.ps1
chat.ps1
local-code-agent.ps1
demo/
```

Validate these while the internals are still in their old locations.

This separates facade defects from path-migration defects.

### Phase 3: move implementation directories

Move one logical group at a time, using Git-aware moves so history remains readable.

Suggested order:

1. `scripts/`
2. `tests/`
3. `docs/`
4. `measurement/`
5. `evaluation/`
6. `benchmark_fixture/`
7. `skills/`
8. `local_agent/`
9. `experiments/` only after frozen-artifact handling is explicitly proven safe

After each group:

- update references
- run targeted regressions
- check CLI/root entrypoints
- check source identity coverage

Do not do one giant blind move followed by mass search-and-replace.

### Phase 4: update packaging and provenance

Once paths are final:

- update `pyproject.toml`
- update package discovery
- update provenance globs
- update `INSTRUMENT.json`
- update tests that assert source coverage
- recompute identities
- document that this is a new source generation

### Phase 5: documentation sweep

Search all Markdown, PowerShell, Python and CI files for old root paths.

Every command in README/QUICKSTART must be physically rehearsed from a fresh checkout of the branch.

### Phase 6: final demo rehearsal

A stranger should be able to clone the branch and understand the project from the root without verbal coaching.

Run the exact demo journey below.

## Required demo rehearsal

### First screen

Run:

```powershell
dir
```

The result should look intentional and small.

The user should immediately see:

```text
install.ps1
chat.ps1
local-code-agent.ps1
README.md
QUICKSTART.md
demo/
```

One implementation container such as `internal/` is acceptable. Nine implementation directories are not.

### Install / readiness

```powershell
.\install.ps1
```

Expected result:

- no Python warnings
- no unexplained internal paths
- no research jargon in the main success screen
- clear PASS/READY result
- next command is obvious

### Direct chat

```powershell
.\chat.ps1 qwen3-8b-npu
```

Expected result:

- no `SyntaxWarning`
- no requirement to start `scripts/demo-accelerator.ps1`
- selected model/device shown clearly
- server starts or is reused automatically
- user reaches `You >`
- normal model answer appears
- useful latency/generation metrics appear after the reply

### Model/device demonstration

```powershell
.\demo\run-qwen-on-npu.ps1
```

Expected result:

- polished LCA banner
- human model/backend labels
- requested and confirmed device
- cold/warm request explanation
- no benchmark overclaim

Repeat GPU and CPU versions.

### Controlled agent

```powershell
.\local-code-agent.ps1
.\local-code-agent.ps1 capabilities
```

Expected result:

- difference between chat and agent is obvious
- capabilities are plain language
- restrictions are visible and confidence-building

### Trust boundary

```powershell
.\demo\show-stale-test-rejection.ps1
```

Expected result:

- user understands that raw passing tests can be stale
- user understands Local Code Agent rejects stale evidence independently
- no internal Python path is required

## Regression requirements

This refactor is not complete without tests that protect the product surface.

Add regressions for at least:

- root wrappers exist
- user-facing README/QUICKSTART commands resolve
- `chat.py` emits no `SyntaxWarning`
- friendly chat names resolve to real profiles/devices
- chat startup uses the existing serving controller
- root wrappers do not duplicate model/server configuration
- `demo/` wrappers point to real known-good implementations
- package discovery works after internal relocation
- all current tool and skill discovery still works
- provenance includes all moved behaviour-affecting files
- historical frozen experiment bytes/hashes remain unchanged
- current source identity matches the new instrument declaration
- Linux pytest
- native Windows pytest
- serving tests
- physical Panther Lake chat and NPU demo

## CI and verification gate

Do not call the refactor verified because imports work on one machine.

Before merge, require:

- authoritative Linux pytest green
- authoritative native Windows pytest green
- serving job green
- source identity tests green
- root facade regression tests green
- physical workstation `install.ps1` or validation path green
- physical `chat.ps1 qwen3-8b-npu` successful conversation
- NPU/GPU/CPU demo paths still callable
- trust-boundary demo green

If CI is unavailable, say exactly what was and was not verified. Do not infer green.

## Merge strategy

Do all physical reorganisation on this dedicated branch.

Do not merge partial path moves into `main` just to make progress.

The branch should reach a coherent state where:

- root UX is complete
- package/import paths are coherent
- provenance is coherent
- docs are coherent
- tests are coherent
- demo journey is coherent

Then merge once.

If the branch becomes unstable, `main` remains the known-good demo path.

## Explicit non-goals

This refactor must not become an excuse to:

- redesign the orchestrator
- change experiment semantics
- alter task contracts
- re-score frozen evidence
- add arbitrary shell access
- introduce a second serving architecture
- build a massive installer framework
- redesign model profiles
- implement the future backend qualification framework

Those are separate projects.

## Definition of done

The refactor is done when a first-time user can open the repository and, without coaching:

1. understand what to run
2. install/validate the workstation
3. start a direct local-model chat with one command
4. understand the difference between chat and Local Code Agent
5. run the hardware demo
6. run the independent-verification demo
7. never need to know where the internal implementation lives

And the engineering project still retains:

- deterministic verification
- restricted tool policy
- reproducible source identity
- frozen historical evidence
- current tests
- existing known-good model serving behaviour

## Final standard

The bar for this branch is not merely "nothing broke".

The bar is:

> **A stranger opens the repository and immediately feels that Local Code Agent is a deliberate, coherent product rather than a research tree with a few demo scripts added on top.**

That first impression is the primary UX acceptance criterion for this refactor.
