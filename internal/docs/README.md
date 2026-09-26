# internal/docs/

Documents here fall into three useful buckets: normative contracts, agreed design intent and operational/record material. Current source and CI remain implementation truth.

## Normative

| File | Governs |
|---|---|
| `session-contract/v1/events.schema.json` | supported `lca.session.events/1` wire shape loaded by the runtime |
| `verification.md` | what counts as build/test proof and when proof goes stale |
| `provenance/session-contract-source-identity.md` | why the runtime event schema participates in product source identity |
| `architecture/process-containment.md` | what cancellation may and may not claim |

If product code deliberately changes one of these contracts, update the corresponding documentation and regression tests in the same change.

## Design intent

Important design documents include:

- `product-roadmap.md`
- `product-execution-priorities.md`
- `session-contract/README.md`
- `session-hub-design.md`
- `session-hub-file-layout.md`
- `textual-client-design.md`
- `vscode-client-design.md`
- `endpoint-scheduler-design.md`
- `gateway-transport-design.md`
- `conversation-product-architecture.md`
- `conversation-branch-validation.md`
- `serving.md`
- `serving-and-accelerators.md`

Read these for intended boundaries, not as automatic claims about current `main`.

## Operational and record material

Operational documents include `bring-up.md`, `work-laptop-bootstrap.md` and `testing-and-tev-framework.md`.

Record/backlog documents include `review-history.md`, `quality-hardening-roadmap.md`, `standards-repair-backlog-2026-09-22.md` and `UX_ROOT_REFACTOR_PLAN.md`.

Historical experiment methodology and collected evidence are not maintained on the active product branch. The exact pre-cleanup tree is preserved on `archive/legacy-experiments-2026-09-26` at commit `ad07af071a62acfa4d0168c7a89d7110a52088f6`.

## Current-status rule

For current implementation claims use this order:

1. current GitHub `main` source, tests and CI;
2. root `CURRENT_STATE.md` for the latest operational snapshot;
3. root `PROJECT_OVERVIEW.md` for durable architecture and product direction;
4. `product-execution-priorities.md` for ordered product work beyond the immediate queue;
5. other design documents for intended future composition.

A merged primitive is not a delivered feature until the public composition uses it and a behavioural acceptance path proves it.
