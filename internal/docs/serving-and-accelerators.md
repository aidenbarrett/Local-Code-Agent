# Serving and accelerators

Which model runs on which device, and why. Verified against Intel's own
documentation and model cards rather than inferred from names. Read this before
changing a preset or pointing a model at a new device.

`internal/docs/bring-up.md` is the procedure. This is the reference the procedure has to
obey.

## Device and model compatibility

| Model | Quantisation | CPU | GPU | NPU |
|---|---|:--:|:--:|:--:|
| `OpenVINO/Qwen3-8B-int4-cw-ov` | **INT4_SYM**, channel-wise, ratio 1.0 | yes | yes | **yes** |
| `OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov` | **INT4_ASYM**, group size 128 | yes | yes | **no** |

**The NPU requires symmetric INT4.** Channel-wise (`--group-size -1`) or group
size 128, exported with `--sym`. The 8B model card states `INT4_SYM`, says it is
optimised for inference on NPU, and carries an NPU example. The 30B card states
`INT4_ASYM`, documents CPU and GPU only, and never mentions NPU.

This is not a matter of trying harder. Intel support told a user that
`Qwen2.5-Coder-3B-Instruct-int4-ov`, from the same family of published
asymmetric conversions, "is supported on CPU and GPU only and does not currently
support execution on Intel NPU", after it failed NPU compilation with
`StopLocationVerifierPass Pass failed`. The 30B belongs on the Arc B390.

## The failure mode that matters

**Wrong precision on NPU does not raise an error. It crashes.**

OpenVINO issue #35641: an INT8 weight-only IR is silently accepted by
`LLMPipeline(ir, "NPU")` at construction, then dies at the first `generate()`
with an uncatchable `0xC0000005` access violation. No Python exception, no plugin
diagnostic, no traceback. Reproduced on 2026.0.0 and 2026.1.0 on Windows. The
same IR runs fine on GPU.

So the model's OpenVINO config must be checked against the target device
**before** the server is launched, read from the pulled model directory rather
than inferred from the model name. A preflight refusal naming the actual
quantisation mode found is worth more than any amount of log reading afterwards.

## Prompt length on NPU is a hard, silent limit

The NPU pipeline is static shape. Prompt length is fixed when the blob is
compiled, and **overrunning it produces garbage output rather than an error**.
So the compiled `--max_prompt_len` and the preset's `context_budget_tokens` must
move together, always, and the budget must sit strictly under the compiled
length.

Current pinned configuration: blob compiled at **8192**, `ptl-npu-8b` budget
**7500**. `ptl-npu-8b-16k` assumes a blob at 16384 and is **unproven**: OVMS
2026.3 and 2026.3.1 release notes still document an 8K NPU limit. Until a server
actually starts with a 16K blob on this hardware, treat the 16K preset as a
hypothesis.

A test asserts that no preset's context budget meets or exceeds its declared max
prompt length. Do not remove it; this exact pair drifted across three files
before it existed.

## Two runtimes, three devices

**OVMS** is the primary path and the one Intel documents for this model family.
Device is selected with `--target_device NPU|GPU|CPU`. NPU serving additionally
wants `--max_prompt_len` and
`--plugin_config "{\"NPUW_LLM_PREFILL_ATTENTION_HINT\":\"PYRAMID\"}"`.

**llama.cpp** is a first-class backend, not a fallback, and since the OpenVINO
backend landed it can also reach the accelerators. Build with
`-DGGML_OPENVINO=ON`, select with `GGML_OPENVINO_DEVICE=CPU|GPU|NPU` (defaults
to CPU; `GPU.0`/`GPU.1` for multiple GPUs). Constraints on this backend, which
belong in the preset rather than being discovered at runtime:

- NPU is Q4_0 and **stateless only**, no KV cache reuse
- `llama-server -np > 1` is unsupported
- context defaults to the model's training context and will OOM or fail without
  an explicit `-c`
- prefill chunk size is fixed at 256, tunable via
  `GGML_OPENVINO_PREFILL_CHUNK_SIZE`
- `GGML_OPENVINO_STATEFUL_EXECUTION=1` helps CPU and GPU, is experimental, and
  limits you to one chat session
- text-only, and a subset of GGML ops

It is the newer and rougher path. Its value is as an independent cross-check of
OVMS numbers on the same silicon, which is worth a great deal when a result looks
surprising. The optional accelerator-enabled llama.cpp build remains deferred;
PR #8 landed the controller and tested native llama-server launch constraints,
not a measured accelerator result.

## Energy measurement exists; the hardware result does not

The repository now has an HWiNFO CSV capture path in `internal/measurement/energy.py` and
the serving controller can wrap an explicitly supplied sampler command. It
records a linked completion manifest and treats absent sensors as `unobserved`,
never zero. Synthetic capture has exercised the plumbing, but no physical NPU or
GPU energy result has been measured yet.

The research claim remains joules per verified completed task, not an assumed
latency or efficiency win. HWiNFO64 is the primary managed-Windows route; Intel
SoC Watch is the heavier documented alternative; on Linux, RAPL under
`/sys/class/powercap` plus NPU telemetry under `/sys/class/intel_pmt/telem*` and
`/sys/bus/pci/drivers/intel_vpu/` are candidates.

Whatever is used, record `energy_joules`, `sampler`, `sample_hz` and
`measurement_quality`, with the measurement linked to the exact run evidence.

## Serving controller status

`internal/measurement/serve.py` is the device-selectable launcher for OVMS profiles.
It derives launch arguments from `MODEL_PRESETS`, owns per-profile ports/cache
and process state, and supports start/status/stop/logs/pull plus exact dry-runs.
`internal/work-laptop-one-shot.ps1` delegates to that controller rather than
keeping a second NPU-only command table.

The Panther Lake **NPU Qwen3-8B path has been physically exercised on Windows**,
including real local inference. The remaining hardware work is to finish the same
physical rehearsal for the other intended accelerator paths and to capture
HWiNFO power data. Those are hardware-evidence gaps, not missing launcher
architecture.

## Sources

- [Qwen3-8B-int4-cw-ov model card](https://huggingface.co/OpenVINO/Qwen3-8B-int4-cw-ov)
- [Qwen3-Coder-30B-A3B-Instruct-int4-ov model card](https://huggingface.co/OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov)
- [OpenVINO issue #35641: NPU silently accepts unsupported precision](https://github.com/openvinotoolkit/openvino/issues/35641)
- [Intel Community: official model fails on NPU, works on GPU](https://community.intel.com/t5/Intel-Distribution-of-OpenVINO/Running-official-OpenVINO-model-with-target-device-NPU-leads-to/m-p/1752704)
- [llama.cpp OpenVINO backend](https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/OPENVINO.md)
- [OpenVINO Model Server releases](https://github.com/openvinotoolkit/model_server/releases)
- [OVMS text generation with NPU acceleration](https://docs.openvino.ai/2025/model-server/ovms_demos_llm_npu.html)
- [Intel NPU Driver for Windows](https://www.intel.com/content/www/us/en/download/794734/intel-npu-driver-windows.html)
