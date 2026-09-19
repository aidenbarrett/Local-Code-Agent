# VS Code client design

Status: **future design only**. There is no extension manifest or installable VSIX in
the repository.

The immediate client is the in-process Textual Session Hub. A VS Code client remains a
later possibility, particularly for Windows VS Code Remote SSH into a Linux repository,
but it is not a dependency or approved implementation scope for the first hub.

A future implementation would need, at minimum:

1. Workspace-host extension activation and explicit repository admission.
2. A versioned gateway/control protocol with capability negotiation.
3. Conversation and activity views over the same durable event/result schema as the
   terminal hub.
4. Native diff/log navigation using host-qualified artifact identities, never arbitrary
   model-supplied URLs or filesystem paths.
5. Scoped approval and cancellation controls kept separate from model output.
6. Remote SSH placement rules: repository/controller work beside the Linux checkout;
   Windows inference only through explicitly configured connectivity.
7. Credential storage in VS Code's secret store, excluded from prompts, events and logs.
8. Crash/reconnect and idempotency tests proving extension reload cannot replay effects.

Do not create `internal/vscode/` merely to hold plans. That path is reserved for a real
client implementation. When such source exists, adding the directory will require an
explicit hashed-surface classification decision.

The first acceptance target for that later client would be a Windows VS Code Remote SSH
window that can inspect and test the selected Linux workspace while keeping model-host
telemetry and repository-host facts separate. No global filesystem authority or SSH
command construction is delegated to a model.
