# Proposed event and hand-off contract: lca.session.events/1

Design artifact only. Normative shape: [v1/events.schema.json](v1/events.schema.json).
This is not the current prototype's EventBuffer format. No runtime emits this
schema yet. The version is reserved for implementation/review, not declared stable
because this document exists. Freeze it when the first producer/consumer PR agrees.

## Envelope and ownership

Every event has exactly: `schema_id="lca.session.events"`, `schema_version=1`,
`event_id: UUID`, `stream_id: UUID`, `stream_kind: durable|ephemeral`,
`sequence: positive integer`, `producer_epoch: UUID`, `session_id: UUID|null`,
`task_id: UUID|null`, `occurred_utc: RFC3339 UTC string`, `kind`, typed `payload`.
No additional fields. UUIDs use standard hyphenated form. Hashes are lowercase
64-digit SHA256. Dates use UTC Z, including across time zones and DST.

Task/tool events require task ID. Headless watch streams may have null session ID.
The service writer, not the model/UI, assigns IDs, timestamps and sequences.
Payload validation occurs before publication and again at a persisted-data/client
boundary. JSON Schema shape checking alone does not authorise a transition.

## Exhaustive v1 vocabulary

All fields in each shape are required; `?` below means an explicitly nullable
value, not a missing field. `ArtifactRef`, `TurnRef` and `TaskCompletion` are typed
in the schema. Maximum strings/arrays are constrained there; event frame cap is
64 KiB encoded UTF-8. Files, full logs, prompts and tool arguments use artifacts.

| Kind | Typed payload fields | Durable? |
|---|---|---|
| `session.opened` | conversation_id?, repository_id, controller_commit, capabilities[], recovered:bool | Yes |
| `turn.recorded` | turn_ref:TurnRef, role:user/assistant, content_ref:ArtifactRef | Yes |
| `route.proposed` | route_id:UUID, revision:int, turn_ref, source:rule/model_proposal/user_direct, mode:conversation/work/clarify, skill?, rule_id?, explanation, requires_acceptance:bool | Yes |
| `route.resolved` | route_id, revision, resolution:accepted/corrected/rejected, mode, source:user/controller, skill? | Yes |
| `task.admitted` | origin:Origin, request_ref, contract_sha256, repository_id, skill?, execution_epoch:int, deadline_utc | Yes |
| `task.state_changed` | previous:TaskState, current:TaskState, reason_code:Reason, execution_epoch | Yes |
| `endpoint.state_changed` | endpoint_id, role:conversation/worker/strong, state:waiting/acquired/released/quarantined, queue_position?, wait_ms:int, lease_id? | Yes |
| `tool.started` | call_id, tool_name, arguments_ref?, execution_epoch, deadline_utc | Yes |
| `tool.finished` | call_id, tool_name, execution:ok/blocked/error/interrupted/unknown, domain:pass/fail/unknown, reason_code, exit_code:int?, duration_ms:int, evidence_ids[], result_ref?, execution_epoch | Yes |
| `artifact.recorded` | artifact_ref, purpose:request/result/evidence/log/analysis/delta/turn, truncated:bool | Yes |
| `task.cancel_requested` | request_id, source:user/deadline/shutdown, reason_code, execution_epoch | Yes |
| `task.verdict` | completion:TaskCompletion | Yes |
| `task.closed` | status:TerminalState, result_ref, cleanup:confirmed/not_needed/unknown | Yes |
| `watch.state_changed` | job_id, job_revision, schedule_revision, state:enabled/paused/disabled/skipped, reason_code, due_utc?, next_due_utc? | Yes |
| `watch.run_recorded` | job_id/revisions, execution_contract_sha256, run_id, task_id, due_utc?, started_utc, finished_utc, status, verdict, previous_attempt_id?, comparison_run_id?, comparison:BASELINE/COMPARABLE/INCOMPARABLE/NO_CURRENT_RESULT, counts:Counts?, delta_ref?, next_due_utc? | Yes |
| `telemetry.policy` | visible, requested_sampling, effective_sampling:bool, run_mode:operational/measured/scored, reason_code, sampler_ids[], poll_interval_ms?, manifest_ref?, exclusion_lease_id? | Yes |
| `fault.reported` | reason_code, message, recoverable:bool, detail_ref? | Yes |
| `telemetry.sample` | host_id, device_id, source, sampled_utc, utilization_percent:number?, memory_bytes:int?, stale:bool, unavailable_reason? | No |
| `conversation.delta` | turn_request_id, chunk_index:int, text, final:bool | No |

This set is exhaustive: no `worker.*` wildcard and no arbitrary dictionary passed
through from the orchestrator. Approval request/resolution is deliberately absent
from v1: source mutation is disabled in the first workflows. Add approval types
in a negotiated later contract before enabling writes, never overload route.accept.

Minimum activity projections: route.proposed/resolved -> selection;
task.admitted/state_changed -> current operation; endpoint.state_changed -> wait;
tool.started/finished -> concrete action; task.verdict/closed -> result. Show these
automatically. Artifact and token events do not each create a new activity row.
Watch projects watch events into its own mandatory pane. Telemetry never drives
task lifecycle or proof.

## Shared types and semantic validation

`Origin` is a disjoint union: user_direct(TurnRef), user_rule(TurnRef,rule_id),
model_proposal(TurnRef,proposal_id), watch(job_id,run_id). The model cannot choose
its own origin or forge a user_direct request. Only the input controller supplies
these fields. `route.resolved` from the user is tied to its current route revision.

`ArtifactRef` includes artifact ID, SHA256, media type, byte size and availability
(retained/expired/unavailable). It is never a model-supplied path or URL. The store
resolves it under an allowed root, checks containment/hash and streams bounded
chunks. An immutable Turn artifact snapshots one stored Turn, not the growing
conversation JSON file. It follows the same private-content policy and caps.

`TaskCompletion` contains task ID, terminal lifecycle state, `VerdictBlock`, an
optional labelled worker-analysis artifact and immutable result artifact.
`VerdictBlock` contains verdict, reason code, exact verification scope, canonical
evidence IDs, optional tree digest and controller-rendered lines. Its schema and
rendered text are validated together: the result must match the verifier record,
rendered output must match the deterministic renderer, and every evidence ID must
resolve inside that task/attempt. Model-generated text never populates this type.

Additional required checks (not expressible solely as independent JSON fields):

- Legal state transition, expected prior revision, matching active execution epoch.
- Nested task IDs equal the envelope task ID; a tool call finishes at most once.
- VERIFIED requires successful required proof of the declared scope at its tree
  digest, usable evidence references, and controller-owned verifier attribution.
- NOT_REQUIRED is valid only when the admitted verification plan requires no proof.
- REFUSED requires a recorded refusal, FAILED an observed verifier failure,
  NO_VERDICT explicitly represents unknown or incomplete verification.
- No `task.verdict` except the single writer; exactly one final result per task.
- Terminal state/result/cleanup agree. Unknown cleanup cannot report clean cancel.
- COMPARABLE watch deltas require both run IDs and matching contract identities;
  noncomparable/unknown runs must not populate regression/recovery counts.
- effective_sampling=false means no collector instance/timer is running, not just
  a hidden display. Measured/scored policy requires the documented admission lease.

## Ordering, delivery and backpressure

Each durable stream has one writer and strictly increasing committed sequence
numbers. No global ordering across sessions or watch streams is promised. Task
events stay in the owning stream; watch.run_recorded references a completed task
and its result, but is not ordered relative to another stream by timestamp.

For a task: admitted before tool events; tool.started durably precedes dispatch;
tool.finished follows reconciliation; verdict precedes closed. Terminal state,
verdict, closed and result index are committed in one transaction. Critical state
events are delivered after commit, never optimistic “success” before persistence.
Tools are sequential initially; do not infer parallel tool completion from indices.

At-least-once replay may duplicate deliveries. Consumers deduplicate event_id or
(stream_id,sequence), reject contradictions and resnapshot after a gap. Effects
are never driven by event replay. A lost response cannot generate a second task:
admission checks idempotency key plus payload digest.

Ephemeral streams have independent sequence/epoch and may drop/coalesce samples
or text deltas. They never create gaps in durable sequence numbers. Final chat
text is committed through turn.recorded; missing chunks are replaced by that final
text, not guessed. Control messages have their own bounded queue and priority.

In-process delivery buffer: 512 durable notifications, plus latest telemetry per
device and at most 256 KiB pending chat text. Slow UI drops notifications only
after events are committed, then receives an explicit gap/resnapshot signal; it
does not stall a subprocess output drain. Terminal status remains in the store.
Storage failure blocks new effects. If it occurs during a tool, terminate/fence
as possible and recover NO_VERDICT; the UI must not claim durable completion.

## Persistence, caps and recovery

Planned paths under managed runtime root (never frozen experiment directories):

| Path | Content |
|---|---|
| `chat/<conversation_id>.json` | Existing schema-v1 raw conversation |
| `hub/state.sqlite3` | Session/turn references, jobs, attempts, leases, event log, outbox |
| `hub/tasks/<task_id>/request.json` | Immutable admitted task and contract identity |
| `hub/tasks/<task_id>/result.json` | One immutable final result |
| `hub/artifacts/<artifact_id>` | Content-address-checked logs, evidence, turn snapshots, deltas |

SQLite transactions own state+event sequence; artifact writes use temp+fsync+rename
before index publication. On restart, verify referenced artifacts and reconcile
orphan temporaries without executing their tasks. WAL is not tamper-proof evidence.

Initial configurable caps: chat retains its existing 1 MiB file cap; durable hub
events 16 MiB per stream; task artifacts 100 MiB per task and 10 MiB per individual
log; hub total 2 GiB including artifacts/WAL; ephemeral buffers as above. Reserve
at least 2 MiB for terminal metadata before admission. Byte accounting includes
pending writes; actual disk errors still require refusal/recovery. Do not let the
OS scheduler admit work after the cap check fails.

Retain completed data for up to 30 days within cap. Pin active tasks, unresolved
cleanup and the last attempt plus last comparable watch baseline. Evict oldest
eligible completed artifacts/closed streams first, preserving small tombstones
and retained_from_sequence. If pins prevent staying within cap, refuse admission.
Never prune active event streams silently: close/rotate them at a safe idle boundary
or refuse further admission until retention can proceed. Evidence removed by
retention becomes unavailable, not valid proof from a remembered badge. Log cap
truncation is explicit; if required verification output/report is incomplete,
NO_VERDICT. Output draining continues to avoid child pipe deadlock.

Unknown schema_id/version/event kind or enum: preserve raw bytes for diagnosis,
show protocol incompatibility, refuse new client task actions and do not render
verification badges from it. Startup negotiates exact version and capabilities
before subscribing. There is no silent downgrade or reinterpretation. Changes to
mandatory fields, meanings, enums or vocabulary require a new supported version.

## Required fixture traces before implementation merges

1. Direct work -> tool -> NOT_REQUIRED, no chat classifier call.
2. Rule-routed build -> tool failure -> FAILED, model prose cannot turn it green.
3. Model proposal -> one-word correction -> chat, no repository effect.
4. Queued chat behind worker -> lease handover, contexts remain isolated.
5. Cancel during tool -> partial log -> unknown cleanup -> NO_VERDICT.
6. Worker timeout -> late result discarded -> one terminal record only.
7. Crash after effect before result persistence -> interrupted, no automatic retry.
8. Duplicate event/request -> one rendered/result/task identity.
9. First watch -> BASELINE; comparable regression; contract change -> INCOMPARABLE.
10. Telemetry quiet handshake failure -> measured-run refusal before measurement.
11. Unknown protocol -> no badge; retention gap -> explicit resnapshot.
12. Saved task references survive conversation resume without tool replay.

Validate schema shape and these stateful semantics separately. A schema-valid
model-forged verdict must still fail the producer/verification trust boundary.
