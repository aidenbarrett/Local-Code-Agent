# Current state

Reconciled against GitHub `main` at `bcdbd49e5c56480dbadb23db799a5549a31b4dfd`
on 2026-09-24. That commit merged the truthful-runtime-facts work. The merged PR head
`92c462d5a5b1826c70d79e7c7345d0743010dc0a` completed `tests`, `serving`,
`product acceptance`, `integration eligibility`, `frozen experiment guard`,
`test import boundary` and `contract declaration integrity` successfully. GitHub
returned no pull-request-triggered workflow runs for the merge commit itself. This is
source/CI evidence for that reviewed head, not Panther Lake/NPU acceptance.

Current source and CI are authoritative for implementation; frozen artifacts for
historical experiments; [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) for durable
rationale; [TRICKS.md](TRICKS.md) for first-release public acceptance; and
[product-execution-priorities.md](internal/docs/product-execution-priorities.md) for
adopted cross-project product learnings, anti-tangent rules and the priority ladder
beyond the immediate queue below.

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
  acceptance job on Windows, deterministic help, `--check`, and deterministic
  in-session help/runtime-fact responses that do not require a model call.
- Runtime observation that keeps facts distinct: the Hub can project an observed
  model identity from a bounded `/v1/models` probe while configured preset/device
  remain declared facts. Endpoint URL and execution-enabled state are visible.
  This is not observed device-utilisation proof.
- Typed endpoint-unavailable handling separate from malformed/invalid model
  proposal handling; an endpoint outage no longer tells the user to rephrase.
- Conservative managed primary-endpoint startup before the Session Hub opens:
  reuse a healthy matching owned runtime, stop only stale owned state, refuse an
  unowned reachable endpoint, otherwise start through the serving controller.
  Runtime-specific process ownership remains at the serving edge.
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
- `EndpointArbiter`, `EndpointRuntime` and `EndpointCallAdapter` remain the one
  in-process endpoint arbitration implementation. The managed public startup
  path now composes runtime ownership, but actual conversation/worker calls are
  not yet fully routed through that one queue/lease authority and cross-process
  ownership is not claimed.

These are parts of an honest worker, not evidence that every journey in
`TRICKS.md` passes on the physical laptop.

## Immediate public gaps

| Gap | Observed source behaviour | First-release effect |
|---|---|---|
| Ordinary read-only work | Only a few anchored phrases route directly. `Inspect this repo` and `Where is X defined?` still rely on model proposal/fallback rather than a direct supported read-only journey. | J3 still makes ordinary work depend too much on internal routing behaviour. |
| Requested versus proved scope | Existing tool-level source hashes, verification facts and epoch rules do not yet bind the exact user request, complete command/check scope and current tree identity as one acceptance fact. | J5 must distinguish build pass from test pass, partial proof and stale/unavailable proof. |
| Failure follow-up | `Why did that fail?` is deterministic only with one eligible retained failure; explicit UUID syntax works, but a useful latest/recent failure interaction remains incomplete. | J6 becomes cumbersome after multiple failures. |
| Cancellation | `CancellableDurableTaskExecutor` can receive a cancellation request and fence a late completion. The busy Textual product path still lacks one complete Stop contract across queued work, inference, descendants, endpoint cleanup and terminalization. | J7 cannot be demonstrated honestly end to end. |
| Endpoint call composition | Managed startup/runtime ownership is public, but real conversation and worker calls are not yet all wrapped by the single endpoint arbitration/lease authority. | Queue/lease/quarantine semantics are not yet one public execution contract. |
| Watch creation/reuse | Watch admission/lifecycle components and a watch projection exist, but no reviewed public workflow creates a reusable watch from a successful task specification. | Recurring work is not yet a normal user capability. |
| Physical runtime/device proof | Model identity can be observed from the endpoint, while device remains declared configuration. No disconnected Panther Lake acceptance has established actual NPU utilisation for the public journeys. | Hardware/support claims must remain narrower than the configured profile name. |

Build/test execution requires `--allow-execution` and configured repository
policy. Source mutation, rename, conflict resolution and commit are longer-term
product journeys. The existing read-only tools and worker are valuable, but
their presence does not establish public reachability, output quality or NPU
performance.

## Next work, in order

1. Deliver direct supported read-only inspection, symbol lookup and branch review
   through durable admission, fixed tool authority and useful cited output. The user
   should ask for the outcome without a magic `work` turn or route/skill vocabulary.
2. Make build/test/diagnostic and follow-up verdicts task-specific and current-tree
   aware. Exercise positive, negative, partial, stale and unavailable proof through
   the installed public path.
3. Compose a visible Stop action with cancellation fencing, queued/inference handling,
   owned process-tree cleanup, endpoint quarantine/reconciliation and one durable
   terminal result; preserve unknown cleanup honestly as `NO_VERDICT`.
4. Route actual conversation and worker inference through the single endpoint
   arbitration/lease authority. Then expose truthful queue/lease/quarantine facts and
   only afterward make recurring Watch creation a public workflow.
5. Run the public journeys disconnected on the physical Panther Lake NPU with exact
   model/runtime/device identities, cold/warm latency, screenshots/logs and failures.
   Only then call that configuration supported.

Once those first-release joins are closed, take the next work from
`internal/docs/product-execution-priorities.md`: truthful evidence/result/Attention
UX, deterministic whole-run fault injection, safe scoped mutation, reviewed reusable
Task/Watch specifications, then real-task onboarding and human comprehension testing.
Those later ideas do not preempt this immediate sequence.

Security and a user-visible false claim may interrupt the sequence. Novelty does not.
The standards repair backlog supplies correctness repairs for these journeys, not an
independent stream of new primitives. Each change needs a public-path behavioural
regression, applicable native Linux/Windows tests, fresh direct-to-current-main CI,
source identity and unchanged frozen experimental axes. A green CI run is evidence for
that tested checkout and scope only.

Repository-required-check enforcement remains a separate repository-owner concern.
Do not describe workflow files as enforced branch protection unless repository settings
have actually been verified/configured. Open PRs can change this snapshot; inspect live
source and PR state before acting.
