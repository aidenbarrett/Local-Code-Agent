# Bring-up

> Paths, drive letters and ports in this document are the ones from one
> development machine. They are examples to adapt, not requirements.


Order matters here. Every step is a gate: if it fails, you stop and fix that,
rather than carrying an unproven assumption into the next one.

The one thing worth repeating from the plan: **the agent works today with no
model at all.** `python measurement/run_test_suite.py tests` exercises the whole state
machine, the policy engine, every tool and a real `cmake`/`ctest` build using a
scripted client. So model bring-up is a separate problem from agent bring-up,
and you can fail at one without being blocked on the other.

---

## Stage 0: agent only, no model

```powershell
git clone <this> C:\local-code-agent
cd C:\local-code-agent
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

python fixtures\generate_project.py
python measurement\run_test_suite.py tests
```

Expect 77 passed. If cmake or ctest is missing, the integration tests skip and
the rest still run.

Then rehearse the whole measurement pipeline with no model at all:

```powershell
python tests\evals\run_evaluation.py --rehearse --out rehearsal.json
python measurement\compare_evals.py rehearsal.json
```

The scores are meaningless (there is no model), but if this completes you know
the harness, the scoring and the report generator all work, so when a real run
looks wrong you will not waste an evening suspecting the wrong component.

**Gate:** the suite is green and the rehearsal completes. Nothing below is
worth starting otherwise.

---

## Stage 1: direct GenAI on CPU

This is the known-good route: the OpenVINO model card for
`OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov` demonstrates
`openvino_genai.LLMPipeline(model_path, "CPU")` directly.

```powershell
python -m pip install openvino-genai huggingface_hub openai
python -c "from openvino import Core; print(Core().available_devices)"
```

Expect `CPU` in the list.

```python
from pathlib import Path
from huggingface_hub import snapshot_download

MODEL_ID = "OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov"
snapshot_download(repo_id=MODEL_ID, local_dir=str(Path("models") / MODEL_ID.split("/")[-1]))
```

```python
import openvino_genai as ov_genai

pipe = ov_genai.LLMPipeline("models/Qwen3-Coder-30B-A3B-Instruct-int4-ov", "CPU")
print(pipe.generate("Write a move-only RAII owner for a POSIX file descriptor.",
                    max_new_tokens=400))
```

Record, from your own machine, not from anybody's blog post:

- model load time
- peak RAM
- time to first token
- generation rate
- CPU utilisation

**Gate:** it generates. If the generation rate makes an agent loop unusable,
that is a finding, and it is better to have it now than after building an
extension around it.

---

## Stage 1.5: llama-server, the first local backend

llama-server comes **before** OVMS, and it is a first-class backend rather than
a stepping stone. The agent must work perfectly on a machine with no OpenVINO
installed at all. It is also the control: if the client fails against both
llama-server and OVMS the bug is ours, and if it works here and fails there the
difference is OVMS, its parser or its runtime.

```powershell
llama-server `
  -m D:\LLM\models\gguf\Qwen3-Coder-30B-A3B-Instruct\Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL.gguf `
  --alias qwen3-coder-30b `
  --host 127.0.0.1 --port 8080 `
  -c 16384 `
  --jinja `
  -dev none `
  --cache-reuse 256
```

Verify every flag against `llama-server --help` on **your** build
(b10816-427291b5b) before running it. `-cnv` was rejected once already, and a
flag that has moved should fail loudly rather than silently changing behaviour.
Whatever you actually run is part of the configuration identity, so record it.

Four of those flags matter more than they look:

- **`--alias qwen3-coder-30b`** fixes the OpenAI `model` field to a stable id
  instead of an absolute GGUF path, which is what the `nuc-llama-30b` profile
  expects and what keeps reports readable.

- **`--jinja`** uses the model's own chat template. Without it, tool calling
  through the OpenAI-compatible endpoint will not work, and the failure looks
  like the model refusing to call tools rather than a missing flag.
- **`-c 16384`**. The default context is small. Run the context sweep against a
  server started without this and you will get failures that are llama.cpp
  configuration, not model capability.
- **`-dev none`** keeps it genuinely CPU-only, so the UHD 770 cannot quietly
  offload part of the work and contaminate a CPU baseline.

Then qualify it:

```powershell
python measurement\qualify.py --profile nuc-llama-30b `
  --json qualify-llama-30b.json `
  --dump-dir qualify-dumps
```

**Gate:** every protocol check passes, including the tool call. An
agent-capable profile that cannot call a tool is a qualification failure, not
an unsupported optional feature. `cached_tokens` absent is a SKIP.

If the tool call fails, `--dump-dir` has the exact request and response.
Reproduce it with curl outside local-agent, record the build and the chat
template, and only then decide whether it is our client or llama-server. Do not
write a Qwen tool parser.

Deployment-specific parser behaviour (`qwen3coder`, `hermes3`) belongs to the
OVMS profiles and is not tested here.

## Stage 2: OVMS on CPU

The agent talks HTTP, so this is where the architecture you actually ship
begins. Take the **Python-enabled** Windows package: the C++-only package has
limited chat-template support and cannot use tools, which makes it the wrong
package for an agent. Grab the current release from
<https://github.com/openvinotoolkit/model_server/releases> and follow its
`setupvars` step.

Do not pip-install replacement OpenVINO or OpenVINO GenAI packages into the
Python-enabled OVMS environment. The bundle ships matched dependencies.

Then, with the tool parser for the coder model:

```powershell
ovms.exe --rest_port 8000 `
  --source_model OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov `
  --model_repository_path C:\local-code-agent\ovms-models `
  --tool_parser qwen3coder `
  --target_device CPU `
  --task text_generation
```

This exact combination (that model, that tool parser, `--target_device CPU`,
through OVMS) is **an integration branch, not a documented configuration**.
The model card shows OVMS with GPU and direct GenAI with CPU. Treat it as a
test.

```powershell
local-agent --repo fixtures\cpp_project --base-url http://127.0.0.1:8000/v3 `
  --model OpenVINO/Qwen3-Coder-30B-A3B-Instruct-int4-ov doctor
```

**Gate:** `doctor` reports the model as served.

If OVMS refuses the 30B on CPU, do not spend two evenings on it. Serve
`OpenVINO/Qwen3-8B-int4-cw-ov` through OVMS instead, carry on building, and put
"30B through OVMS on CPU" on a separate list. Model-serving compatibility and
agent development are different problems and there is no reason to couple them.

---

## Stage 3: first real agent run

```powershell
cd fixtures\cpp_project
python scripts\apply_scenario.py compile_error
cd ..\..

local-agent --repo fixtures\cpp_project --profile nuc-cpu-30b `
  run "the build is broken, find the first compiler error and explain it" `
  --transcript run1.json
```

Read `run1.json`. This is the most informative thing you will do all week. What
you are looking for:

- Did it call `build_target` once, or five times?
- Did it read the source around line 13, or guess?
- Did it try tools that do not exist?
- Did it claim the fix worked without rebuilding?

Then characterise the hardware before you characterise the model:

```powershell
python measurement\bench_model.py --profile nuc-cpu-30b --repeats 5 `
  --out bench-nuc-ddr4.json --markdown bench-nuc-ddr4.md
```

This gives you the three numbers that actually matter: time to first token
against prompt length, sustained decode rate, and what a prompt cache hit is
worth. On DDR4-3200 the cache line is the one to watch. If the warm TTFT is not
several times faster than the cold one, prefix caching is not working, and
fixing that is worth more than any other optimisation available to you.

Then turn the task into a number:

```powershell
python tests\evals\run_evaluation.py --profile nuc-cpu-30b --repeat 3 `
  --label "Qwen3-Coder 30B INT4, CPU, DDR4-3200" --out evals-nuc.json
```

**Gate:** a score and a TTFT you are willing to write down. Those are the
baseline every later change is measured against. See
`docs/measurement-protocol.md` before you quote any of it to anybody.

---

## Stage 3.5: disk budget on the work laptop

512 GB with Windows and a work toolchain on it is the binding constraint, and
model files multiply quietly:

| Item | Size |
|---|---|
| Qwen3-8B int4-cw | about 5 GB |
| Qwen3-Coder 30B-A3B int4 | about 16.3 GB |
| Hugging Face cache copy, if you do not redirect it | the same again |
| OVMS model repository copy | the same again |
| Compiled NPU and GPU blob caches | a few GB, and they grow |

Careless, one 30B lands at close to 40 GB. Before downloading anything:

```powershell
setx HF_HOME D:\hf-cache          # or wherever you have room
$env:OV_CACHE_DIR = "C:\ov-cache"  # so you can find and clear the blob cache
```

Point `--model_repository_path` at the directory the model already lives in
rather than letting OVMS make a second copy, and check free space between
stages. Running out of disk halfway through an NPU blob compile produces a
failure that looks nothing like a disk problem.

## Stage 4: the accelerators

Same package, one flag per configuration. On a Core Ultra X7 358H there are
three engines, not one: 16 CPU cores, an NPU at 50 TOPS, and an Arc B390 iGPU
at 122 TOPS. Run all three. The iGPU is the bigger engine and has no
static-shape prompt limit, so if you only try the NPU you will conclude the
laptop is slower than it is.

OVMS is the primary NPU route. Use the preset controller so the command and
agent budget have one owner:

```powershell
python measurement/serve.py start --profile ptl-npu-8b --dry-run
python measurement/serve.py pull --profile ptl-npu-8b
python measurement/serve.py start --profile ptl-npu-8b
local-agent --profile ptl-npu-8b doctor
```

The daily preset uses **8192** maximum prompt tokens and a **7500** token agent
budget. The former 16384/15500 guidance was stale. The separate 16K preset is
unproven and requires an explicit experimental opt-in; OVMS 2026.3 and 2026.3.1
still document an 8K NPU limit. See [serving.md](serving.md) for preflight,
concurrent profiles, stop/status/logs, and energy capture.

Then the same model family on the iGPU, which is the path Intel documents for
the coder model through OVMS:

```powershell
python measurement/serve.py pull --profile ptl-gpu-30b
python measurement/serve.py start --profile ptl-gpu-30b
python measurement/serve.py start --profile ptl-cpu-30b

python measurement\run_suite.py --profile ptl-gpu-30b `
  --label "Qwen3-Coder 30B INT4, Arc B390" --memory-note "LPDDR5X, 64 GB" `
  --outdir results\ptl-gpu

python measurement\run_suite.py --profile ptl-cpu-30b `
  --label "Qwen3-Coder 30B INT4, CPU" --memory-note "LPDDR5X, 64 GB" `
  --outdir results\ptl-cpu

python measurement\compare_evals.py results\*\evals.json --markdown comparison.md
```

Measure latency and package energy on each device; neither winner nor ratio is
established yet. Use the HWiNFO CSV wrapper in [serving.md](serving.md). Compare
joules per verified completed task only after collecting properly linked energy
and outcome evidence. Absent sensors are unobserved, never zero.

## Stage 4.5: the two-tier run, which is the actual result

With both servers up, run the deployment you would actually ship: cheap skills
on the NPU, hard ones on the iGPU, escalation when the small model fails.

```powershell
python measurement\run_suite.py --profile ptl-gpu-30b --cheap-profile ptl-npu-8b `
  --label "Two-tier: 8B on NPU, 30B on B390" --memory-note "LPDDR5X, 64 GB" `
  --outdir results\ptl-tiered
```

The report gives you the ledger:

```
Tasks                   9
Blocked (environment)   0
Attempted               9

Cheap tier alone        x/9
Escalated success       y/z
Local success total     n/9
Cloud would be needed   m%

Model calls             cheap a, strong b
Median task             cheap-only  s, escalated  s
```

Those three middle lines, next to the energy figure, are the entire POC.
Everything else in the results directory is supporting evidence for them.

One caution before you quote the cheap-tier number: check the blocked list
first. A run where the toolchain was not reachable will show a flattering
attempted-task rate over a tiny denominator, and somebody at work will spot
that before you do.

Set `context_budget_tokens` on the `ptl-npu-8b` preset to a few hundred below
whatever `MAX_PROMPT_LEN` the blob was actually compiled with. Overrunning it
on the NPU is not an error, it is garbage output, and it will look exactly like
a model quality problem when it is a configuration problem.

Read `comparison.md` case by case. The interesting
question is not the overall score, it is **which checks the 8B misses**. If it
loses points on "verified before claiming success" that is an orchestrator
problem you can fix in code. If it loses them on "named the right file" that is
a model capacity problem and no amount of prompt work fixes it.

Do not attempt 30B on NPU until everything above works. That is the classic
mistake: three weeks optimising something that does not exist yet.

---

## Stage 5: the editor

`local_agent/rpc/stdio.py` is already the seam. A VS Code extension spawns
`python -m local_agent.rpc.stdio`, writes one JSON object per line, and renders
the `tool`, `observe` and `approval_required` events. The extension holds no
logic, which is the point: a bug can never be "only in the extension".

Over Remote SSH the extension runs on the remote host, so the tunnel only has
to carry the RPC, not the model traffic.

---

## Keeping work and personal separate

Everything in this repository is generic. No internal names, no internal
commands, no copied logs, no internal hosts, no work skills. The proprietary
layer is exactly two things, and both live inside the work repository rather
than here:

1. its `.local-agent.toml`, holding the real build and test invocations
2. its `skills/`, holding skills that override the bundled ones by name

That separation is not tidiness. It is what keeps this project something you
can talk about.
