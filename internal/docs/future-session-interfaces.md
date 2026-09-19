# Future Session Hub interfaces

Status: design only. The interfaces described here are not runtime modules and must not be imported as if they were implemented product capabilities.

They used to live as Python Protocol skeletons under `local_agent/session/`. That made the source tree look more complete than the product actually was. Their design intent is kept here until a feature PR has a real implementation and behavioural tests.

## Endpoint scheduler

The first scheduler implementation should default to one lease per endpoint, fair FIFO queues, deadlines, bounded admission, and explicit chat/control priority between worker calls. Never preempt a mutation mid-write.

Endpoint identity is the normalized endpoint URL plus served model/revision and device, not a friendly profile name. Two contexts on one endpoint do not imply two loaded weight copies or simultaneous generation.

A future runtime scheduler must support acquiring a lease for a task and atomically cancelling queued work. Behavioural acceptance belongs with the implementation, not with this design note.

## Session transport

The first approved transport direction is stdio for VS Code Remote SSH, followed only later by an explicitly designed loopback GUI transport if still needed.

Versioned messages are expected for session creation, turn submission, task cancellation, approval response, event subscription, and session snapshots. Every mutating request needs a unique request ID and expected session revision. Frame size, queue bounds, replay-cursor gaps, and cancellation acknowledgements must be tested before exposure.

For Remote SSH, gateway/tools live beside the Linux repository; inference may sit behind explicitly configured authenticated tunnelling to Windows. No wildcard HTTP binding. Any loopback transport still needs an unpredictable bearer token and Origin validation. User input/approval channels remain separate from model-output events.
