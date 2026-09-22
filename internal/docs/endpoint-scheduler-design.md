# Endpoint arbitration design

Status: **one in-process endpoint authority exists; public client/runtime composition remains incomplete**.

`internal/local_agent/session/endpoint_lease.py` is the policy owner for endpoint admission, queueing, lease ownership and quarantine. `EndpointArbiter` is the only supported in-process arbitration authority. `EndpointRuntime` and `EndpointCallAdapter` compose that policy into runtime calls.

Do not introduce a second scheduler, queue, lease vocabulary or endpoint owner beside this stack. New behaviour belongs in these existing boundaries unless a materially different ownership domain is demonstrated first.

The current authority provides:

- one active non-preemptive lease per endpoint ID;
- bounded chat and work queues;
- explicit chat/work fairness rather than one undifferentiated FIFO queue;
- exact request and lease identities;
- queued-request removal without pretending to cancel active inference;
- endpoint quarantine and explicit reconciliation;
- fail-closed lease release while endpoint completion is uncertain;
- runtime/call adapters that preserve ownership across the supported in-process call path.

The physical endpoint identity used by product composition must be derived from effective endpoint properties, not a friendly profile name. URL/model/revision/device normalization belongs at the composition/configuration boundary before `EndpointRequest` is created. Credentials must never become durable endpoint identity.

Remaining P1 composition work:

- wrap the real conversation and worker inference calls with the existing endpoint authority;
- define one canonical endpoint identity mapping for aliases/profiles;
- surface waiting/acquired/released/quarantined state truthfully from that same implementation;
- connect queued cancellation and unresolved active cancellation to the cancellation runtime;
- quarantine capacity when inference completion is unknown rather than releasing it as healthy;
- either provide a shared owner for supported multi-process topologies or explicitly refuse/document concurrent-process use. The current arbiter is process-local and must not be described as cross-process protection.

Durable task state remains separate from process-local hardware ownership. Restart/recovery must never infer that an old process-local lease still owns hardware.

See [Session Hub design](session-hub-design.md) and [file layout](session-hub-file-layout.md).