# VS Code client skeleton

Status: design only. No extension manifest or installable VSIX in this branch.

Deferred by the session-hub refinement. The immediate client is in-process Textual,
not a webview/desktop client. The extension ideas below are future possibilities,
not dependencies or approved implementation scope for the first hub.

Required implementation pieces:

1. Workspace-host extension activation and explicit repository admission.
2. Gateway child process over stdio with version/capability handshake.
3. Webview conversation/activity client using the same event/result schema as desktop.
4. Native diff/log navigation using host-qualified artifact IDs, never arbitrary
   model-supplied URLs or filesystem paths.
5. Scoped approval UI and cancellation commands over separate control messages.
6. Remote SSH detection: spawn controller beside the Linux repository; resolve
   inference endpoint through explicit tunnel configuration to Windows if needed.
7. Credential storage in VS Code's secret store, excluded from events/logs/prompts.
8. Crash/reconnect and idempotency tests; do not replay work on extension reload.

Acceptance: a Windows VS Code Remote SSH window can inspect and test the selected
Linux workspace through its local gateway while showing inference metrics from
the Windows model host separately. No global filesystem access or SSH command
construction is delegated to a model.
