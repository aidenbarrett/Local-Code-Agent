# Session contract source identity

The executable Session Hub v1 event validator loads its normative JSON Schema from `internal/docs/session-contract/v1/` at runtime. That versioned JSON contract is therefore part of the behaviour-affecting source surface even though it lives under `docs/`. The prose README in the parent `session-contract/` directory remains outside the source hash.

`source_sha256` must move when any normative runtime session-contract JSON schema changes. Model-facing and outcome-facing experimental contract identities remain separate and do not move merely because the source surface is widened.

Regression coverage mutates an actual validation bound in the v1 event schema and requires `source_sha256` to change. This protects against omissions caused by a missing glob, an overly narrow pattern, or a skip rule that makes the runtime schema invisible to provenance.
