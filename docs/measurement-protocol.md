# Measurement protocol

Numbers that get shown to other people have to survive somebody asking "how did
you measure that". This is how, written down before the first run so it cannot
be adjusted afterwards to suit the result.

## What is being measured, and why those three things

An agent turn is a **long prompt and a short completion**: roughly 2000 to 6000
tokens in, 40 to 150 tokens out. So tokens per second, the number everybody
quotes, is close to irrelevant. Three quantities decide whether this is usable:

| Quantity | Why it matters | Where it comes from |
|---|---|---|
| Time to first token, per turn | Dominates wall clock. This is prefill. | streaming, measured client side |
| Sustained decode rate | Sets the cost of the short completion | streaming, `(tokens - 1) / (total - ttft)` |
| Prompt cache hit ratio | Decides whether prefill is paid once or every turn | server `usage.prompt_tokens_details.cached_tokens` |

Everything else in the reports is derived from those plus the eval scores.

## Rules

**Discard the first call.** It pays model load, graph compilation and blob
cache misses. `bench_model.py` does this automatically and says so in its
output. If you quote a cold first call as if it were steady state you will be
correctly torn apart for it.

**Report median and p90, never mean.** One thermal throttle event or one page
fault ruins a mean. Minimum three repeats per point, five if you are going to
show it to anybody.

**Defeat the cache when measuring prefill, exercise it when measuring caching.**
The prefill sweep generates a fresh random prompt every sample so nothing can
be reused. The cache test deliberately sends the same prompt twice. Mixing
those two is the single easiest way to publish a wrong number.

**Pin the machine.** Same power profile, same background load, plugged in, lid
open, and say which in the report. On the 12th gen i9 also record whether you
pinned to P-cores, because the E-cores drag a tensor-parallel step down to the
slowest thread and it changes the answer materially:

```
ov::hint::scheduling_core_type = PCORE_ONLY
INFERENCE_NUM_THREADS = <number of P-cores>
```

**Record the memory configuration.** DDR4-3200 dual channel is roughly 51 GB/s
theoretical against roughly 77 GB/s for DDR5-4800, and decode on a
bandwidth-bound model scales almost directly with it. A comparison between two
machines that does not state their memory is not a comparison.

**State the context budget and the prompt-length limit.** On the NPU, prompt
length is fixed when the blob is compiled. A run at `MAX_PROMPT_LEN=2048` and a
run at 8192 are different experiments, and overrunning the limit produces
garbage output rather than an error, so a silent overrun looks like a model
quality problem when it is a configuration problem.

## Configuration identity

Every number is quoted next to the configuration that produced it, and the
reports carry all of it:

```
model  device  runtime  quant  tool_parser  thinking
context_budget_tokens  max_tool_result_tokens  temperature
```

`thinking` is in that list for a reason. Qwen3 will happily reason its way
through a one-line instruction, and comparing an 8B with thinking on against a
30B with thinking off, then attributing the difference to the model or the
device, would be a rotten mistake. `qualify.py` measures whether it fires and
how many characters it burns, and the client strips reasoning out of the answer
so it can never be graded as one.

## Before any of this: qualify the server

```bash
python measurement/qualify_server.py --profile ptl-npu-8b --json qualify-npu.json
```

Reachability, streaming TTFT, usage fields, whether `cached_tokens` exists at
all, a tool call round trip, a structured finish, prefix cache without tools,
prefix cache **with** tools, our own schema serialisation stability, and a
context validity sweep that checks a known answer at each size so a garbled
overrun is caught rather than trusted to raise.

If any of that plumbing is broken there is no value in generating benchmark
numbers, because you will be measuring the plumbing.

## The short version

One command per machine, then one command to compare:

```bash
python measurement/run_benchmark_suite.py --profile nuc-cpu-30b \
    --label "Qwen3-Coder 30B INT4, CPU" --memory-note "DDR4-3200 dual channel, 64 GB" \
    --outdir results/nuc-ddr4
python measurement/compare_datasets.py results/*/evals.json --markdown comparison.md
```

`run_suite.py` does everything below in order and writes one directory you can
attach to a mail. The rest of this document is what it does and why, so that
you can defend it when somebody asks.

## Energy

Where the counters are readable, the suite reports package energy for the whole
eval pass and joules per completed task. That second number is the one worth
arguing with, because it is invariant to how long the task took: "same answer
for a fifth of the energy" survives the objection that the NPU was slower.

On Linux the counters are Intel RAPL under `/sys/class/powercap`, and they
usually need root. On Windows they are not exposed to a plain process, so run
Intel Power Gadget or SoC Watch alongside the pass and paste the figure into
the report by hand, saying which tool you used. A measured figure with its
source named beats a modelled one every time.

## The experiment

Two machines, five configurations, and only one variable moves between any
adjacent pair. That is what makes it a comparison rather than a collection of
numbers.

| # | Machine | Model | Device | Isolates |
|---|---|---|---|---|
| A | NUC12, DDR4-3200 | Qwen3-Coder 30B-A3B int4 | CPU | baseline |
| B | Panther Lake, LPDDR5X | Qwen3-Coder 30B-A3B int4 | CPU | **memory bandwidth** (A vs B) |
| C | Panther Lake, LPDDR5X | Qwen3-Coder 30B-A3B int4 | GPU, Arc B390 | **device** (B vs C) |
| D | Panther Lake, LPDDR5X | Qwen3-8B int4-cw | NPU | **model and device** (C vs D) |
| E | Panther Lake, LPDDR5X | Qwen3.8-27B dense int4 | GPU | **quality ceiling**, run once |

A against B is the clean bandwidth result: identical model, identical device
class, only the memory changes. C against D is the one people will argue about,
and it is two variables at once, so state that plainly rather than pretending
otherwise. E exists to calibrate: if the biggest model available also fails a
case, that case is a harness problem and nobody should spend a week blaming a
small model for it.

The NPU is not the fastest engine in that laptop. The Arc B390 is 122 TOPS
against the NPU's 50, and it has no static-shape prompt limit. So the NPU's
case has to be made on energy, not latency, which is exactly why joules per
completed task is in the report.

## The runs

### 1. Prove the harness before blaming the model

```bash
python evaluation/run_evaluation.py --rehearse --out rehearsal.json
```

No model server, no network. If this does not complete cleanly, the problem is
the harness or the sandbox, not the model, and you have just saved yourself an
evening of chasing the wrong thing.

### 2. Hardware characterisation

```bash
python measurement/benchmark_model.py --profile nuc-cpu-30b \
    --repeats 5 --out bench-nuc-ddr4.json --markdown bench-nuc-ddr4.md
```

Produces the prefill sweep, the decode rate, the prefix cache speedup, and the
cost of a mid-prompt rewrite. That last number is what one context compaction
costs, which is why the orchestrator counts them.

### 3. Behaviour

```bash
python evaluation/run_evaluation.py --profile nuc-cpu-30b --repeat 3 \
    --label "Qwen3-Coder 30B INT4, CPU, DDR4-3200" --out evals-nuc.json
```

Three repeats because tool-calling models are not deterministic even at low
temperature, and a single run tells you nothing about variance.

### 4. The artefact

```bash
python measurement/compare_datasets.py evals-nuc.json evals-npu.json \
    --markdown comparison.md --json comparison.json
```

## The two headline numbers

They are different numbers and conflating them would be the easiest way to
oversell this:

```
NPU alone       what the cheap tier completed on its own
Local overall   what the local stack completed at either tier
Cloud required  the remainder
```

Both counted **per task, not per model call**. An agent that takes six internal
turns to answer one git query has completed one task, and reporting it as six
cheap-tier wins would flatter the small model for being chatty.

A task counts as completed when the loop finished cleanly and the answer scored
at or above `SUCCESS_THRESHOLD` (0.75, fixed in `task_contracts.py` before any data
existed). A run that finishes tidily with the wrong answer is not a success.

### BLOCKED comes out of the denominator

If cmake is missing, or policy forbids running tests, or an approval was
declined, the model did not fail at engineering. Those runs are counted as
`blocked`, they do not escalate, they are excluded from the rates, and they are
listed by name. Otherwise one broken toolchain quietly reads as a model that
cannot do its job.

A build that fails to compile is **not** blocked. It is a result, and for a
diagnosis skill it is the evidence.

## How the verdict is decided

Set before any data was collected, in `evaluation/task_contracts.py`:

- **Diagnostic subset**: `compile-error-locate`, `compile-error-fix`,
  `test-failure-diagnose`, `segfault`. These four require the model to read
  evidence and reach a specific, checkable conclusion. A configuration can
  score well overall by doing almost nothing on the easy cases, so the
  diagnostic subset is reported separately.
- **Kill threshold: 0.6 on the diagnostic subset.** Below it, the configuration
  is not a daily driver and prompt engineering will not rescue it. Say so and
  move on.
- **Interactive threshold: 10 s median time to first token.** Above it the
  thing is a batch tool, which is a different product with a different pitch,
  not a worse version of the same one.

Those two thresholds split the outcome four ways, and only one of the four is a
disappointment:

| | fast | slow |
|---|---|---|
| **accurate** | sits in the loop with you | overnight and background work |
| **inaccurate** | useless, but cheap to have learned | bin it |

## What to put in front of people at work

1. `comparison.md`: score per case, per configuration, with the verdict.
2. `bench-*.md`: the hardware characterisation, one per machine.
3. One transcript from a case that worked, with the tool calls visible.
4. One transcript from a case that failed, and your reading of why.

Bring the failure. A comparison with no failures in it reads as marketing and
gets treated as marketing.
