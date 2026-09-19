# Gateway transport design

Status: **design only**. The first Session Hub is in-process and does not require a
transport layer between the UI and controller.

A later VS Code / Remote SSH client may need a versioned gateway transport. If that
work is approved, the design constraints are:

- versioned request/message types for session creation, turn submission, cancellation,
  approval response, event subscription and snapshots;
- unique request identities and expected session revisions for mutating operations;
- bounded frame size and queues;
- replay cursor gap handling rather than silently dropping history;
- explicit cancellation acknowledgement;
- user-input and approval channels separate from model-output events;
- no wildcard HTTP binding;
- loopback network transports still require authentication and Origin validation;
- Remote SSH keeps repository tools beside the Linux checkout; Windows inference is
  reached only through explicitly configured connectivity.

The intended near-term remote shape is stdio if/when a VS Code client needs it. This
is not implemented today, and the old RPC package must not be mistaken for the future
Session Hub transport authority.

`internal/local_agent/session/transport.py` should only return when an actual runtime
consumer exists. Keeping an unused Protocol in source makes a future component look
implemented and moves source identity for design prose.

See [VS Code client design](vscode-client-design.md) and
[Session Hub design](session-hub-design.md).
