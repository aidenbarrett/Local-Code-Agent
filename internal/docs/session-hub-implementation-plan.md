# Session Hub implementation plan

Status: active implementation sequence after PR #48.

This document records the implementation order so the accepted design in
`session-hub-design.md` is not mistaken for the current delivery sequence.
It does not change frozen experiment methodology, evaluation semantics, or
historical artifacts.

## Landed foundation

PR #47 established the conversation gateway/session contract and Session Hub
design. PR #48 then landed the first execution/session foundation slice:
lock-scoped conversation ownership, closed product outcome semantics including
`NO_VERDICT`, executable Gen2 outcome-contract freeze gates, route-source
provenance, and fail-closed unknown-execution handling.

## #49 — durable event service and replay

Make the v1 event contract executable and durable before any UI depends on it.

Required properties:

- validate every produced event envelope and payload before commit/publication;
- one authoritative service writer owns sequence assignment and durable task
  lifecycle state;
- durable task admission precedes execution;
- terminal state and verdict are persisted independently from conversation prose;
- replay reconstructs the same observable projection as live delivery;
- admitted-without-terminal after restart resolves to `NO_VERDICT` / unknown
  effects and is never auto-retried;
- submission is non-blocking and returns a task ID immediately;
- subscriptions cross worker/UI thread boundaries safely through bounded queues;
- overflow/gap behaviour is explicit and detectable rather than silent;
- cancellation requests are thread-safe; cancellation truth still depends on
  owned process-tree cleanup and fencing from the execution foundation;
- task/verdict/evidence records remain sibling artifacts and never become model
  conversation history.

Acceptance gates must be behavioural where possible: durable admission before
execution, replay equality, no duplicate effects on recovery, monotonic unique
sequences, bounded delivery/gap signalling, and single-writer ownership.

## #50 — deterministic routing and controlled watch execution

Land explicit Work/Chat routing, anchored deterministic rules, model proposal only
as fallback, fixed watch `Run now`, stable watch identity/revision, and comparable
deltas. OS scheduling is later and is not a prerequisite for the first watch
workflow.

## #51 — Textual Session Hub shell

Build the UI against fixture events and the #49 service interface. The Textual
event loop must never block on inference, subprocess waits, or disk-heavy work.
Missing/unknown state stays visible; the UI must not manufacture success.

## #52 — live wiring and physical acceptance

Connect the Textual shell to the real service/controller/endpoint arbitration and
run the physical laptop acceptance flow: resume + inspect, run/verify, visible NPU
queueing, cancellation, restart without repeated effects, and repeated watch delta.
Operational telemetry may be added only if measurement exclusion is explicit.

## Deferred beyond the Hub milestone

Source mutation, repair, commit/push workflows, exact-diff approvals, and broader
OS scheduling remain later work. `Fix it` must continue to refuse while mutation
is unavailable.
