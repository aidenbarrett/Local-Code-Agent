# Current state

Reconciled against GitHub `main` at `8bdcd72fcd91ce4a597f5f32509d71d220691b29`
on 2026-10-13. One CPU-reference model repair on pinned cxxopts produced the exact
identifier fix, passed its configured full build and independent full tests, and
completed diff/apply/exact commit with unrelated work preserved after PR#517 repaired
byte fidelity. This is one retained attempt, not a success rate, general competence,
or physical/offline qualification; see
`internal/docs/rc-model-competence-2026-10-11.json`.

The fresh Windows CPU30B archive setup reached READY TO USE, but its enabled
second-launch repository answers were MEASURED:no-evidence; semantic correctness
remains unknown (PR#522). A separate CPU30B production count-defect repair timed out
after its 300-second repair budget and remains UNKNOWN; its original raw report,
unchanged upstream tests, baseline logs and preparation errors are retained in
`internal/docs/rc-model-production-test-2026-10-12.json`. Neither observation
replaces #293's physical GPU30B J10 comparison or certifies offline operation.

## Capability ledger

Qualification describes the strongest retained evidence, not configured intent. `unknown`
is deliberate where the required physical or repository-setting observation is absent.

| ID | Public request | Implemented behaviour | Evidence | Qualification | Known limit | Next action | Owner |
|---|---|---|---|---|---|---|---|
| CAP-hub-recovery | Type, recover from an outage and read a reopened result | The composer receives first keystrokes; compact conversation/recovery panes scroll within the viewport; reopening displays the retained answer and task without reissuing work | PR#500, internal/tests/unit/test_session_textual_hub.py::test_outage_and_long_path_keep_task_watch_and_composer_inside_viewport, internal/tests/unit/test_session_textual_retained_results.py::test_reopened_failure_details_are_keyboard_accessible_without_reissuing_task | deterministic-ci | Tested terminal sizes and durable fixtures do not certify every terminal or physical workstation | Use the public Hub in the target-machine workflow | controller |
| CAP-continuous-engineering | Inspect, find, diagnose, fix, review, apply, undo and commit in one session | One repository/session carries the complete scripted workflow, rejects stale apply and preserves unrelated staged, unstaged and untracked work; machine-delimited search handles Windows drive paths with ripgrep installed | PR#498, PR#499, J24-continuous, internal/tests/unit/test_search_match_records.py::test_absolute_ripgrep_match_is_a_portable_repo_relative_definition | deterministic-ci | Scripted worker evidence does not measure real model competence | Run the same engineering outcomes with the selected physical model and external repository | controller |
| CAP-inspect | Inspect this repository | Read-only paths, files and text search | PR#250, J12-questions | deterministic-ci | Model answers need grounding checks | Keep R01 coverage | controller |
| CAP-symbol | Where is this symbol? | Bounded declaration/definition candidates and include-relative lookup for non-template C++ identifiers | PR#386, PR#448, J23-symbol-lookup | deterministic-ci | Textual heuristic; template-ids and language-aware indexing are not claimed; zero candidates does not prove absence | Add a language-aware owner only from measured failures | controller |
| CAP-branch-review | What changed on my branch? | Merge-base, upstream and detached-HEAD facts | PR#367, J17-branch-review | deterministic-ci | No upstream means ahead/behind is unknown | Keep read-only | controller |
| CAP-build-verify | Build it | Configured build with proof bound to stable inputs | PR#433, J01-build-pass | deterministic-ci | Repository must declare a build profile; `init` proposes one for a single-CMake root only (PR#469) | Qualify more repositories | controller |
| CAP-test-truth | Run the tests | Full or filtered test truth with zero/stale non-PASS | PR#383, J19-test-truth | deterministic-ci | Repository must declare a test profile; `init` proposes one for a single-CMake root only (PR#469) | Qualify more repositories | controller |
| CAP-failure-diagnosis | Why did that fail? | Uses retained build/test evidence and explicit task identity; failed builds and tests name their kind (compile, link, configure; assertion, crash, timeout, failed), first evidence and exact argv | PR#250, PR#474, J02-build-fail, internal/tests/unit/test_failure_kind.py::test_a_failed_build_names_kind_evidence_and_command | deterministic-ci | The kind is in the tool result and answer, not yet a durable typed fact the diagnosis route consumes; unrecognised logs stay unclassified | Feed typed failure facts to 'Why did that fail?' (#462) | controller |
| CAP-candidate-fix | Fix the build or tests | Isolated candidate with build/test proof; narrow repairs finish from current configured proof without another model reporting turn, while Stop still discards the candidate | PR#197, PR#514, PR#517, J13-candidate-scripted, internal/tests/unit/test_candidate_change_journey.py::test_a_proven_repair_finishes_before_another_model_call, internal/tests/unit/test_candidate_change_journey.py::test_stop_between_passing_proof_and_completion_discards_the_candidate | real-model-ci | One pinned cxxopts CPU-8B attempt produced a verified repair; this does not establish a rate, general model competence, physical PTL behaviour or offline qualification; a general change still requires the worker to finish its semantic request | Run the controlled physical-profile matrix in #293 | controller |
| CAP-change | Change this source | Intent-scoped isolated candidate | PR#215, J11-change | deterministic-ci | General arbitrary checkout mutation is refused | Keep candidate boundary | controller |
| CAP-apply | Apply this candidate | All-or-nothing import with stale-content refusal | PR#378, J13-candidate-scripted | deterministic-ci | Interrupted receipt stays UNKNOWN | Keep restart regressions | controller |
| CAP-undo | Undo this candidate | Restores owned pre-import bytes and modes | PR#407, J13-candidate-scripted | deterministic-ci | Refuses after later user edits | Keep preservation regressions | controller |
| CAP-exact-commit | Commit this candidate | Private-index exact-path commit, no push; verified before anything names it, then published by one `update-ref HEAD` from the parent it was built from, which LCA's reference-transaction hook admits only when HEAD resolved to that branch (HEAD stays locked for the update); a concurrent commit, reset, branch switch, detach or candidate-file edit refuses with no ref moved | PR#434, PR#454, J21-exact-commit, internal/tests/unit/test_commit_expected_parent.py::test_a_concurrent_user_commit_is_never_published_over | deterministic-ci | Commit policy may refuse | Keep hook/restart regressions | controller |
| CAP-stop-command | Stop the running command | Owned process trees; Stop ends reads, lets writes finish and is reported by the same call. On Windows, cleanup is confirmed only when every Job member's process handle is signaled within one deadline, never from Job accounting alone. On Linux a dedicated child subreaper owns the tree, so `setsid()` descendants are ended and reaped before return; a descendant that was ended and confirmed gone is evidence, not failure, and only unconfirmed settlement fails the step | PR#379, PR#436, PR#437, PR#451, PR#452, PR#456, J05-stop-build, internal/tests/unit/test_runner_process_tree.py::test_linux_subreaper_ends_a_setsid_descendant_after_normal_parent_exit, internal/tests/unit/test_supervised_settlement.py::test_an_unreported_settlement_fails_the_public_result_closed, internal/tests/unit/test_supervisor_overhead.py::test_owned_command_overhead_stays_bounded | deterministic-ci | Other POSIX platforms have only an escapable process group and effect-owning commands fail closed | Qualify on target hardware | controller |
| CAP-stop-generation | Stop model work | Fences late authority and records NO_VERDICT when unreconciled; on a `stop_proof` profile, cuts the stream once the server is seen working and frees the endpoint only on observed idle | PR#249, J07-stop-model, PR#446 | deterministic-ci | OVMS profiles (`stop_proof = none`): one in-flight generation may remain. llama-server cut not yet observed against a real server | Observe the cut on the NUC llama-server; find an OVMS proof source | Claude |
| CAP-git-state | What Git operation is active? / What's wrong with my repo? | Reports merge, rebase, am, cherry-pick, revert and bisect state. State questions are answered by the controller with no model: one `git_status` call, explained as observed facts (operation, conflicts, branch or detached HEAD, upstream or its absence, ahead/behind, staged/unstaged/partially staged/untracked) and next steps for the user to run, each saying what it affects and what it can lose (abort discards changes staged since a merge started; continue commits everything staged) | PR#373, J20-conflict-explain, internal/tests/unit/test_repository_state.py::test_whats_wrong_with_my_repo_explains_a_stopped_merge_and_changes_nothing, internal/tests/unit/test_repository_state.py::test_the_abort_warning_matches_what_git_does_to_staged_work, PR#491 | deterministic-ci | Never performs continue, abort, reset or push; remote reachability, stash and lost-commit recovery are not covered; a repository whose tracked files use a configured Git filter gets no answer (see CAP-git-filters) | Public journey for the state question (#464) | controller |
| CAP-unusual-filenames | Inspect or commit unusual names | Literal path handling protects wildcard-like names | PR#404, J15b-rename-binary | deterministic-ci | Platform filesystem rules still apply | Keep cross-platform regression | controller |
| CAP-git-filters | Inspect repository identity safely | Refuses executable Git-filter boundaries: candidate checks, apply, undo and commit, and the read tools that touch the working tree (`git_status`, working-tree `git_diff`, `git_stage`), through one shared detector; staged diff, log and show stay available | PR#424, internal/tests/unit/test_tool_git_filters.py::test_a_worktree_git_tool_refuses_a_filtered_repository_and_runs_nothing, internal/tests/unit/test_tool_git_filters.py::test_the_planted_filter_does_run_under_plain_git_status | deterministic-ci | Does not evaluate repository filter programs; a repository whose tracked files use a configured filter gets no working-tree Git reads | Keep fail-closed | controller |
| CAP-orphan-cleanup | Clean abandoned workspaces | Deletes only when owner death and worktree removal are proven | PR#410, PR#437 | deterministic-ci | Unknown ownership is retained and reported | Keep removal-proof regressions | controller |
| CAP-build-dir | Rebuild safely | Deletes only marked owned build trees | PR#408 | deterministic-ci | Unmarked directories require user action | Keep ownership marker | controller |
| CAP-empty-target | Build an explicit target | Empty or malformed targets refused before effects | PR#429 | deterministic-ci | Target existence is build-system-specific | Keep negative cases | controller |
| CAP-fallback-search | Search without ripgrep | Searches scoped tracked and untracked source, excluding builds | PR#405 | deterministic-ci | Bounded filesystem traversal | Keep parity tests | controller |
| CAP-repo-explain | What does this repository do? | Exact how-built/tested questions are answered by a fixed read-only plan from repo_info, with each declared profile, default, command arguments and absent stages; no model, build or test runs. Other explanations observe, never certify, answers about the repository: every cited file must be a regular file contained in the repository (missing, traversal, absolute, drive, UNC, file: URI and outward-symlink references are ungrounded); the configured commands must be given for how-it-is-built questions (J22, R01 and Q06 alike); the repository must be unchanged; a bounded refusal signal counts only the listed opening forms, never length. J22's scripted worker is a deterministic PASS. Model answers (J12, R01, Q06) are MEASURED on those dimensions only and never count as completed or verified | PR#413, PR#449, J22-repo-explain, R01-questions, internal/tests/unit/test_configured_build.py::test_durable_gateway_reports_configuration_without_a_model_or_build, internal/tests/unit/test_acceptance_journeys.py::test_unreal_and_external_references_are_never_grounded, internal/tests/unit/test_acceptance_journeys.py::test_model_answers_are_never_counted_as_completed_verified | deterministic-ci | Whether a model answer is correct is not measured: it stays unknown and is read in the transcript. A real token in a wrong or contradictory sentence is still only a real token. Path detection covers a directory separator with a lettered suffix or a bare name with a known source/build suffix, after removing a :line or #Lline anchor; any file: token is outside with no suffix needed, percent-decoded once when well-formed and reported raw when malformed; network URLs are skipped | Measure Q06 on the NUC (#418); add an independent, retained scorer before any correctness claim | controller |
| CAP-install | Install and first run | Checks Python, CMake and Windows C++ toolchain; read-only `doctor` reports readiness with one next action per gap; `init` declares a repository without guessing | PR#374, PR#467, PR#468, PR#469, internal/tests/unit/test_doctor.py::test_main_exits_3_when_not_ready_and_repeats_identically, internal/tests/unit/test_repo_setup.py::test_a_nested_git_repository_never_inherits_an_ancestor_declaration | deterministic-ci | Physical offline first run is unqualified | Run disconnected PTL acceptance | Aiden |
| CAP-runtime-lifecycle | Keep the local model server warm between sessions | One owner (`ensure_managed_runtime`) reuses an owned server only when it was launched exactly as the current profile would launch it (resolved executable, args, child env, model directory); a changed server argument replaces it, client-only settings never do. The outcome is typed (reused, started, replaced with dead/unhealthy/config_changed, refused with foreign_endpoint/start_failed/not_healthy), and a launch made by that call records client-observed launch-to-ready in `process.json`. Hub, `run-task` and `chat` share it | PR#484, internal/tests/unit/test_runtime_lifecycle.py::test_a_healthy_server_with_stale_arguments_is_replaced_not_reused, internal/tests/unit/test_runtime_lifecycle.py::test_start_records_launch_to_ready_for_the_process_it_launched | deterministic-ci | The lifecycle outcome is not yet in the durable session stream; `endpoint.state_changed` has a consumer but no producer; launch-to-ready is not model-load or compile time. Cold/warm endpoint measurement lives in the perf harness (PR#483), not in product telemetry | Agree and add a durable session-scoped runtime fact (#481 slice B) | Claude |
| CAP-offline-physical | Work fully offline on target hardware | `qualify --profile <p> --output <new-dir>` owns preparation, exact disconnect confirmation, retained operator-described isolation method, offline preflight, public acceptance execution, finalization and printed/retained summary; failed route/adapter observation or a missing isolation description refuses, while configured and present device facts remain distinct from observed physical execution | PR#232, PR#479, PR#499, PR#532, internal/tests/test_panther_lake_acceptance.py::test_prepare_reports_missing_weights_without_certifying_offline, internal/tests/unit/test_panther_lake_acceptance_capture.py::test_run_refuses_missing_or_multiline_isolation_method | unknown | No disconnected exact-head Panther Lake run; PnP device candidates do not establish the runtime execution device; scripted results never certify physical execution; cold/warm model timings and human interventions remain unknown without independent records | Run the public flow on the target machine and review retained device/offline evidence | Aiden |
| CAP-merge-policy | Require reviewed green integration | Existing checker observes branch/ruleset enforcement | PR#276 | unknown | Observed 2026-10-12 with the existing checker: main unprotected, no active required-status-check ruleset and no required checks | Configure reviewed-green PR/check enforcement, then re-observe | Aiden |

Use [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) for durable architecture/rationale,
[TRICKS.md](../development/TRICKS.md) for first-release public journeys and acceptance evidence, and
[product-execution-priorities.md](../../internal/docs/product-execution-priorities.md) for the
priority ladder after the immediate first-release gate.

## Direction and public path

Local Code Agent is a dependable local/offline worker for repository inspection, Git,
bounded code changes, builds/tests and diagnosis. The public path is the in-process Textual
Session Hub launched by `local-code-agent.ps1`; the deterministic controller owns authority,
effects, proof and durable state, while qualified model/runtime backends remain replaceable.

Detailed architecture is in [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md), public journeys are
in [TRICKS.md](../development/TRICKS.md), and the active priority ladder is in
[product-execution-priorities.md](../../internal/docs/product-execution-priorities.md).

## Boundaries that remain open

#403 remains the release decision. Its child defects #392–#402 are closed, and
the capability ledger plus J24 now cover the repaired composed controller path.
This is a development handoff, not an offline-qualified release: physical target
evidence, repeatable configuration-specific model competence and enforced merge policy
still need evidence. One CPU-reference cxxopts repair is retained, but is not a rate or
a physical-profile qualification. Qualification reports count dispatched attempts; skipped model work
is not an attempt, unknown outcomes do not prove correct refusal, and unrecorded
human edits/retries/manual Stops remain unknown.

- Physical disconnected Panther Lake qualification is unknown: see
  `CAP-offline-physical` and #418.
- Stop reaches workspace Git and owned commands (#436, #437, #451, #452). In-flight model generation is
  cut only on profiles with endpoint stop proof (llama-server, #446); OVMS still waits for
  the call to finish. Windows Job Objects and the Linux child-subreaper supervisor own
  command trees; other POSIX platforms refuse effect-owning success: see `CAP-stop-command`
  and `CAP-stop-generation`.
- Native-endpoint deployment and cross-process endpoint ownership are not qualified serving
  capabilities: see #403.
- The merge-policy observation on 2026-10-12 found main unprotected,
  no active required-status-check ruleset and no required checks: see `CAP-merge-policy` and #403.
- Real-model coding evidence remains configuration- and repository-specific: see
  `CAP-candidate-fix` and #418.

Historical per-PR status prose is preserved unchanged in
[internal/docs/history/current-state-log.md](../../internal/docs/history/current-state-log.md);
it is not current product status.

The pinned development walkthrough and separate CI/model/physical limits are in
[RC_HANDOFF.md](../releases/RC_HANDOFF.md). One retained CPU-reference trial produced a verified cxxopts repair; this does not
establish a success rate or certify PTL/offline readiness. The development handoff in
#465 is complete; #293 and #403 remain open for physical evidence and the release decision.
