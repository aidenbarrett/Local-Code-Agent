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

A healthy owned process may be reused only when the configured model/device launch identity matches. Startup failure over an already-owned stale process may be reconciled once by stopping that proven-owned process and retrying. Unknown ownership fails closed.

## Product qualification

`internal/serving/qualify_server.py` is the small deterministic product conformance check used by workstation setup. It checks endpoint/model identity, plain chat, a schema-constrained tool call and bounded context probes. It is not a performance benchmark.

Performance comparisons belong exclusively to `internal/perf/endpoint_harness.py`. Client-boundary timing and endpoint-reported telemetry remain separate there.

## Runtime state

Managed runtime state lives outside the checkout under the configured runtime root. Process records include ownership facts and launch configuration; model weights and compiled caches also remain outside source control.

Status distinguishes process liveness, HTTP readiness and expected model presence. A successful HTTP response alone never proves device identity or hardware utilisation.

## Hardware claims

A configured device name is not physical acceptance. Hardware support claims require the public product journey to be exercised on the actual machine with observed runtime/device evidence. See `panther-lake-acceptance.md` for the current physical acceptance procedure.
