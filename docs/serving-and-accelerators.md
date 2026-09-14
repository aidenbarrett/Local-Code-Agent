# Serving and accelerators

Which model runs on which device, and why. Verified against Intel's own
documentation and model cards rather than inferred from names. Read this before
changing a preset or pointing a model at a new device.

`docs/bring-up.md` is the procedure. This is the reference the procedure has to
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
2026.2 release notes state that NPU execution on LLMs has a limit on the max
prompt parameter of 8k tokens. Until a server actually starts with a 16K blob on
this hardware, treat the 16K preset as a hypothesis.

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
surprising.

## The NPU's case is energy, and it currently cannot be made

The NPU will not beat a 122 TOPS iGPU on latency and does not need to. The
argument that lands is joules per completed task with the CPU left free for the
build. That claim requires a power measurement, and **no sampler is nominated
anywhere in this repository**. Intel Power Gadget is deprecated.

Candidates, in the order worth trying: HWiNFO64 in shared-memory or CSV logging
mode, most likely to be permitted on a managed machine; Intel SoC Watch, the
documented Intel route but heavier; on Linux, RAPL under `/sys/class/powercap`
plus NPU telemetry under `/sys/class/intel_pmt/telem*` and
`/sys/bus/pci/drivers/intel_vpu/`.

Whatever is chosen, record `energy_joules`, `sampler`, `sample_hz` and
`measurement_quality` in the run manifest, and follow the manifest's existing
convention: no sampler available records `"unobserved"`, never a zero.

## What does not exist yet

There is no device-selectable launcher. `scripts/work-laptop-one-shot.ps1` is
NPU-only, one model, one port, hardcoded, and nothing starts the GPU or CPU
servers at all. Until that exists, every non-NPU configuration is a manual
command.

Whatever builds it must derive every argument from `MODEL_PRESETS` and never
from a second table. The three-way prompt-length drift described above happened
precisely because the doc, the config and the script each kept their own copy.

## Sources

- [Qwen3-8B-int4-cw-ov model card](https://huggingface.co/OpenVINO/Qwen3-8B-int4-cw-ov)
- [Qwen3-Coder-30B-A3B-Instruct-int4-ov model card](https://huggingface.co/OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov)
- [OpenVINO issue #35641: NPU silently accepts unsupported precision](https://github.com/openvinotoolkit/openvino/issues/35641)
- [Intel Community: official model fails on NPU, works on GPU](https://community.intel.com/t5/Intel-Distribution-of-OpenVINO/Running-official-OpenVINO-model-with-target-device-NPU-leads-to/m-p/1752704)
- [llama.cpp OpenVINO backend](https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/OPENVINO.md)
- [OpenVINO Model Server releases](https://github.com/openvinotoolkit/model_server/releases)
- [OVMS text generation with NPU acceleration](https://docs.openvino.ai/2025/model-server/ovms_demos_llm_npu.html)
- [Intel NPU Driver for Windows](https://www.intel.com/content/www/us/en/download/794734/intel-npu-driver-windows.html)
