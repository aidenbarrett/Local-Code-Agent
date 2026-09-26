# internal/docs/

Current source and CI own implementation truth. These documents describe contracts, agreed design and supported operating procedures.

## Normative contracts

- `session-contract/v1/events.schema.json`: supported Session Hub event shape loaded by the runtime
- `verification.md`: what counts as build/test proof and when it goes stale
- `provenance/session-contract-source-identity.md`: why the event schema participates in source identity
- `architecture/process-containment.md`: cancellation and cleanup claim boundaries

## Product design

- `product-roadmap.md`: product destination and milestone gates
- `product-execution-priorities.md`: immediate priority ladder and anti-tangent rules
- `session-hub-design.md`: Session Hub architecture
- `session-contract/README.md`: event-contract semantics
- `session-hub-file-layout.md`: intended ownership/layout
- `endpoint-scheduler-design.md`: endpoint arbitration design
- `conversation-product-architecture.md`: conversation/task authority model
- `textual-client-design.md`: Textual client design
- `vscode-client-design.md`: later VS Code client design

Read design documents as intent. Current source and `CURRENT_STATE.md` decide what is actually composed.

## Operations

- `serving.md`: product-owned local serving and qualification
- `panther-lake-acceptance.md`: physical offline product acceptance
- `verification.md`: proof semantics

Historical experiment methodology, collected evidence and research-era operating manuals are intentionally absent from the active branch. The exact old tree is preserved on `archive/legacy-experiments-2026-09-26` at commit `ad07af071a62acfa4d0168c7a89d7110a52088f6`.

## Current-status order

For current implementation claims use:

1. current GitHub `main` source, tests and CI
2. root `CURRENT_STATE.md`
3. root `PROJECT_OVERVIEW.md`
4. `product-execution-priorities.md`
5. other design documents

A primitive is not a delivered feature until the public composition uses it and behavioural acceptance proves it.
