# Textual hub client: design placeholder

No UI runtime is implemented here. The current target is Textual in Windows
Terminal or an SSH terminal, in-process with the gateway/controller and subscribing
directly to validated events. No web frontend, Electron or terminal emulator.

Mandatory: conversation, activity/evidence, watch, controller-owned verdict widget.
Optional: telemetry strip, with collectors off/quiesced during measured/scored runs.
Named palette roles in one future themes/lca.json file; high-contrast/NO_COLOR
fallback. Export Windows Terminal scheme JSON from the same source, never duplicate it.

See [current design](../docs/session-hub-design.md),
[event contract](../docs/session-contract/README.md) and
[implementation destinations](../docs/session-hub-file-layout.md).
Wire plain chat persistence and fixed watch execution before building these panes.
