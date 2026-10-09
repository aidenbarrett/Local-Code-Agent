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

## Runtime measurement contract

Do not use `cold` and `warm` as aliases for `first request` and `later request`. Keep these clocks and facts separate:

- **Process launch to ready:** client-observed time from starting the configured runtime process until the readiness endpoint responds. This includes whatever the runtime does before readiness and must not be relabelled as model-load or compile time.
- **Model load/readiness:** report only when the backend exposes an explicit observation for it. Otherwise it is `unknown`; process readiness is not a substitute.
- **Compile/cache state:** report compile duration or cache hit/miss only when an explicit backend hook exposes it. A fast first request is not proof of a cache hit.
- **First inference after established cold start:** TTFT and request duration for the first measured generation after the harness has proved the endpoint was not ready, launched it, and observed readiness.
- **Warm inference:** the same measurement repeated against the same still-running endpoint, without a runtime restart or model/profile switch between the two requests.
- **Pre-existing endpoint:** if the harness did not establish the endpoint lifecycle, the first request is merely the first request observed by the harness. It must not be reported as cold.
- **Decode:** tokens per second after first-token arrival, kept separate from TTFT/prefill.

Cold and warm comparisons must state whether the endpoint process was reused. A model/profile switch, endpoint restart, failed readiness transition or unobserved lifecycle boundary starts a new measurement sequence rather than silently continuing a warm series.

## Residency and product cost

Latency is not enough to choose a deployed model. Record backend process memory or working set when it can be observed, with the source named. Client-process RSS is not backend residency. If backend memory cannot be observed, report it as unsupported or unknown rather than inferring it from model size.

Model comparisons used by agent routing or qualification should keep these dimensions side by side:

- verified task completion;
- process/readiness cost;
- first-after-cold-start inference latency;
- same-endpoint warm inference latency;
- decode rate;
- observed backend memory footprint;
- human intervention rate.

For product-level qualification, a **human intervention** is any human edit, human-triggered retry or re-run, or manual Stop/cancel required to reach verified completion. Record the reason for each intervention. Report interventions per attempted task and per verified completion so failed or abandoned runs cannot appear artificially better. Automatic controller retries and automatic recovery are separate machine events and must not be counted as human intervention.

The endpoint harness measures runtime behaviour, not task success or human intervention. Product acceptance/qualification owns those task-level facts and may join them with this harness output by retained run identity. Do not add a second task-verdict or telemetry authority here.

Persistence is an optimisation, not an authority change. A resident endpoint must not weaken Stop, cancellation proof, cleanup, ownership handoff, offline guarantees, or controller verification. Any persistence change that makes those guarantees weaker is a reliability regression even if warm latency improves.

## Run it

Copy `internal/perf/profile.example.toml` to an ignored local path, fill in the exact endpoint identity and any optional hooks, then run for example:

```text
python internal/perf/endpoint_harness.py --profile .local-agent/perf/my-endpoint.toml --cold-start --cancellation --soak-hours 4 --output run.json
```

Keep backend-specific launch commands, telemetry URLs and request-observation hooks in the local profile, never in the harness source.
