# Typed proof and repository-tree binding

This change closes the next requested-versus-proved-scope join for product task results.

A worker result now carries one controller-produced `proof_binding` with four facts: the SHA-256 of the exact task text given to the worker, the typed proof scope actually established by current-epoch tool evidence, the SHA-256 of the nonignored repository tree at completion, and the evidence ID that supplied that proof. Durable verdict rendering consumes and validates that binding instead of treating a loose `metrics["tree_sha256"]` value plus a generic scope sentence as sufficient proof.

A verified worker completion requires a current full-build or full-test proof scope. Targeted passes remain useful evidence but cannot be upgraded into whole-tree success. A full proof from an earlier mutation epoch is stale and fails closed rather than being rebound to the current tree. Failure and read-only results can carry narrower or `none` proof scope without becoming success-shaped.

The repository-tree digest is product proof identity, not experiment provenance. It binds Git HEAD/index plus tracked and nonignored untracked working-tree bytes; ignored build outputs remain outside this contract. Frozen experiment artifacts and historical scoring are unchanged.

Self-check retains its existing dedicated tree digest path in this slice. The next build/failure acceptance work should consume these typed scope/tree facts through positive, negative, partial, stale and unavailable public journeys rather than invent another proof vocabulary.
