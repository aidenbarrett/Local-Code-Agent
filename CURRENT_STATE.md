# Current state

Reconciled against GitHub `main` at `b2cd21f6d4af623c2aba4b2c980e8fa4b92e0852`
on 2026-10-06.

## Capability ledger

Qualification describes the strongest retained evidence, not configured intent. `unknown`
is deliberate where the required physical or repository-setting observation is absent.

| ID | Public request | Implemented behaviour | Evidence | Qualification | Known limit | Next action | Owner |
|---|---|---|---|---|---|---|---|
| CAP-inspect | Inspect this repository | Read-only paths, files and text search | PR#250, J12-questions | deterministic-ci | Model answers need grounding checks | Keep R01 coverage | controller |
| CAP-symbol | Where is this symbol? | Bounded declaration/definition candidates and include-relative lookup for non-template C++ identifiers | PR#386, J23-symbol-lookup | deterministic-ci | Textual heuristic; template-ids and language-aware indexing are not claimed; zero candidates does not prove absence | Add a language-aware owner only from measured failures | controller |
| CAP-branch-review | What changed on my branch? | Merge-base, upstream and detached-HEAD facts | PR#367, J17-branch-review | deterministic-ci | No upstream means ahead/behind is unknown | Keep read-only | controller |
| CAP-build-verify | Build it | Configured build with proof bound to stable inputs | PR#433, J01-build-pass | deterministic-ci | Repository must declare a build profile | Qualify more repositories | controller |
| CAP-test-truth | Run the tests | Full or filtered test truth with zero/stale non-PASS | PR#383, J19-test-truth | deterministic-ci | Repository must declare a test profile | Qualify more repositories | controller |
| CAP-failure-diagnosis | Why did that fail? | Uses retained build/test evidence and explicit task identity | PR#250, J02-build-fail | deterministic-ci | Unsupported failures remain unclassified | Extend typed diagnoses | controller |
| CAP-candidate-fix | Fix the build or tests | Isolated candidate with build/test proof | PR#197, J13-candidate-scripted | deterministic-ci | Real-model success is configuration-specific | Finish real-repository qualification | controller |
| CAP-change | Change this source | Intent-scoped isolated candidate | PR#215, J11-change | deterministic-ci | General arbitrary checkout mutation is refused | Keep candidate boundary | controller |
| CAP-apply | Apply this candidate | All-or-nothing import with stale-content refusal | PR#378, J13-candidate-scripted | deterministic-ci | Interrupted receipt stays UNKNOWN | Keep restart regressions | controller |
| CAP-undo | Undo this candidate | Restores owned pre-import bytes and modes | PR#407, J13-candidate-scripted | deterministic-ci | Refuses after later user edits | Keep preservation regressions | controller |
| CAP-exact-commit | Commit this candidate | Private-index exact-path commit, no push; verified before anything names it, then published by one ref update of the branch from the parent it was built from, admitted by LCA's reference-transaction hook only while HEAD still names that branch; a concurrent commit, reset, branch switch, detach or candidate-file edit refuses with no ref moved | PR#434, J21-exact-commit, internal/tests/unit/test_commit_expected_parent.py::test_a_concurrent_user_commit_is_never_published_over | deterministic-ci | Commit policy may refuse | Keep hook/restart regressions | controller |
| CAP-stop-command | Stop the running command | Owned process trees; Stop ends reads, lets writes finish and is reported by the same call | PR#379, PR#436, PR#437, J05-stop-build | deterministic-ci | POSIX cleanup is never confirmed (a setsid descendant can escape the group) | Qualify on target hardware | Claude |
| CAP-stop-generation | Stop model work | Fences late authority and records NO_VERDICT when unreconciled; on a `stop_proof` profile, cuts the stream once the server is seen working and frees the endpoint only on observed idle | PR#249, J07-stop-model, PR#446 | deterministic-ci | OVMS profiles (`stop_proof = none`): one in-flight generation may remain. llama-server cut not yet observed against a real server | Observe the cut on the NUC llama-server; find an OVMS proof source | Claude |
| CAP-git-state | What Git operation is active? | Reports merge, rebase, am, cherry-pick, revert and bisect state | PR#373, J20-conflict-explain | deterministic-ci | Does not perform continue or abort | Keep read-only | controller |
| CAP-unusual-filenames | Inspect or commit unusual names | Literal path handling protects wildcard-like names | PR#404, J15b-rename-binary | deterministic-ci | Platform filesystem rules still apply | Keep cross-platform regression | controller |
| CAP-git-filters | Inspect repository identity safely | Refuses executable Git-filter boundaries | PR#424 | deterministic-ci | Does not evaluate repository filter programs | Keep fail-closed | controller |
| CAP-orphan-cleanup | Clean abandoned workspaces | Deletes only when owner death and worktree removal are proven | PR#410, PR#437 | deterministic-ci | Unknown ownership is retained and reported | Keep removal-proof regressions | controller |
| CAP-build-dir | Rebuild safely | Deletes only marked owned build trees | PR#408 | deterministic-ci | Unmarked directories require user action | Keep ownership marker | controller |
| CAP-empty-target | Build an explicit target | Empty or malformed targets refused before effects | PR#429 | deterministic-ci | Target existence is build-system-specific | Keep negative cases | controller |
| CAP-fallback-search | Search without ripgrep | Searches scoped tracked and untracked source, excluding builds | PR#405 | deterministic-ci | Bounded filesystem traversal | Keep parity tests | controller |
| CAP-repo-explain | What does this repository do? | Observes, never certifies, answers about the repository: every cited file must be a regular file contained in the repository (missing, traversal, absolute, drive, UNC, file: URI and outward-symlink references are ungrounded); the configured commands must be given for how-it-is-built questions (J22, R01 and Q06 alike); the repository must be unchanged; a bounded refusal signal counts only the listed opening forms, never length. J22's scripted worker is a deterministic PASS. Model answers (J12, R01, Q06) are MEASURED on those dimensions only and never count as completed or verified | PR#413, J22-repo-explain, R01-questions, internal/tests/unit/test_acceptance_journeys.py::test_unreal_and_external_references_are_never_grounded, internal/tests/unit/test_acceptance_journeys.py::test_model_answers_are_never_counted_as_completed_verified | deterministic-ci | Whether a model answer is correct is not measured: it stays unknown and is read in the transcript. A real token in a wrong or contradictory sentence is still only a real token. Path detection covers a directory separator with a lettered suffix or a bare name with a known source/build suffix, after removing a :line or #Lline anchor; any file: token is outside with no suffix needed, percent-decoded once when well-formed and reported raw when malformed; network URLs are skipped | Measure Q06 on the NUC (#418); add an independent, retained scorer before any correctness claim | controller |
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
- Stop reaches workspace Git and owned commands (#436, #437). In-flight model generation is
  cut only on profiles with endpoint stop proof (llama-server, #446); OVMS still waits for
  the call to finish. POSIX process cleanup is never reported confirmed: see
  `CAP-stop-command` and `CAP-stop-generation`.
- Native-endpoint deployment and cross-process endpoint ownership are not qualified serving
  capabilities: see #403.
- Merge policy was observed not enforced on 2026-10-06 (main unprotected, no ruleset,
  no required checks): see `CAP-merge-policy` and #403.
- Real-model coding evidence remains configuration- and repository-specific: see
  `CAP-candidate-fix` and #418.

Historical per-PR status prose is preserved unchanged in
[internal/docs/history/current-state-log.md](internal/docs/history/current-state-log.md);
it is not current product status.
