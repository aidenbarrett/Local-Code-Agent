# Current state

Reconciled against GitHub `main` at `85dedfa14b381fc663e739c37eee5a67477ed5b8`
on 2026-09-23. Its `tests`, `serving` and `product acceptance` workflow runs
completed successfully. This is a source/CI snapshot, not a Panther Lake/NPU
acceptance result. Recheck the current head and open PRs before implementation claims.

Current source and CI are authoritative for implementation; frozen artifacts for
historical experiments; [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) for durable
rationale; [TRICKS.md](TRICKS.md) for first-release public acceptance.

## Direction and actual public path

The goal is a dependable offline local worker for finding, Git, bounded code
changes, builds/tests and diagnosis, with replaceable qualified model/runtime
backends. The active product is the in-process Textual Session Hub launched by
`local-code-agent.ps1` with no argument or `session`. The controller owns
admission, policy, execution facts, verification and durable state. The model
may propose work but cannot certify it. Measurement collection remains paused;
Generation-1 evidence is frozen and Generation-2 has no collected model rows
at this snapshot.

The public path now reaches:

- An exact-interpreter dependency preflight, a managed installed-checkout
  acceptance job on Windows, a deterministic `help` command, and `--check`.
  The Windows job tests help/capabilities, a missing dependency, and explicit
  reinstall. It does not run live inference or an NPU.
- One persisted raw conversation and a separate durable event/task store,
  admission before effects, accepted-route provenance, retained request/result
  artifacts, and restart reconciliation to unknown without replaying effects.
- Anchored deterministic routes for `Build it.`, `What changed on my branch?`,
  `Why did that fail?` when there is exactly one eligible failure, explicit
  `why did task <UUID> fail?`, and `/check`. The source fixes the admitted
  skill through the worker boundary.
- A live Textual conversation, activity and watch projection. The activity
  pane shows task identity, tool/verdict, retained answer and recent task IDs.
  A broken durable feed refuses new input and preserves the draft.
- The existing proof classifier, source-aware build stamp, source/skill
  provenance at admission, stronger durable completion invariants, and
  cancellation epoch fencing in the admitted executor. Source mutation and
  commits remain disabled on the conversation product path.
- `EndpointArbiter`, `EndpointRuntime` and `EndpointCallAdapter` as one
  in-process endpoint ownership implementation. The duplicate scheduler
  concept has been removed. This is not yet composition of all public model
  calls or cross-process ownership.

These are parts of an honest worker, not evidence that every journey in
`TRICKS.md` passes on the physical laptop.

## Immediate public gaps

| Gap | Observed source behaviour | First-release effect |
|---|---|---|
| In-session help and runtime status | `intents.py` has no deterministic help/status route. Session setup knows configured chat/worker profile/model/device and endpoint URL, but the Hub does not display verified active model or observed device. | J1/J2 fail with the endpoint down; a profile name can be mistaken for hardware evidence. |
| Ordinary read-only work | Only a few anchored phrases route directly. `Inspect this repo` and `Where is X defined?` use model fallback; repository proposals require a subsequent `work` turn. | J3 requires a magic word for work the user already asked for. |
| Model/transport failure | `ConversationGateway._model_proposal` catches invalid proposal and `LLMTransportError` together and records "Please rephrase." | J8 gives the wrong remedy when the server is unavailable. |
| Requested versus proved scope | The orchestrator's `state.verified` can be set by either a full build or full test; the product verdict does not yet bind the specific user request, complete command scope and tree identity as one acceptance fact. Existing tool-level source hashes and epoch rules are useful, but do not close this product join. | J5 must distinguish build pass from test pass and incomplete verification, even when a generic proof exists. |
| Failure follow-up | `Why did that fail?` selects only when exactly one eligible retained failed task exists; it does not choose the latest one. UUID syntax works for eligible candidates. | J6 becomes cumbersome after multiple failures. |
| Cancellation | `CancellableDurableTaskExecutor` can receive a cancellation request and fence a late completion. The busy Textual input path cannot dispatch a Stop action, and inference/process cleanup is not composed end to end. | J7 cannot be demonstrated honestly. |
| Endpoint arbitration and watch | Endpoint lease/call components and watch admission/lifecycle components exist. `session-hub.py` constructs plain `OpenAICompatibleClient` for both model roles; no public watch-create control is present. | Neither endpoint scheduling nor recurring watches are public capabilities yet. |

Build/test execution requires `--allow-execution` and configured repository
policy. Source mutation, rename, conflict resolution and commit are longer-term
product journeys. The existing read-only tools and worker are valuable, but
their presence does not establish public reachability, output quality or NPU
performance.

## Next work, in order

1. Deliver deterministic in-session help/status and separate endpoint outage
   from malformed model output. Show configured model/device separately from
   observed runtime evidence, with explicit unknowns.
2. Deliver direct supported read-only inspection, symbol lookup and branch
   review through durable admission, fixed tool authority and useful cited
   output. Do not grant model proposals execution authority.
3. Make the build/test and follow-up verdict task-specific and current-tree
   aware. Exercise real positive, negative, stale and unavailable cases via the
   installed public path.
4. Compose a visible Stop action with cancellation fencing, endpoint quarantine
   and process cleanup/reconciliation; preserve unknown cleanup honestly.
5. Run the public journeys disconnected on the physical Panther Lake NPU with
   exact model/runtime/device identities, cold/warm latency, screenshots/logs and
   failures. Only then call those configurations supported.

Security and a user-visible false claim interrupt this sequence. The PR
backlog supplies repairs for the journeys, not an independent stream of new
primitives. Each change needs a public-path behavioral regression, applicable
native Linux/Windows tests, exact-head CI, source identity and unchanged
frozen experimental axes. A green CI run is evidence for that tested checkout
and scope only.

The repository API returned no active rulesets at this snapshot; branch
protection could not be inspected with this connection (GitHub returned 403).
Do not describe required checks as enforced by repository settings until an
administrator verifies/configures them. Open PRs can change the snapshot;
inspect their exact diffs and merge state before acting.
