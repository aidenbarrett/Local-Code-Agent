# Current state

Reconciled against GitHub `main` at `b2cd21f6d4af623c2aba4b2c980e8fa4b92e0852`
on 2026-10-06.

## Capability ledger

Qualification describes the strongest retained evidence, not configured intent. `unknown`
is deliberate where the required physical or repository-setting observation is absent.

| ID | Public request | Implemented behaviour | Evidence | Qualification | Known limit | Next action | Owner |
|---|---|---|---|---|---|---|---|
| CAP-inspect | Inspect this repository | Read-only paths, files and text search | PR#250, J12-questions | deterministic-ci | Model answers need grounding checks | Keep R01 coverage | controller |
| CAP-symbol | Where is this symbol? | Definition and include-relative lookup | PR#386 | deterministic-ci | Language-aware indexing is not claimed | Extend only from failures | controller |
| CAP-branch-review | What changed on my branch? | Merge-base, upstream and detached-HEAD facts | PR#367, J17-branch-review | deterministic-ci | No upstream means ahead/behind is unknown | Keep read-only | controller |
| CAP-build-verify | Build it | Configured build with proof bound to stable inputs | PR#433, J01-build-pass | deterministic-ci | Repository must declare a build profile | Qualify more repositories | controller |
| CAP-test-truth | Run the tests | Full or filtered test truth with zero/stale non-PASS | PR#383, J19-test-truth | deterministic-ci | Repository must declare a test profile | Qualify more repositories | controller |
| CAP-failure-diagnosis | Why did that fail? | Uses retained build/test evidence and explicit task identity | PR#250, J02-build-fail | deterministic-ci | Unsupported failures remain unclassified | Extend typed diagnoses | controller |
| CAP-candidate-fix | Fix the build or tests | Isolated candidate with build/test proof | PR#197, J13-candidate-scripted | deterministic-ci | Real-model success is configuration-specific | Finish real-repository qualification | controller |
| CAP-change | Change this source | Intent-scoped isolated candidate | PR#215, J11-change | deterministic-ci | General arbitrary checkout mutation is refused | Keep candidate boundary | controller |
| CAP-apply | Apply this candidate | All-or-nothing import with stale-content refusal | PR#378, J13-candidate-scripted | deterministic-ci | Interrupted receipt stays UNKNOWN | Keep restart regressions | controller |
| CAP-undo | Undo this candidate | Restores owned pre-import bytes and modes | PR#407, J13-candidate-scripted | deterministic-ci | Refuses after later user edits | Keep preservation regressions | controller |
| CAP-exact-commit | Commit this candidate | Private-index exact-path commit, no push | PR#434, J21-exact-commit | deterministic-ci | Commit policy may refuse | Keep hook/restart regressions | controller |
| CAP-stop-command | Stop the running command | Owned process trees; Stop ends reads, lets writes finish and is reported by the same call | PR#379, PR#436, PR#437, J05-stop-build | deterministic-ci | POSIX cleanup is never confirmed (a setsid descendant can escape the group) | Qualify on target hardware | Claude |
| CAP-stop-generation | Stop model work | Fences late authority and records NO_VERDICT when unreconciled | PR#249, J07-stop-model | deterministic-ci | One in-flight generation may remain | Prove endpoint interruption | Claude |
| CAP-git-state | What Git operation is active? | Reports merge, rebase, am, cherry-pick, revert and bisect state | PR#373, J20-conflict-explain | deterministic-ci | Does not perform continue or abort | Keep read-only | controller |
| CAP-unusual-filenames | Inspect or commit unusual names | Literal path handling protects wildcard-like names | PR#404, J15b-rename-binary | deterministic-ci | Platform filesystem rules still apply | Keep cross-platform regression | controller |
| CAP-git-filters | Inspect repository identity safely | Refuses executable Git-filter boundaries | PR#424 | deterministic-ci | Does not evaluate repository filter programs | Keep fail-closed | controller |
| CAP-orphan-cleanup | Clean abandoned workspaces | Deletes only when owner death and worktree removal are proven | PR#410, PR#437 | deterministic-ci | Unknown ownership is retained and reported | Keep removal-proof regressions | controller |
| CAP-build-dir | Rebuild safely | Deletes only marked owned build trees | PR#408 | deterministic-ci | Unmarked directories require user action | Keep ownership marker | controller |
| CAP-empty-target | Build an explicit target | Empty or malformed targets refused before effects | PR#429 | deterministic-ci | Target existence is build-system-specific | Keep negative cases | controller |
| CAP-fallback-search | Search without ripgrep | Searches scoped tracked and untracked source, excluding builds | PR#405 | deterministic-ci | Bounded filesystem traversal | Keep parity tests | controller |
| CAP-repo-explain | What does this repository do? | Grounds files and configured commands in repository facts | PR#413, J22-repo-explain | deterministic-ci | Generalisation beyond tested repositories is open | Complete cxxopts qualification | controller |
| CAP-install | Install and first run | Checks Python, CMake and Windows C++ toolchain | PR#374 | deterministic-ci | Physical offline first run is unqualified | Run disconnected PTL acceptance | Aiden |
| CAP-offline-physical | Work fully offline on target hardware | Capture path exists; qualification is not complete | PR#232 | unknown | No current disconnected exact-head hardware run | Execute and retain physical run | Aiden |
| CAP-merge-policy | Require reviewed green integration | Offline checker can observe branch/ruleset enforcement | PR#276 | unknown | Observed 2026-10-06: main unprotected, no ruleset, no required checks | Configure reviewed-green PR/check enforcement, then re-observe | Aiden |

Use [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) for durable architecture/rationale,
[TRICKS.md](TRICKS.md) for first-release public journeys and acceptance evidence, and
[product-execution-priorities.md](internal/docs/product-execution-priorities.md) for the
priority ladder after the immediate first-release gate.

## Direction and public path

Local Code Agent is a dependable local/offline worker for repository inspection, Git,
bounded code changes, builds/tests and diagnosis. The public path is the in-process Textual
Session Hub launched by `local-code-agent.ps1`; the deterministic controller owns authority,
effects, proof and durable state, while qualified model/runtime backends remain replaceable.

Detailed architecture is in [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md), public journeys are
in [TRICKS.md](TRICKS.md), and the active priority ladder is in
[product-execution-priorities.md](internal/docs/product-execution-priorities.md).

## Boundaries that remain open

- Physical disconnected Panther Lake qualification is unknown: see
  `CAP-offline-physical` and #418.
- Stop reaches workspace Git and owned commands (#436, #437), but in-flight model
  generation and endpoint cleanup proof are incomplete, and POSIX process cleanup is never
  reported confirmed: see `CAP-stop-command` and `CAP-stop-generation`.
- Native-endpoint deployment and cross-process endpoint ownership are not qualified serving
  capabilities: see #403.
- Merge policy was observed not enforced on 2026-10-06 (main unprotected, no ruleset,
  no required checks): see `CAP-merge-policy` and #403.
- Real-model coding evidence remains configuration- and repository-specific: see
  `CAP-candidate-fix` and #418.

Historical per-PR status prose is preserved unchanged in
[internal/docs/history/current-state-log.md](internal/docs/history/current-state-log.md);
it is not current product status.
