# Retained task detail surface

The Session Hub must let a user inspect a completed task without asking the model to remember or reconstruct what happened.

This slice adds a deterministic local `/task <uuid>` control that resolves one already-materialized durable task from the current feed and renders its authoritative task identity, state, verdict, scope, evidence ids, retained worker answer and verification facts. The command never calls the conversation model and never grants execution authority.

A missing task, a non-terminal task without a retained answer, or a feed-integrity failure is reported as unavailable rather than synthesized. Retained worker prose remains labelled as worker output; deterministic verdict/scope/evidence stay visually separate.

The selection is presentation-local and is cleared by `/task clear`; durable task/result authority remains the event store plus retained artifact bytes. This does not add artifact-log expansion, mutation authority, Windows containment, complete Stop semantics or physical-NPU support claims.
