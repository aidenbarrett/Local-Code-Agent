# Local serving

Local Code Agent owns only the model-server processes it starts. Runtime lifecycle code lives under `internal/serving/`; user setup remains `install.ps1` and normal use remains `local-code-agent.ps1`.

## Product-owned serving

`internal/serving/serve.py` owns local runtime launch, readiness, status, logs and stop operations for configured profiles. It derives model, device, endpoint and context limits from `MODEL_PRESETS` rather than maintaining a second command table.

Examples for engineering/debugging:

```powershell
python internal/serving/serve.py start --profile ptl-npu-8b --dry-run
python internal/serving/serve.py pull --profile ptl-npu-8b
python internal/serving/serve.py start --profile ptl-npu-8b
python internal/serving/serve.py status --all
python internal/serving/serve.py logs --profile ptl-npu-8b --tail 60
python internal/serving/serve.py stop --profile ptl-npu-8b
```

Normal users should not need these commands. The public setup/chat/Session Hub paths call the same controller.

## Ownership rules

A reachable HTTP endpoint is not automatically ours. A process is reusable or stoppable only when the controller can establish its recorded ownership and compatible launch identity. A foreign endpoint, reused PID or ambiguous live process is never adopted or killed merely because it occupies the expected port.

A healthy owned process may be reused only when it was launched exactly as the current profile would launch it: the same resolved executable, arguments, child environment and model directory (`serve.launch_differences`, shared by `serve.start` and `serving/managed_runtime.ensure_managed_runtime`). Client-side settings such as temperature or timeouts are not server identity and never force a restart; a changed server argument always does. Startup failure over an already-owned stale process may be reconciled once by stopping that proven-owned process and retrying. Unknown ownership fails closed.

`ensure_managed_runtime` is the one owner of that decision for every product entrypoint (Session Hub, `run-task`, `chat`). Its outcome is typed: `reused`, `started`, `replaced` (with `dead`, `unhealthy` or `config_changed` and the differing launch fields) or `refused` (with `foreign_endpoint`, `start_failed` or `not_healthy`). A launch made by that call carries `launch_to_ready_ms`: the client-observed time from launching the process to the first healthy readiness poll, also stored with `ready_utc` in the profile's `process.json`. It is not model-load or compile time, and a reused server reports none.

## Product qualification

`internal/serving/qualify_server.py` is the small deterministic product conformance check used by workstation setup. It checks endpoint/model identity, plain chat, a schema-constrained tool call and bounded context probes. It is not a performance benchmark.

Performance comparisons belong exclusively to `internal/perf/endpoint_harness.py`. Client-boundary timing and endpoint-reported telemetry remain separate there.

## Native endpoint

`internal/native_endpoint/` builds `lca-endpoint`, a C++ OpenAI-compatible server with pluggable backends: a deterministic fixture, the in-process OpenVINO GenAI pipeline, and any runtime packaged as a plugin against the C ABI in `include/lca/backend_plugin.h`. It is the intended home for a direct device runtime: the runtime implements describe, count-tokens and generate; the endpoint keeps the protocol, prompt template, tool-call parsing, cancellation proof, identity and telemetry identical across runtimes.

It is not yet a serving profile. `serve.py` does not launch it and no preset points at it; that wiring follows a measured comparison on the target machine. See `internal/native_endpoint/README.md` for build, run, harness profile and known limits.

## Runtime state

Managed runtime state lives outside the checkout under the configured runtime root. Process records include ownership facts and launch configuration; model weights and compiled caches also remain outside source control.

Status distinguishes process liveness, HTTP readiness and expected model presence. A successful HTTP response alone never proves device identity or hardware utilisation.

## Hardware claims

A configured device name is not physical acceptance. Hardware support claims require the public product journey to be exercised on the actual machine with observed runtime/device evidence. See `panther-lake-acceptance.md` for the current physical acceptance procedure.
