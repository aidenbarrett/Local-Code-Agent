# internal/docs/

Four kinds of document. The kind matters more than the topic, because it tells you
whether disagreeing with a file makes you wrong or makes it wrong.

No file count here on purpose. An index that states how many documents exist is wrong
the first time somebody adds one.

## Normative: the code must match these

| File | Governs |
|---|---|
| `session-contract/v1/events.schema.json` | normative `lca.session.events/1` wire shape. It is loaded at runtime and part of the hashed surface. Changing frozen values requires a new supported schema version, never a silent edit |
| `verification.md` | what counts as proof that a build or test happened, and when proof goes stale |
| `next-experiment-preregistration.md` | endpoints E1-E4 and the bounded repeat rule, registered before collection |
| `measurement-protocol.md` | how a run is collected and what invalidates it |
| `provenance/session-contract-source-identity.md` | why the schema is hashed |
| `architecture/process-containment.md` | what cancellation may and may not claim |

If the code disagrees with one of these, the code is wrong.

## Design: agreed intent, not automatically implemented

| File | Current interpretation |
|---|---|
| `product-roadmap.md` | agreed product destination and workflow acceptance gates; future scope is not an implementation claim |
| `session-contract/README.md` | design rationale and semantic notes for the v1 event contract; JSON schema is normative |
| `session-hub-design.md` | Session Hub architecture. Current source and `CURRENT_STATE.md` decide which slices are actually composed |
| `session-hub-file-layout.md` | intended ownership/layout; verify names against current source before adding another component |
| `session-hub-implementation-plan.md` | historical/working slice order, not proof a slice reached the public product |
| `textual-client-design.md` | design intent for the Textual client. **A live Textual Session Hub now exists on the public session path**; remaining acceptance gaps are tracked in `CURRENT_STATE.md` |
| `vscode-client-design.md` | later VS Code / Remote SSH client shape; no extension is delivered yet |
| `endpoint-scheduler-design.md` | canonical endpoint arbitration design. `EndpointArbiter`, `EndpointRuntime` and `EndpointCallAdapter` exist; public conversation/worker client composition and cancellation reconciliation remain incomplete. Do not create a second scheduler authority |
| `gateway-transport-design.md` | later transport constraints; current Session Hub remains in-process |
| `conversation-product-architecture.md` | conversation product intent; verify reachability against current source |
| `conversation-branch-validation.md` | branching semantics |
| `serving.md`, `serving-and-accelerators.md` | model serving and accelerator choices |
| `energy-study-methodology.md`, `energy-study-decisions.md` | energy study methodology/history |
| `experiment-design.md`, `pilot-design.md`, `gen2-scripted-baseline.md` | experiment design/history |

Read design documents for intended boundaries. Do not read them as automatic descriptions
of current `main`.

## Operational: how to do a thing

`bring-up.md`, `work-laptop-bootstrap.md`, `running-experiments.md`,
`testing-and-tev-framework.md`.

These go stale in the ordinary way. If one is wrong, fix it in the same change that
changes the supported workflow.

## Record: what happened, and what we do not yet know

`review-history.md`, `project-history.md`, `open-methodology-questions.md`,
`quality-hardening-roadmap.md`, `external-review-brief.md`, `UX_ROOT_REFACTOR_PLAN.md`.

`review-history.md` is where historical merge context belongs. Historical scores and old
snapshots stay historical. `quality-hardening-roadmap.md` is a backlog, not a
specification and not a current assurance score.

## Current-status rule

For current implementation claims use this order:

1. current GitHub `main` source/tests/CI;
2. root `CURRENT_STATE.md` for the latest reconciled operational snapshot;
3. root `PROJECT_OVERVIEW.md` for durable architecture, decisions and experiment history;
4. design documents for intended future composition;
5. chats only as disposable working discussion.

A merged primitive is not a delivered feature until the public composition uses it and a
behavioural acceptance path proves it. A document that claims a capability the tree does
not have is worse than a missing document, because somebody will build on it.
