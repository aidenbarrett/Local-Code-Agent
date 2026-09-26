# Endpoint performance harness

This is the one performance harness for Local Code Agent. It is backend-agnostic and talks only to an OpenAI-compatible chat endpoint plus optional generic observation hooks. Machine-specific profiles are local files and are not committed.

It reports:

- cold start from launch to first ready when a profile supplies a startup command
- backend-reported compile time only when an explicit telemetry hook exposes it
- client-boundary time to first token and decode tokens/second
- a context ladder, 2K/8K/32K by default, stopping at the first rejected size
- client-observed prompt-to-first-token latency for each context size, plus backend-reported prefill time only when explicitly exposed
- endpoint-observed cancellation latency only when the endpoint can prove request lifetime
- a configurable multi-hour fixed-task soak with client RSS, optional backend memory, errors and TTFT drift

## Measurement rules

Client timings are measured at the socket boundary. Backend timings are stored under separately named `backend_reported_*` fields and never substitute for client observations.

A cold-start run must establish cold state. If the endpoint is already ready and the profile has no stop command, cold start is reported as unsupported rather than pretending the existing process is cold. When the harness starts the endpoint, it keeps that same endpoint alive through the rest of the requested measurements and stops it only after the report is complete.

Closing a stream is never treated as proof that inference stopped. Cancellation requires an endpoint-side request ID plus an active-request observation hook. The harness proves the request is active before disconnect, then measures until the endpoint no longer reports it. Without that evidence, cancellation latency is unsupported.

The JSON report records configured model, runtime and device identity and verifies the model against `/v1/models`. A profile may also point at a generic identity endpoint to prove model/runtime/device identity from the backend.

## Run it

Copy `internal/perf/profile.example.toml` to an ignored local path, fill in the exact endpoint identity and any optional hooks, then run for example:

```text
python internal/perf/endpoint_harness.py --profile .local-agent/perf/my-endpoint.toml --cold-start --cancellation --soak-hours 4 --output run.json
```

Keep backend-specific launch commands, telemetry URLs and request-observation hooks in the local profile, never in the harness source.
