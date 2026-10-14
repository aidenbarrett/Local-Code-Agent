# Quickstart

Local Code Agent currently targets a managed Windows 11 local-AI workstation for its primary hardware path. Other runtimes/devices fit behind the same controller boundary, but support claims require their own acceptance evidence.

## 1. Clone and check the machine

```powershell
git clone https://github.com/aidenbarrett/Local-Code-Agent.git
cd Local-Code-Agent
.\install.ps1 -CheckOnly
```

Run setup when ready:

```powershell
.\install.ps1
```

Bare setup shows the machine-level changes it may make and asks first. `-InstallMissing` is the explicit non-interactive approval for approved prerequisite installation. Driver and optional Windows-feature changes remain explicit.

Setup creates/updates the managed Python environment, prepares the configured local runtime/model, runs protocol conformance checks and proves the local C++ toolchain can configure/build/test the validation fixture.

After setup, ask whether the machine and the repository you are in are ready. It
only looks: it installs, downloads and starts nothing, and for each missing piece it
prints the one command to run next:

```powershell
.\local-code-agent.ps1 doctor
```

The default worker is `ptl-gpu-30b`, the Qwen3-Coder 30B-A3B INT4 model on the
GPU. Its weights are about 17 GB, so use the lighter `ptl-npu-8b` option on a
machine that cannot comfortably run it. List the available presets and their
download state before opening the product:

```powershell
.\local-code-agent.ps1 models
.\local-code-agent.ps1 models pull ptl-gpu-30b
```

To install without any model download (for example when you already have the
weights and will copy them in yourself), use `-SkipModelDownload`. Setup finishes
everything else, prints the exact folder the selected model's weights belong in and
exits 3. Copy them there, confirm with `models`, then rerun the same command to start
and qualify the model:

```powershell
.\install.ps1 -SkipModelDownload
```

`models use` remembers the normal choice for future sessions. It changes a
per-user runtime setting, not this repository:

```powershell
.\local-code-agent.ps1 models use ptl-gpu-30b
```

## 2. Declare your repository

Local Code Agent builds and tests only what a repository declares in its
`.local-agent.toml`; it never guesses. From inside your repository (any folder in
it works), `init` shows the root it will use and, for a CMake project, a proposed
declaration. It only looks; nothing is written or run:

```powershell
cd C:\src\my-project
$lca = 'C:\src\Local-Code-Agent\local-code-agent.ps1' # your installed checkout
& $lca init
```

Review the proposal, then write it with `init --write` (it never replaces an
existing declaration) and confirm with `doctor`. To let LCA prepare isolated
repairs and explicitly commit applied candidates, opt in with `--allow-patch`
and `--allow-commit`. Preview those same flags before writing; they do not apply
or commit anything. Without them, repairs and commits stay disabled. Build and
test still run only in a session you open with `session --allow-execution`.

```powershell
& $lca init --write
& $lca doctor --repo .
```

For candidate repairs and exact commits, use this **instead of** the plain write:

```powershell
& $lca init --allow-patch --allow-commit
& $lca init --write --allow-patch --allow-commit
```

An existing declaration is preserved. To change its permissions, review and edit
its `[policy]` table explicitly. Applying, undoing and committing remain separate
user actions; LCA never pushes.

To prove the declaration works before involving a model, build and test the
repository in place. No model is used and each step gets its own verified result.
Tracked and unignored work stays exactly as it was; the build itself creates or
updates normal artifacts in the declared build directory (`build/`), and run logs go
under `.local-agent/`. Both should be in your `.gitignore`: a check that leaves
tracked or unignored files changed fails instead of passing.

```powershell
& $lca acceptance --repo . --allow-build --output C:\lca-evidence\my-project-1 --only R02-build --only R03-tests
```

`R02-build` passes only on a verified full build; `R03-tests` passes only on a
verified full test run (a build, then the tests). Every log is kept in the run's
output directory.

Choose a new output directory for each run; existing evidence is never replaced.

## 3. Open the product

```powershell
& $lca session --repo . --allow-execution
```

That opens the Textual Session Hub, the default human interface for conversation plus controller-owned repository work.

Useful explicit modes:

```powershell
& $lca help
& $lca capabilities
& $lca session --repo . --profile ptl-npu-8b --allow-execution
& $lca chat ptl-gpu-30b
```

An explicit `session --profile <preset>` overrides the remembered choice for
that session only. It does not change what the next session uses.

Raw chat has no repository authority, tools or independent verification. The Session Hub is the product path for controlled engineering work.

## 4. Ask for work naturally

Examples:

```text
Inspect this repository.
Where is EndpointRuntime used?
What changed on my branch?
Build it.
Why did that fail?
Fix the build.
Fix the failing tests.
```

The controller decides the permitted route/tools and owns verification. The model never receives arbitrary shell authority and cannot certify its own success.

If several tasks have failed, select the failed **Build** or **Tests** task in the
Hub. **Explain** reads its recorded failure; **Fix** requests an isolated repair
of that exact task. These actions preserve your unfinished message and still use
the controller's eligibility and permission checks. Selecting a task only shows
its retained result; it does not rerun anything.

A verified fix is an isolated candidate. Select it in the Hub, click **Review**,
then **Apply** to change your checkout. After applying, **Undo** restores that
candidate's owned changes or **Commit** commits exactly those changes. Each button
uses the selected candidate's exact task ID; you do not need to copy it. The
`/diff`, `/apply`, `/undo` and `/commit` commands with a task ID remain available.
Commit does not push. Stale
candidates and later conflicting edits refuse; read the result before retrying.

Stop fences further work and reconciles owned commands. With OVMS, an in-flight
generation may still have to finish; a stopped UI does not prove endpoint-side
cancellation. Close and reopen the same repository to inspect retained results;
interrupted effects are not silently replayed.

## 5. Hardware demo

The demo scripts run the configured local model on a selected device for a visible smoke test:

```powershell
Set-Location (Split-Path -Parent $lca)
.\demo\run-qwen-on-npu.ps1
.\demo\run-qwen-on-gpu.ps1
.\demo\run-qwen-on-cpu.ps1
```

Their timing is live demo output, not benchmark evidence. Runtime comparison belongs to the neutral endpoint harness under `internal/perf/`.

## 6. Performance comparison

For engineering/runtime work, copy the generic example profile to an ignored local path and point it at the endpoint being measured:

```powershell
.\local-code-agent.ps1 benchmark --profile .local-agent\perf\my-endpoint.toml --cold-start --cancellation --soak-hours 4 --output run.json
```

See `internal/perf/README.md`. Backend-specific commands and observation hooks belong in that local profile, not in harness source.

## What to read next

- `README.md`: product purpose and architecture
- `docs/project/CURRENT_STATE.md`: current implemented state and open gaps
- `docs/development/TRICKS.md`: first-release public journeys
- `internal/docs/verification.md`: independent proof semantics
- `internal/docs/serving.md`: product-owned serving lifecycle
- `internal/docs/product-roadmap.md`: longer product direction
- `AGENTS.md`: engineering rules for this repository

Historical research material is archived off the active branch and is not required for normal use or current development.
