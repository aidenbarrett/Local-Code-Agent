# internal/docs/

Four kinds of document. The kind matters more than the topic, because it tells you
whether disagreeing with a file makes you wrong or makes it wrong.

No file count here on purpose. An index that states how many documents exist is wrong the
first time somebody adds one, and this index exists to stop exactly that.

## Normative: the code must match these

| File | Governs |
|---|---|
| `session-contract/v1/events.schema.json` | the normative `lca.session.events/1` wire shape. It is loaded at runtime and is part of the hashed surface. Its values are frozen: event kinds, terminal states, verdicts, route sources. Changing one requires a new supported schema version, never a silent edit |
| `verification.md` | what counts as proof that a build or test happened, and when a stamp goes stale |
| `next-experiment-preregistration.md` | endpoints E1-E4 and the bounded repeat rule, registered before collection |
| `measurement-protocol.md` | how a run is collected and what invalidates it |
| `provenance/session-contract-source-identity.md` | why the schema is hashed |
| `architecture/process-containment.md` | what cancellation may and may not claim |

If the code disagrees with one of these, the code is wrong.

## Design: agreed intent, not yet fully built

| File | Status |
|---|---|
| `session-contract/README.md` | design rationale and semantic notes for `lca.session.events/1`; the versioned JSON schema is normative and `CURRENT_STATE.md` owns implementation/reachability status |
| `session-hub-design.md` | the Session Hub architecture. Parts are implemented, parts are not; `CURRENT_STATE.md` is the authority on which |
| `session-hub-file-layout.md` | intended module layout |
| `session-hub-implementation-plan.md` | slice order |
| `future-session-interfaces.md` | endpoint scheduling and transport contracts that are still design-only and intentionally do not live in the source package |
| `future-textual-client.md` | future Textual client requirements; no UI runtime exists yet |
| `future-vscode-client.md` | future VS Code / Remote SSH client requirements; no extension exists yet |
| `conversation-product-architecture.md` | the conversation product |
| `conversation-branch-validation.md` | branching semantics |
| `serving.md`, `serving-and-accelerators.md` | model serving and accelerator choices |
| `energy-study-methodology.md`, `energy-study-decisions.md` | the energy study |
| `experiment-design.md`, `pilot-design.md`, `gen2-scripted-baseline.md` | experiment shape |

Read these for what we decided to build. Do not read them as descriptions of `main`.

## Operational: how to do a thing

`bring-up.md`, `work-laptop-bootstrap.md`, `running-experiments.md`,
`testing-and-tev-framework.md`.

These go stale in the ordinary way. If one is wrong, fix it.

## Record: what happened, and what we do not yet know

`review-history.md`, `project-history.md`, `open-methodology-questions.md`,
`quality-hardening-roadmap.md`, `external-review-brief.md`, `UX_ROOT_REFACTOR_PLAN.md`.

`review-history.md` is where merge history and PR numbers belong, so that the
current-state documents do not have to carry them. `quality-hardening-roadmap.md` is a
backlog, not a specification: nothing in it is a commitment.

## One rule for this directory

A document that claims a capability the tree does not have is worse than a missing
document, because somebody will build on it. If you cannot tell whether something here
is normative or aspirational, that is a defect in this README; say so and it gets fixed.
