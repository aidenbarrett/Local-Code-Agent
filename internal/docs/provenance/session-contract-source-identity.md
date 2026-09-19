# Session contract source identity

The executable Session Hub event validator loads its normative JSON Schema from `internal/docs/session-contract/` at runtime. Those JSON files are therefore part of the behaviour-affecting source surface even though they live under `docs/`.

`source_sha256` must move when any normative session-contract JSON schema changes. Prose documentation remains outside the source hash. Model-facing and outcome-facing experimental contract identities remain separate and do not move merely because the source surface is widened.

Regression coverage mutates an actual validation bound in the v1 event schema and requires `source_sha256` to change. This protects against omissions caused by a missing glob, an overly narrow pattern, or a skip rule that makes the runtime schema invisible to provenance.
