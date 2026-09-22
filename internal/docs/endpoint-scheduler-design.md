# Endpoint scheduler design

Status: **bounded in-process lease arbitration implemented; runtime/client wiring still pending**.

`internal/local_agent/session/scheduler.py` owns deterministic access to a physical
inference endpoint identity. It deliberately does not choose models, routes or task
policy. The implemented boundary provides:

- one active non-preemptive lease per physical endpoint;
- bounded FIFO queueing and explicit monotonic deadlines;
- explicit queued-request cancellation that never pretends to cancel an active call;
- endpoint quarantine that wakes/refuses queued work and blocks new acquisition until
  an owner explicitly clears it;
- inspectable active/queued/quarantined state;
- endpoint identity derived from normalized endpoint URL plus served model revision and
  device, never from a friendly profile name;
- embedded endpoint credentials are rejected rather than normalized into identity.

The remaining runtime composition must preserve these rules:

- interactive chat/control may receive a slot between worker calls, but active calls are
  not preempted;
- cancellation of an active inference call belongs to the runtime/client cancellation
  path; unresolved cancellation quarantines the endpoint rather than returning the lease
  as healthy;
- two logical contexts on one endpoint do not imply two weight copies or simultaneous
  generation;
- mutation work is never preempted in the middle of a side effect;
- queue admission and lease state may inform presentation, but UI state cannot create or
  release execution authority.

This scheduler is intentionally process-local today. Durable task state remains the
Session Hub authority across restart; reconnect/recovery must not infer that an old
process-local lease still owns hardware. Shared-daemon or cross-process arbitration, if
needed later, requires a separate ownership design rather than silently treating this
object as distributed locking.

See [Session Hub design](session-hub-design.md), especially the endpoint arbitration
section, and [file layout](session-hub-file-layout.md) for the intended composition
location.
