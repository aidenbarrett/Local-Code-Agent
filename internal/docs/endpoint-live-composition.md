# Endpoint live inference composition

This change closes the next R08 join for the public Session Hub: conversation and admitted worker model calls are composed through the existing `EndpointArbiter` -> `EndpointRuntime` -> `EndpointCallAdapter` authority instead of constructing clients that bypass it.

The user-visible failure being removed is concurrent Session Hub inference reaching one physical endpoint without the queue/lease policy that the product already claims as its in-process endpoint authority. Conversation requests carry the durable session identity. Worker requests are bound only inside an admitted task execution scope and carry the exact durable task UUID and execution epoch. Calling the managed worker factory outside that scope fails closed.

This does **not** claim cross-process arbitration, server-side inference cancellation, physical-device proof, or complete Stop semantics. A client exception quarantines the in-process endpoint because Python-side failure is not proof that inference stopped; explicit reconciliation remains required. Explicitly split chat/worker base URLs get separate endpoint authorities, while identical base URLs share one authority.

Behavioral coverage exercises clean lease release, admitted worker authority, refusal outside an admitted scope, and quarantine after transport failure. Existing endpoint-runtime tests continue to own FIFO, bounded queue and reconciliation policy. Frozen experiment artifacts and historical methodology are untouched.
