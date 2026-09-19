# Future VS Code client

Status: design only. There is no extension manifest or installable VSIX in the repository today.

The VS Code client is deferred behind the in-process Textual Session Hub. These are future possibilities, not dependencies or approved implementation scope for the first hub.

A future implementation would need:

1. Workspace-host extension activation and explicit repository admission.
2. A gateway child process over stdio with a version/capability handshake.
3. A conversation/activity client using the same event/result schema as the terminal client.
4. Native diff/log navigation using host-qualified artifact IDs, never arbitrary model-supplied URLs or filesystem paths.
5. Scoped approval UI and cancellation commands over separate control messages.
6. Remote SSH detection: run the controller beside the Linux repository and resolve inference through explicit tunnel configuration to Windows when needed.
7. Credential storage in VS Code's secret store, excluded from events, logs, and prompts.
8. Crash/reconnect and idempotency tests; extension reload must not replay work.

Acceptance target: a Windows VS Code Remote SSH window can inspect and test the selected Linux workspace through its local gateway while showing inference metrics from the Windows model host separately. No global filesystem access or SSH command construction is delegated to a model.
