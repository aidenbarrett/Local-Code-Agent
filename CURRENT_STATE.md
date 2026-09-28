# Current state

Reconciled against GitHub `main` at `141aa625c5a4729da2e25afc29e3d2714238fcee`
on 2026-09-27 after the isolated candidate-change journeys (#197-#215: build/test fixes,
`fix it`, `change:`, `/apply`, `/undo`, `/commit`, `/diff`, `/discard`, durable candidate
facts, readiness refusal), the engineering-standards ratchet (#216, burn-down
#217/#220/#222-#227), the native C++ endpoint (#214) and its C++ gates (#229), the
real-model CI journey (#230), deterministic build/test routes (#233), the acceptance
journey runner (#234, #239, #243), truthful model-unavailable answers (#241, #242) and
the context-exhaustion verdict fix (#244), the quality floor (#231), the Panther Lake
acceptance summary (#232), candidate-ready truth (#236) and Stop endpoint fencing with
natural-completion reconciliation (#237, #238, #240, integrated by #246 and #247), the
answered stopped turn and durable unproven test runs (#249), repository questions answered
from the repository and the search-streak nudge (#250), deterministic push/commit/delete
refusals and the scripted candidate journey (#252, #253), failed-test CI annotations
(#254) and duration-balanced CI test shards (#256, #257) landed. In-job parallel tests
(#251) were measured and not adopted; the parallel probe exposed an unconfirmed
abandoned-descendant cleanup on Windows, fixed with this reconciliation. Live
source and current CI remain authoritative for implementation; frozen artifacts remain
authoritative for historical experiments.

Use [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) for durable architecture/rationale,
[TRICKS.md](TRICKS.md) for first-release public journeys and acceptance evidence, and
[product-execution-priorities.md](internal/docs/product-execution-priorities.md) for the
priority ladder after the immediate first-release gate.

## Direction and public path

The product target is a dependable local/offline worker for repository inspection, Git,
bounded code changes, builds/tests and diagnosis with replaceable qualified model/runtime
backends. The active surface is the in-process Textual Session Hub launched by
`local-code-agent.ps1` with no argument or `session`.

The deterministic controller owns admission, permissions, execution facts, verification,
provenance and durable state. Models may propose work but cannot grant authority or
self-certify success. Measurement collection remains paused; Generation-1 evidence is
frozen and product hardening must not rewrite historical methodology or denominators.

## What is implemented on current main

The public/product path now includes:

- exact-interpreter dependency preflight, native Windows installed-checkout launcher
  acceptance, deterministic help and Session Hub `--check`;
- managed primary-runtime startup with conservative ownership/reuse rules and truthful
  configured-versus-observed runtime facts;
- one persisted raw conversation plus separate durable event/task state, admission before
  effects, retained request/result artifacts and restart reconciliation that never replays
  unknown effects;
- deterministic public read-only journeys for repository inspection, symbol lookup and
  branch review, with model fallback remaining advice rather than permission;
- task-specific build/failure journeys through the public conversation gateway; exact
  `build it` and `run the tests` / `test it` are configured checks the controller runs
  with no model client at all (a fixed plan drives the ordinary worker, so verification
  and the durable facts `fix it` reads keep one owner);
- `.\local-code-agent.ps1 acceptance --output <dir> [--allow-model] [--repo <path>]` runs
  the acceptance journeys (build/test truth, ambiguity, authority, Stop during a build and
  during model work, the full candidate lifecycle, measured model fixes) on the real Session
  Hub composition and keeps `journeys.json`, a summary, transcripts and durable event logs;
  the report records the exact product source (`package_identity()`), and with
  `--allow-model` one call through the product's own client must succeed before any model
  journey runs, otherwise those journeys are UNKNOWN with the client's error; J13 runs the
  whole candidate lifecycle (diff, stale apply, apply, undo, commit) with a scripted worker
  through the real controller, so CI exercises it on Linux and Windows without a model;
- everyday repository questions ("what does this repository do?", "how is it built and
  tested?", "where is the X implemented?") route directly to read-only repository
  navigation; push, bare commit and delete imperatives are refused without a model, naming
  the supported path; a model repository proposal is shown as the user's own request;
- CI runs the authoritative suite as duration-balanced shards that partition what pytest
  collects (Linux one, Windows five, about nine minutes on Windows instead of 37), names
  failed tests in check annotations and reports each shard's slowest files;
- a command that exits normally but abandons descendants in its Windows job is reported
  with `process_cleanup_confirmed` from the job's own accounting, never assumed clean;
- a task that could not use the model says why in the transport's own words, and a turn
  after the endpoint is quarantined is answered with the reason and "restart the Hub"
  instead of raising; a worker that exhausts its context budget ends BLOCKED /
  `unavailable_capability` with the budget named, and every halt cause projects to a
  verdict the contract accepts;
- typed proof binding tying accepted completion to the exact request, proof scope, current
  mutation epoch and current repository tree identity; targeted/partial proof cannot
  certify whole-tree success;
- deterministic failure follow-up for eligible retained failures, including explicit task
  UUID selection;
- retained-result projection that preserves authoritative durable task/verdict truth when
  sibling retained worker prose fails integrity validation and marks that prose unavailable;
- Windows launcher containment of hostile ambient `PYTHONHOME`/`PYTHONPATH` state;
- one process-local endpoint ownership/arbitration path for live conversation and worker
  calls through the endpoint arbiter/runtime/call adapter; cross-process arbitration is
  not claimed;
- a public `stop`/`/stop` action targeting the exact active durable task ID and execution
  epoch, bypassing model dispatch and fencing late completion authority;
- revoked Stop epochs terminalize durably as `UNKNOWN` / `NO_VERDICT` rather than leaving
  a task hanging non-terminal; late controller results cannot regain commit authority;
- Textual activity/result projection with durable task identity, tool/verdict facts,
  retained answer detail, recent task IDs and fail-closed input behaviour when the durable
  feed is unhealthy;
- a Panther Lake acceptance-capture path that records exact checkout, offline preconditions,
  runtime/model/device provenance, public Session Hub preflight, cold/warm run evidence and
  hashes, while explicitly refusing to self-certify physical NPU acceptance;
- a fail-closed merge-enforcement checker that distinguishes actual GitHub branch/ruleset
  required-check policy from merely having workflow files.
- a native C++ OpenAI-compatible endpoint (`internal/native_endpoint/`, `lca-endpoint`)
  with a fixture backend, an OpenVINO GenAI backend and a C ABI for out-of-tree runtime
  plugins, exposing request-id, active-request, identity and telemetry hooks the endpoint
  harness consumes. CI drives it with the product client, `qualify_server.py` and the
  harness, including endpoint-proven cancellation.

The public conversation path now supports isolated source-changing journeys. Each prepares a
candidate in an LCA-owned worktree whose base is the user's current source (tracked changes
plus untracked, non-ignored files up to 5 MiB, snapshotted through a private temporary
index), proves it there, and retains it for explicit review. Preparing a candidate never
modifies the user's checkout, index or history:

- `fix the build` (proof: a full build), `fix the failing tests` (proof: a full unfiltered
  test run) and `change: <request>` (any requested source change, including new files;
  proof: a full unfiltered test run, which also establishes a current build);
- `fix it` / `fix that` / `fix task <uuid>` for exactly one durable FAILED task in the
  conversation, with the build or test fix chosen from durable `tool.finished` facts, never
  from worker prose;
- explicit, full-UUID, model-free controller actions: `/apply` (all-or-nothing import with a
  base-content precondition and byte-exact owned rollback; never stages), `/undo` (restores
  exact pre-import bytes; refuses if the files changed since), and `/commit` (requires
  `policy.allow_commit`; commits only the applied paths with `--only`, leaves other staged
  work staged, does not run user hooks, never pushes);
- retained results are `lca.task-result/2`, carrying a typed `candidate` block
  (prepared/applied/apply_refused/undone/committed/discarded) for the Session Hub; `/1` stays readable;
- an observed failing build or test is reported as `FAILED` / `verification_failed` with an
  observed-failure proof scope, not as `NO_VERDICT`;
- orphaned candidate worktrees from a dead controller are reaped at Session Hub start;
- a candidate change is refused up front, with reasons and before any workspace exists, when
  the machine cannot run one (git older than 2.25, not a top-level checkout with a commit,
  or an unwritable workspace folder outside the repository);
- `/discard <id>` drops a retained candidate (its worktree and record) without touching the
  checkout; a retained candidate keeps its source but not its build or run output, so kept
  candidates do not hoard disk;
- the candidate-ready answer shows a diffstat and a bounded preview, and `/diff <id>` shows
  the exact retained patch before anything is applied;
- `propose_patch` tolerates indentation slips for one unique whole-line block and
  re-indents the replacement to the file's style, so small models can land edits.

Engineering standards for Python, C++ and Rust are written in
`internal/docs/engineering-standards.md`. The fast gates enforce them on the product package
and dev tools as a ratchet (strict mypy on linux and win32 plus a ruff rule set, per file and
code): no file may gain a finding and new files must be clean. Existing debt is recorded in
`internal/static-standards-baseline.json` and is being burned down; it is not yet zero.

## Boundaries that remain open

The remaining gaps must not be collapsed into stronger claims than current evidence
supports:

- **Physical Panther Lake acceptance is still open.** The capture path exists, but the
  required evidence is still a fresh disconnected run on the physical Windows Panther
  Lake machine using the current Session Hub with exact checkout, model/runtime/device
  identity, cold/warm latency and retained logs/screenshots/failures. No hardware claim is
  complete until that run exists. First measured run, 2026-09-27, on the physical Panther
  Lake machine: acceptance journeys on source `8b9aa96` (the checkout reported uncommitted
  changes), `OpenVINO/Qwen3-8B-int4-cw-ov` through the managed OVMS endpoint with device NPU
  declared (Task Manager showed the NPU busy during model journeys). Product journeys PASS 7 /
  FAIL 0 / UNKNOWN 1 (J09, no verified candidate to exercise); Stop during model work reached
  a terminal in 0.3 s. Model journeys: no verified fix or change (J08 build fix spent its
  budget on repeated `search_text`; J10 was blocked by the durable-finish defect fixed in
  #249); questions were routed to a work proposal instead of answered. It was not a
  disconnected run and recorded no cold/warm latency, so this item stays open.
- **Stop remains bounded, not complete cancellation.** Public Stop, epoch fencing and
  durable `UNKNOWN` / `NO_VERDICT` terminal reconciliation exist. Stop now reaches running
  configured commands (including candidate builds, whose candidate is then discarded), and
  Windows commands run in a kill-on-close Job Object. Complete queued and
  inference interruption, owned descendant process-tree cleanup, endpoint quarantine/
  reconciliation and mutation reconciliation still require effectful proof. Until cleanup
  is proven, the product says `Stop requested`, not `Stopped`. Stop now fences queued worker
  requests for the exact task execution at the endpoint arbiter. Measured with acceptance
  J07 against a slow fixture endpoint (about 60 s per generation) on the #246 tree: Stop
  during model work reached a durable terminal in 128.6 s (on `ce32594` it reached none
  within 240 s), and the next turn was answered after the in-flight generation completed.
  Stop is therefore bounded by one in-flight generation, not interrupted. The stopped turn
  is answered with its durable `NO_VERDICT` / `cancel_unreconciled` terminal.
- **The native endpoint is not a serving profile yet.** No preset or `serve.py` path
  launches it, and it has no NPU, Panther Lake or OVMS-comparison evidence. Its CI
  real-model check is a small model on a hosted CPU runner.
- **Cross-process endpoint ownership is not claimed.** Current arbitration authority is
  intentionally process-local.
- **Merge enforcement is now machine-checkable but not configured by the repository.** At
  the last verified GitHub read, `main` was unprotected and no active required-status-check
  ruleset existed. The checker can prove configuration after an owner applies it; it does
  not possess repository-admin authority itself.
- **Real-model coding is measured, not yet demonstrated.** Every source-changing journey
  is exercised end to end with real git, CMake and CTest and scripted workers. CI now also
  drives `build it -> fix it -> /apply` with a real Qwen3-1.7B on CPU through the native
  endpoint: the first runs observed the failure and reported the unfixed candidate truthfully
  as FAILED; no real model has yet produced a verified fix. The Session Hub's "candidate
  ready, not applied" presentation is still open. Candidate verification belongs to the candidate tree; `/apply` claims it for
  the checkout only when the checkout's source then equals the candidate tree exactly.
- **Push, Watch creation and richer automation remain later product work.**

## Immediate work, in order

1. Complete the physical offline Panther Lake acceptance described in `TRICKS.md` using
   the current public Session Hub and the capture procedure in
   `internal/docs/panther-lake-acceptance.md`.
2. Configure GitHub required-check enforcement for `main` through repository settings,
   then verify it with `internal/devtools/check_merge_enforcement.py`. Until configuration
   is observed, green CI remains convention rather than enforced merge policy.
3. Continue P1 Session Hub trust work from
   `internal/docs/product-execution-priorities.md`: evidence-backed lifecycle projection,
   clearer result semantics, result/evidence detail, one Attention surface and a
   deterministic whole-run/fault-injection harness.
4. Continue Stop cleanup only through bounded authority-specific joins: endpoint
   interruption/quarantine, owned process-tree termination and mutation reconciliation.
   Do not promote `UNKNOWN` / `NO_VERDICT` into `CANCELLED` without cleanup evidence.
5. Extend mutation only through explicit intent-scoped journeys. Preserve dirty worktrees,
   staging and unrelated user edits; keep commit/push/history changes as separate
   capabilities, and never project candidate proof as checkout proof.

Security defects and user-visible false claims may interrupt this order. Novelty does not.
Every new slice needs a concrete user journey or trust-boundary failure, behavioural
regression evidence, fail-closed negatives, fresh exact-head CI and unchanged frozen
experimental axes.
