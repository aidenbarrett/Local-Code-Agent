# Future Textual client

Status: design only. No Textual UI runtime is implemented here yet.

The current target is Textual in Windows Terminal or an SSH terminal, in-process with the gateway/controller and subscribing directly to validated events. No web frontend, Electron, or terminal emulator is part of this client.

Mandatory panes: conversation, activity/evidence, watch, and a controller-owned verdict widget. Telemetry is optional and its collectors must be off/quiesced during measured or scored runs.

Use named palette roles from one future `themes/lca.json` source with high-contrast and `NO_COLOR` fallbacks. Export any Windows Terminal scheme JSON from that same source rather than duplicating palette definitions.

See [Session Hub design](session-hub-design.md), [event contract rationale](session-contract/README.md), and [implementation destinations](session-hub-file-layout.md).

Plain chat persistence and the durable task/event layer must be trustworthy before these panes become load-bearing.
