# Integration merge discipline

This document records the repository rule learned from the Session Hub stacked-PR incident on 2026-09-21.

A GitHub pull request being marked `merged` is not, by itself, proof that its reviewed implementation is reachable from `main`. A child PR can be merged into a feature-parent branch that was already merged earlier; GitHub then reports the child as merged even though the child commit never becomes an ancestor of live `main`.

## Integration authority

`main` is the canonical integration branch. A pull request may target another feature branch for early CI while work is being developed, but that state is not merge-eligible.

Before a change is merged for product integration:

1. Reconcile or retarget the pull request directly onto current `main`.
2. Recompute repository identity from the resulting integration tree. Source-only drift must be finalized in `internal/INSTRUMENT.json`; model-facing or outcome-facing drift requires an explicit generation/methodology decision.
3. Require the authoritative Linux and Windows pytest jobs, the offline compatibility runner, instrument integrity, serving checks and any dedicated contract gates to pass on that direct-to-`main` candidate.
4. Merge only that verified integration candidate. Do not merge a child into an already-merged feature parent and assume reachability will follow.
5. After merge, verify that the intended reviewed head or its preserved ancestry is reachable from live `main`. Repository reachability, not a PR status badge, is the implementation fact.

## Stacked pull requests

Stacking remains useful for parallel development and early feedback. It is a development convenience, not an integration topology.

A stacked child may run tests before its parent lands, but it must remain visibly non-merge-ready until it is reconciled directly onto `main`. If the parent changes after the child was prepared, reconcile the child again rather than carrying a stale merge tree forward.

Do not use squash or rebase blindly when downstream branches rely on parent ancestry. If preserving ancestry is required for an active train, use a normal merge commit or explicitly rebuild downstream branches after the parent lands. The chosen method must be deliberate and visible in the PR.

## Provenance and frozen evidence

Never solve an integration problem by editing historical experiment artifacts, retroactively changing denominators or weakening verification. Frozen experiments remain frozen. New instrumentation or methodology belongs to a new experiment generation.

`source_sha256` may move for ordinary product changes, but the declaration must describe the final coherent integration tree rather than an obsolete stacked merge candidate. `base_prompt_sha256` and `outcome_contract_sha256` remain protected contract axes.

## Practical rule

The safe merge train is deliberately boring:

- one writer per branch;
- early stacked CI if useful;
- direct-to-`main` reconciliation before integration;
- finalized provenance;
- all authoritative CI green;
- merge;
- confirm reachability;
- then start the next dependent product slice from the new live `main`.

This rule favours deterministic integration and reproducibility over the appearance of parallel velocity.
