# Textual hub client design

Status: **design only**. There is no Textual UI package in the source tree yet.

The current client target is a Textual application running in Windows Terminal or an
SSH terminal, in-process with the Session Hub gateway/controller and subscribing
directly to validated events. The first hub is not a web frontend, Electron app or
terminal emulator.

The mandatory panes are conversation, activity/evidence, watch, and a
controller-owned verdict view. Operational telemetry is optional and must remain
quiesced during measured/scored runs unless a later measurement protocol explicitly
permits it.

The eventual UI should keep palette roles in one reviewed theme source, provide a
high-contrast/NO_COLOR fallback, and derive any Windows Terminal scheme from the same
palette rather than duplicating values.

Do not create `internal/ui/` merely to hold notes. That path is reserved for a real
implementation. When UI code exists, adding the directory will require an explicit
hashed-surface classification decision.

See [Session Hub design](session-hub-design.md), the
[event contract](session-contract/README.md), and the
[implementation destinations](session-hub-file-layout.md). Plain chat persistence,
durable correctness, fixed watch execution and cancellation/ownership gates remain
prerequisites for the first real panes.
