# Endpoint scheduler design

Status: **design only**. No endpoint scheduler implementation is present in the Session
Hub source tree.

The eventual scheduler owns endpoint leases and queue admission, not model policy. The
initial contract is:

- one active lease per physical inference endpoint by default;
- bounded admission and explicit deadlines;
- fair FIFO within a role/class;
- interactive chat/control may receive a slot between worker calls, but active calls are
  not preempted;
- cancellation of queued work must be explicit and observable;
- endpoint identity is derived from the normalized endpoint plus served model/revision
  and device, not a friendly profile name;
- two logical contexts on one endpoint do not imply two weight copies or simultaneous
  generation;
- mutation work is never preempted in the middle of a side effect.

A future runtime API may expose an endpoint lease operation and queued-task
cancellation, but this document intentionally does not create an importable Protocol.
`internal/local_agent/session/scheduler.py` should only return when there is real runtime
behaviour or an interface consumed by runtime code.

See [Session Hub design](session-hub-design.md), especially the endpoint arbitration
section, and [file layout](session-hub-file-layout.md) for the intended implementation
location.
