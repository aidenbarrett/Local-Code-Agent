# Retained result feed integrity

The Session Hub must never silently omit a retained terminal result that durable task state says exists.

This slice hardens the materialized feed/result join: terminal task projection, retained result reference and integrity-checked artifact bytes must agree before the Hub exposes the task. Missing or corrupt retained bytes remain explicitly unavailable rather than being rendered as an ordinary task with an absent answer.

The retained worker answer remains untrusted prose. Deterministic verdict, scope, evidence and verification facts remain separate authorities. This change does not add mutation authority, Windows containment, complete Stop semantics or physical-NPU support claims.
