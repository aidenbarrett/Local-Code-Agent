# Integration merge discipline

This document records the repository rule learned from the Session Hub stacked-PR incident on 2026-09-21.

A GitHub pull request being marked `merged` is not, by itself, proof that its reviewed implementation is reachable from `main`. A child PR can be merged into a feature-parent branch that was already merged earlier; GitHub then reports the child as merged even though the child commit never becomes an ancestor of live `main`.

## Integration authority

`main` is the canonical integration branch. A pull request may target another feature branch for early CI while work is being developed, but that state is not merge-eligible.

Before a change is merged for product integration:

1. Reconcile or retarget the pull request directly onto current `main`.
2. Compute `source_sha256` from that exact integration tree and preserve it in the run/build provenance that executes the tree. Ordinary source-only PRs do **not** edit `internal/INSTRUMENT.json`. The file pins only the model-facing and outcome-facing Generation-2 contract axes; drift on either still requires an explicit generation/methodology decision.
3. Require the authoritative Linux and Windows pytest jobs, the offline compatibility runner, contract/instrument integrity, serving checks and any dedicated contract gates to pass on that direct-to-`main` candidate.
4. Merge only that verified integration candidate. Do not merge a child into an already-merged feature parent and assume reachability will follow.
5. After merge, verify that the intended reviewed head or its preserved ancestry is reachable from live `main`. Repository reachability, not a PR status badge, is the implementation fact.

A base retarget does not make CI from the old stacked topology authoritative for the new integration candidate. The direct-to-`main` candidate must produce a fresh synchronized CI run before merge.

## Parallel and stacked pull requests

Several independent PRs may be opened directly against the same current `main` and tested concurrently. If their real code changes do not conflict, they may then be merged sequentially. Merging an earlier PR changes the exact-tree source identity of later candidates, but it does not create a synthetic conflict because no branch carries a mutable live source hash in a shared JSON file. The later PR still needs current-base integration CI before merge; source provenance is simply derived from whatever exact tree is tested.

Stacking remains useful when one change genuinely depends on another. It is a development convenience, not an integration topology. A stacked child may run tests before its parent lands, but it must remain visibly non-merge-ready until it is reconciled directly onto `main`. If the parent changes after the child was prepared, reconcile the child again rather than carrying a stale merge tree forward.

Do not use squash or rebase blindly when downstream branches rely on parent ancestry. If preserving ancestry is required for an active train, use a normal merge commit or explicitly rebuild downstream branches after the parent lands. The chosen method must be deliberate and visible in the PR.

## Provenance and frozen evidence

Never solve an integration problem by editing historical experiment artifacts, retroactively changing denominators or weakening verification. Frozen experiments remain frozen. New instrumentation or methodology belongs to a new experiment generation.

`source_sha256` is exact-tree provenance, not a frozen generation declaration. `local_agent.provenance.source_sha256()` hashes the behaviour-affecting tree and `package_identity()` records that value with runs/packages. This remains exact and reproducible without forcing every ordinary source PR to edit the same repository line.

`base_prompt_sha256` and `outcome_contract_sha256` are different: they are protected experiment contract axes and remain declared in `internal/INSTRUMENT.json`. CI fails closed if either differs from the declared Generation-2 value.

Historical generation records may retain the source identity that actually produced them. Removing the mutable *current-tree* source declaration does not remove or rewrite historical source provenance.

## Practical rule

The safe merge train is deliberately boring:

- independent branches may be prepared and tested in parallel;
- genuine dependencies may use stacked early CI;
- no ordinary source PR edits a shared live-hash declaration;
- reconcile each integration candidate onto current `main`;
- derive exact source provenance from that tested tree;
- keep frozen contract axes fail-closed;
- require all authoritative CI green;
- merge and confirm reachability;
- continue through the queued PRs.

This preserves deterministic integration and reproducibility without manufacturing conflicts that have nothing to do with the code being changed.
