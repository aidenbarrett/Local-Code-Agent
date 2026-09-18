# Desktop client skeleton

Status: design only. No frontend package, desktop process or local listener yet.
Build after the service protocol, not by scraping PowerShell output.

Proposed module map:

- `client/transport`: typed command/RPC client, authenticated events, reconnect.
- `state/session`: reducer driven only by versioned controller events/snapshot.
- `components/Conversation`: text and explicit historical task references.
- `components/Activity`: read-only task/attempt/tool groups and timestamps.
- `components/TaskResult`: typed outcome, verification scope and artifact links.
- `components/Approval`: exact pending diff/action; disables stale requests.
- `components/RuntimeStrip`: host, endpoint, device, TTFT, token rate, CPU/RAM,
  available GPU/NPU counters, sample age and missing-data state.
- `components/DiffReview`: escaping, size bounds, paths, explicit selection.
- `tests/fixtures`: recorded synthetic protocol streams, never invented telemetry
  presented as a live device reading.

Choose the Windows desktop wrapper after a packaging/accessibility spike.
Business logic stays in Python. Do not add a second policy engine in JavaScript.
Native packaging is not required for the initial loopback web client.

Minimum scenarios before release: reconnect mid-task; stale approval; cancellation
in progress; missing telemetry; a failed test despite optimistic model prose;
duplicate event; out-of-order event; a repository file containing malicious HTML.

See `../docs/conversation-product-architecture.md` sections 10 and 11.
