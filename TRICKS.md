# Public journeys: the first useful Local Code Agent release

This is the short product acceptance contract for the public `local-code-agent.ps1` Session Hub. It defines what a user can ask, what they see, and what evidence earns a "delivered" label. It does **not** change the frozen experimental instrument or claim that these journeys all work today. [CURRENT_STATE.md](CURRENT_STATE.md) records the current implementation and evidence; the [product roadmap](internal/docs/product-roadmap.md) owns longer-term scope.

The first release should let a new user inspect a repository, find code, understand a branch, run a configured build or test, explain a failure, and see exactly which local model and device were used. They should be able to stop a running task truthfully. Source edits, conflict resolution, commits and recurring watches remain later release journeys; they are still the product ambition.

## Shared rules for every journey

- The launcher opens one conversation. The user types ordinary requests; supported read-only requests do not need to be prefixed with `work`. An explicit request to build or test is authorization only within configured repository policy and the public execution setting. A model proposal is never permission for mutation or untrusted command execution. Unsupported or ambiguous work gets one specific explanation and next action.
- The controller selects the repository, skill, allowed tools, execution policy and verification requirement before effects. The admitted choice must be the executed choice. Only controller facts may label a result as verified; useful model analysis is visibly separate from the verdict.
- The user can always see the active repository, the configured conversation and worker model/profile, endpoint health, and the **observed** device when runtime evidence supports it. Show "configured NPU; actual device unverified" when that is all we know. Never infer device utilisation from a profile name or a successful HTTP response.
- A result shows what was attempted, what passed or failed, what was not run, the repository state it covers, and a practical next step. A successful full build proves a build, not that tests passed or the requested behaviour is correct. A passing test needs a current matching build where the tool contract requires it. Outage, timeout, zero tests and stale evidence cannot become success.
- The conversation and activity feed preserve the same task identity through restart. Worker text is an inspectable artifact, not an assistant turn that can rewrite task truth. If the durable feed is unhealthy, preserve the draft and refuse actions whose state cannot be established.
- Each delivered journey needs a behavioral test through the **public composition** with a controlled fake server and a negative case, plus an installed Windows launcher check where relevant. A fake server establishes wiring and failure semantics; a separate disconnected run on the Panther Lake laptop establishes actual NPU operation. Record exact checkout, model/artifact, runtime/driver, configured and observed device, commands, repository state and timings. Neither test substitutes for the other.
- Failures of policy, trust, provenance or truthfulness block the affected journey even if the happy-path demo looks good. Existing frozen experiments and Generation-2 axes stay untouched.

## The user contract

| ID | User says or does | Required visible behaviour |
|---|---|---|
| J1 | "Help" / "What can you do?" | Deterministic, in-session list of actions available **here**, with disabled actions explained. No model call or endpoint dependency. Root `help` also works. |
| J2 | "What are you running on?" | Active repo, configured chat/worker model and endpoint; endpoint health and observed device with provenance and a distinct unknown state. No model call. |
| J3 | "Inspect this repo" / "Where is X defined?" | Read-only work starts directly; result includes navigable paths/lines, scope and missing coverage. A vanished endpoint or invalid result says so and retains the request. |
| J4 | "What changed on my branch?" | Compare against an explicit or documented base, distinguish staged, unstaged and committed changes, cite actual paths/commits; say when base cannot be established. |
| J5 | "Build it" / "Run the tests" | Use only configured commands when enabled; give separate build/test facts, exit state, command/profile, evidence and exact tree/scope. Build-only success must never imply tests passed. |
| J6 | "Why did that fail?" | Select the most recent eligible failed task in this conversation with verified retained data; show the failure evidence and analysis. If none or ambiguous across scopes, explain and offer visible task IDs. |
| J7 | "Stop" / visible Stop action | Request stop immediately, fence further dispatch, reconcile owned inference/process effects, and show `stopped` only after cleanup proof; otherwise `stop requested; cleanup unknown` and block unsafe continuation. |
| J8 | Close/reopen Hub; endpoint unavailable | Retain conversation, task/result references and unresolved work without replaying effects. Outage or invalid model response gives a concrete connection/protocol failure and next action, never "please rephrase" for transport failure. |

These are acceptance targets, not claims of model competence. Qualify exact combinations of model, runtime, device and host only for the journeys actually exercised. Record read-only and build/test latency from submission to first useful output and final result, with cold versus warm endpoint state distinguished. The weekly laptop run is useful evidence, not a substitute for behavioral CI or proof of general reliability.

## Delivery order

1. **Fix visible lies and friction**: J1/J2/J8. Differentiate endpoint outage from malformed model response. Expose configured versus observed device. Make help available even with the model down.
2. **Make the worker immediately useful**: J3/J4. Route supported read-only requests without the `work` ritual; inspect the public answer and source references. Preserve exact policy and task admission. Give an explicit route confirmation only when authority is genuinely missing.
3. **Prove the declared task**: J5/J6. Bind verdicts to the requested build/test scope and current tree, repair failure referents and show the retained answer beside deterministic evidence.
4. **Make long work controllable**: J7, then complete J8 restart/outage/cancellation cases. Compose the existing endpoint owner and cancellation runtime; remove duplicate or unreachable paths only after real-entrypoint coverage.
5. **Rehearse on the actual offline laptop**: cold/warm NPU sessions, source/branch/build/failure/stop/restart flows, with timings and failure artifacts. Only then label this first release supported.

After that, add a separately scoped mutation journey: preview a bounded change, apply it under explicit authority, verify the resulting tree, preserve user edits and show the diff. Follow with Git conflict resolution and commits. Recurring watches become a public promise only when users can create, inspect and stop them; until then the required watch pane should say the feature is unavailable, rather than implying an empty configured list is a usable scheduler.

## Change and review discipline

A PR must identify the public journey **or** an immediate security/truthfulness defect it fixes, show the failing boundary case, say where the public entrypoint reaches the fix, and report exact tests and source identities. Infrastructure work that directly enables an accepted journey is allowed; it is not delivered merely because its isolated tests pass. A reviewer may challenge architecture when a diff creates a second authority, weakens a trust boundary or conceals a user-visible failure. Do not ban architecture review by job title.

Do not add a static "importable module" CI gate. Imports can be reachable yet unused, and legitimate plugin/optional modules can be loaded dynamically. Test effects and state transitions from the launcher/composition root; keep a small manually reviewed inventory for deliberately deferred code, and delete obsolete implementations when no public or experimental caller remains.

Keep `CURRENT_STATE.md` as a dated, evidence-linked human snapshot of reachability, hardware observations and unknowns. Scripts may generate a code/CI facts appendix, but they cannot generate human product decisions or physical NPU evidence. The product can change faster than the roadmap; update this contract and the snapshot together when a supported journey changes.

A demo video is a fast sanity check. The release gate is the behavior and evidence above, including failure cases.
