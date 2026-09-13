# Local serving

From a checkout, install `python -m pip install -e ".[dev]"`. The controller needs
psutil for process ownership. OVMS preflight also needs OpenVINO in that same
Python environment. On Windows, import the chosen OVMS package's `setupvars.ps1`
in each new terminal. The one-shot bootstrap does this for its pinned package.
No controller or bootstrap command runs a scored evaluation.

## First NPU start

```powershell
.\scripts\work-laptop-one-shot.ps1 -InstallMissing
```

This retains prerequisite checks, optional installations, model download, model
hashing, protocol qualification and the C++ fixture smoke. Serving now delegates
to `measurement/serve.py`. Model, device, endpoint and prompt budget come from
`MODEL_PRESETS`; the former ModelId/RestPort/MaxPromptLen script overrides are
removed. Change the preset deliberately if changing the experiment configuration.
The script never installs a driver or enables Windows features without the
existing explicit switches. Existing venvs need the updated editable install.

## Daily commands

```powershell
python measurement/serve.py start --profile ptl-npu-8b --dry-run
python measurement/serve.py pull --profile ptl-npu-8b
python measurement/serve.py start --profile ptl-npu-8b
python measurement/serve.py pull --profile ptl-gpu-30b
python measurement/serve.py start --profile ptl-gpu-30b
python measurement/serve.py status --all
python measurement/serve.py logs --profile ptl-npu-8b --tail 60
python measurement/serve.py stop --profile ptl-npu-8b
```

Pass `--executable C:\path\to\ovms.exe` if the server is not on PATH. Keep the
same `--runtime-root` on all commands if overriding its default:
`%LOCALAPPDATA%\LocalCodeAgent` on Windows, `~/LocalCodeAgent` elsewhere.
`--model-dir` selects a local IR directory; `--gguf` selects a downloaded GGUF
for `nuc-llama-8b` or `nuc-llama-30b`. Those historical NUC presets share 8080,
so they are mutually exclusive. The five laptop profiles have distinct ports.

Dry-run prints a JSON argv array, environment overrides and file locations. It
creates no state and probes no hardware. Paths are resolved for the current host
and currently available model layout. Run it again after a pull to see a resolved
version subdirectory, if the downloaded layout has one. Only `start --dry-run`
is a launch preview. Start uses `--model_path --task text_generation`, the
[OVMS 2026.3 local in-memory graph path](https://github.com/openvinotoolkit/model_server/blob/v2026.3/src/cli_parser.cpp),
so it neither downloads a different artifact after validation nor edits a shared
`graph.pbtxt`. CPU and GPU can share weights; compilation caches are per profile.
Pull downloads/configures only and never starts inference. `--model-dir` is for
start; pull always uses the preset's HF model and runtime-root model repository.

## Refusals and status

- NPU requires `INT4_SYM`, ratio 1, group -1 or 128 from
  `openvino_model.xml/rt_info/nncf/weight_compression`. The official 8B and 30B
  `openvino_config.json` files omit the precision mode, so trusting only that
  JSON would reject the good artifact. Missing, malformed or incompatible IR
  metadata refuses before any inference process is started. Metadata checking
  is compatibility screening, not a tensor-by-tensor attestation.
- OpenVINO must enumerate the requested device. GPU may resolve to GPU.0;
  GPU.1 must be present explicitly. A missing NPU driver version is recorded as
  unobserved with a warning; it is never silently repaired.
- Start requires a complete local payload, free port and the preset's minimum
  free cache space (10 GiB). Pull checks the model volume first: 15 GiB for the
  8K NPU model, 20 GiB for the 16K probe and 40 GiB for the other default presets.
  These are conservative thresholds, not a guarantee of final cache size.
- A healthy owned process is reused only with matching launch configuration.
  A live unhealthy process is retained, and the controller refuses a restart.
  A foreign port or reused PID is never adopted or killed. Stop sends SIGTERM
  on Linux or CTRL_BREAK on Windows, waits, then kills the owned process if
  necessary. Windows console attachment can prevent CTRL_BREAK; `forced` reports
  whether fallback was needed.
- Process ownership uses PID, creation time and executable. Per-profile OS locks
  prevent simultaneous controller mutations. Process records and launch specs
  live under `runtime/<profile>/`, alongside the stdout/stderr log pair. Logs
  append across restarts; these are operational records, not frozen run evidence.

Status reports process liveness, readiness HTTP status, `/v1/models`, elapsed
process uptime and both log paths. Model presence must match the expected ID;
HTTP 404 is never healthy. The device and prompt envelope are resolved from the
recorded launch, not independently read from the server. Both
`server_observed_device` and `server_observed_max_prompt_length` remain null.
For CPU/GPU OVMS, the envelope is checked against `max_position_embeddings`;
`--max_prompt_len` is NPU-only and is not falsely used as a CPU/GPU limit.
Protocol qualification is still required after a real start.

## Profiles and limits

| Profile | Device | Port | Agent budget / server envelope | Status |
|---|---|---:|---:|---|
| ptl-npu-8b | NPU | 18000 | 7500 / 8192 | Primary bring-up path; hardware verification pending |
| ptl-npu-8b-16k | NPU | 18010 | 15000 / 16384 | Unsupported on documented 2026.3 stack; explicit probe only |
| ptl-gpu-30b | GPU | 18001 | 12000 / 16384 | INT4_ASYM; hardware verification pending |
| ptl-cpu-30b | CPU | 18002 | 12000 / 16384 | Same 30B artifact; hardware verification pending |
| ceiling-27b-dense | GPU | 18003 | 12000 / 16384 | Experimental model/stack, excluded from normal bring-up |

The [2026.3 and 2026.3.1 release notes](https://github.com/openvinotoolkit/model_server/releases)
still list the NPU 8K cap. Both experimental profiles refuse start/pull unless
`--allow-experimental` is supplied. This opt-in is a probe permission, not a
support claim. The ceiling model also needs its documented matching runtime and
model revision; do not run it against the bootstrap's ordinary stack by default.
The controller does not fetch a nightly stack.

The [8B model card](https://huggingface.co/OpenVINO/Qwen3-8B-int4-cw-ov)
documents the selected NPU artifact. The
[30B artifact](https://huggingface.co/OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov)
is asymmetric INT4 and this controller refuses it on NPU. That is a statement
about this artifact and route, not a proof that every possible 30B conversion
can never run on an NPU. The documented
[INT8 NPU crash](https://github.com/openvinotoolkit/openvino/issues/35641)
is why compatibility is checked before construction/generation.

Native llama.cpp CPU serving remains independent of OpenVINO. The argv builder
also encodes OpenVINO-backend device environment, explicit context, one slot,
stateless mode and Q4_0-only NPU declarations. No accelerator llama preset is
advertised yet: building and hardware-validating that optional route is deferred.
See the [upstream backend limits](https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/OPENVINO.md).
An environment variable alone does not prove an executable has that backend.

## Energy around an unscored command

Use [HWiNFO64 sensor CSV logging](https://www.hwinfo.com/about-software/), started
manually before the workload and left running until the wrapper finishes. Choose
the exact package-power column in watts, log at 1 second intervals, and retain
at least one sample before and after the command. Availability/approval of
HWiNFO on the work laptop remains a hardware/user check. Nothing installs it.

```powershell
python measurement/energy.py --out results/cpu-energy-001.json `
  --hwinfo-csv C:\logs\sensors.csv --sensor "CPU Package Power [W]" `
  --timestamp-format "%d.%m.%Y %H:%M:%S.%f" `
  -- python -c "sum(i*i for i in range(100000000))"

python measurement/energy.py --out results/no-sampler-001.json `
  -- python -c "print('unscored smoke')"
```

Use the timestamp format, delimiter and encoding of the actual export. Numeric
power must use a decimal point. Duplicate sensor names, absent columns, malformed
values, gaps over 5 seconds or missing boundary samples produce null joules and
`measurement_quality: unobserved`. They do not fail the workload. The wrapper
waits up to 5 seconds for the final CSV flush, then integrates trapezoids clipped
to the workload interval. `sample_hz` is calculated from contributing intervals.
The measured quantity is energy for the selected sensor during that interval,
without idle subtraction. Package power is not wall power or an NPU-only rail.
No energy-saving ratio can be claimed from these synthetic/unit checks.

`--run-manifest path` and `--launch-record path` optionally attach SHA256 links.
The new completion manifest contains host, Python, command/exit status, measurement
window, energy and source CSV hash. It is created exclusively; existing output
is never overwritten. The pre-run manifest stays unchanged. Retain the CSV after
logging stops as well as the completion record; its final hash may differ from
the sampled snapshot hash if logging continued. Experimental row attachment and
per-success denominators belong to the experiment owner, outside this PR.

## Review and laptop checklist

Run `python -m pytest tests/test_serving.py tests/test_energy.py`. Dedicated CI
runs these on native Windows and Linux, including child-process/HTTP integration
and a PowerShell 5.1 syntax check. Sandboxes with mismatched PID namespaces may
skip the three OS process tests locally; CI forbids that escape.

On the laptop: inspect the dry-run, run the one-shot, start GPU alongside NPU,
check both in status, stop one and confirm the other stays healthy. Test one
bad-NPU override with an existing 30B payload and inspect the named INT4_ASYM
refusal. Then run the two energy examples with and without actual sensor data.
Downloading weights and first compilation are additional to this short check.
No model execution or real package-energy result has been established by a mock
server test. Do not collect scored rows until the experiment owner freezes the
new source identity after this serving change lands.
