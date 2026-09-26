# Endpoint performance harness

This is the one performance harness for Local Code Agent. It is backend-agnostic and talks only to an OpenAI-compatible chat endpoint plus optional generic observation hooks. Machine-specific profiles are local files and are not committed.

It reports cold start to first ready when a profile supplies a start command, backend-reported compile time only when an explicit telemetry hook exposes it, client-observed time to first token and decode tokens/second, a context ladder (2K, 8K and 32K by default) that stops at the first rejected size, endpoint-observed cancellation latency when the backend can prove request lifetime, and a configurable multi-hour fixed-task soak with client RSS, optional backend memory, errors and TTFT drift.

Client timings are measured at the socket boundary. Backend timings are kept in separately named fields. Closing a stream is never treated as proof that inference stopped. If the endpoint cannot expose active-request state, cancellation latency is reported as unsupported rather than invented.

The JSON report records configured model, runtime and device identity and verifies the model against `/v1/models`. A profile can optionally point at a generic identity endpoint to prove model/runtime/device identity from the backend as well.

Example:

```text
python internal/perf/endpoint_harness.py --profile .local-agent/perf/my-endpoint.toml --cold-start --cancellation --soak-hours 4 --output run.json
```

Copy `internal/perf/profile.example.toml` to a local ignored path and fill in the real endpoint identity and any optional hooks. Keep backend-specific launch details in that local profile, not in the harness.
