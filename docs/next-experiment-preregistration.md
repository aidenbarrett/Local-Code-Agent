# Pre-registration: next model-configuration and deployment experiments

Status: **binding before the next model row is collected**.

This document exists to stop us explaining the result after we have seen it.
The ten existing fixture tasks are a **pilot**. They validate the comparison,
the telemetry and the decision logic. They do not confirm a population-level
claim about procedural skills. Confirmation, if it is ever worth doing, uses
fresh held-out tasks that were not used to tune the procedures.

## 1. The question we are actually asking

The next CPU experiment is a **model-configuration x procedure** comparison.
It is not a clean small-model-versus-large-model experiment.

The two practical configurations are:

- Qwen3-Coder-30B-A3B-Instruct, a MoE model (30.5B total / 3.3B active parameters per token as project-declared architecture metadata);
- Qwen3-8B, a dense model.

Active parameter count is relevant architecture metadata. It is **not** a
measurement of runtime compute, memory traffic, latency or energy. Architecture,
routing, quantisation and implementation all matter. Therefore neither a good
nor a bad 8B result is evidence for or against a universal "smaller models need
procedures more" thesis.

What the pilot can answer is narrower and useful:

> On the same CPU deployment path and frozen agent instrument, which model
> configuration completes which bounded engineering tasks, under which tool and
> procedure condition, at what cost?

## 2. Frozen comparison before NPU work

Both models are rerun on the same frozen instrument. Fresh 8B rows are not
compared against the historical generation-1 30B rows as if nothing changed.
Those historical rows remain development evidence.

Hold constant for the CPU pilot:

- same host;
- same llama.cpp runtime family and server configuration policy;
- same agent source and generation;
- same task fixture;
- same condition semantics;
- same tool contracts;
- same scoring/outcome contract;
- same run-manifest schema;
- same transcript policy.

Run all three conditions for both model configurations:

1. `control`: full executable registry, no procedure;
2. `narrow`: skill tool boundary, no procedure text;
3. `skill`: identical tool boundary plus procedure text.

The first pass is one run per cell. More repeats are justified only by a
specific decision the first pass cannot make; repeated executions of the same
ten fixtures are not ten new tasks.

## 3. Four endpoints, never collapsed into one claim

Every analysis reports these separately:

### Engineering correctness

Did the system identify/fix the engineering problem correctly, independent of
whether it used the expected terminal claim label?

### Contract compliance

Did it follow the requested scope and output contract: correct claim type,
structured submission when required, permission boundary and no off-contract
mutation?

### Verified task completion

Did the complete operational contract pass, including deterministic evidence,
independent evaluator checks, scope, tamper and claim requirements?

### Efficiency

Wall time, model calls, tool calls, retries/escalations, measured token data
only where the backend actually reports tokens, and later energy when a defined
measurement boundary exists.

A gain caused only by changing `failure` to `diagnosis` is a contract-compliance
gain. It is not described as improved engineering capability.

## 4. Practical thresholds chosen before the run

These are **decision thresholds for the pilot**, not statistical significance
claims and not universal ML benchmarks. One task in a ten-task pilot is ten
percentage points, so false precision is prohibited.

### Cheap-tier candidate

Proceed with an 8B configuration as a serious cheap/local tier candidate when,
under the selected permission boundary:

- engineering correctness is at least **7/10** overall;
- the existing diagnostic subset is at least **60%** correct;
- there are **zero uncontained scope violations** in the selected deployment condition;
- no verifier/instrument integrity defect invalidates a row.

A result of 6/10 is development-grade, not deployment-grade. At 5/10 or below,
or below 50% on the diagnostic subset, stop tuning these ten tasks and change
the model/configuration or task class instead of prompt-engineering the pilot.

### Procedure effect

For `skill - narrow` on engineering correctness:

- **+20 percentage points or more** (at least two additional tasks in this pilot): practically meaningful pilot signal;
- **+10 points**: weak signal worth testing only on fresh tasks;
- **0 or negative**: no engineering-capability benefit demonstrated by the procedure on this pilot.

Contract compliance and efficiency may still improve and are reported as such.
They do not get renamed "capability".

### Permission-boundary effect

If narrowing materially reduces scope violations while engineering correctness
is no worse by more than one task, ship the permission boundary as an
engineering control whether or not any cognitive explanation is true.

If executor-side permission enforcement later matches hidden-tool narrowing,
prefer the simpler enforceable boundary and drop the stronger "hiding tools
improves reasoning" claim.

### Scripted baseline

A deterministic script + templated report is run alongside the first CPU pilot
for the most procedural task classes. If it reaches the same verified result
with equal or better reliability and materially less inference cost, the script
wins that workload. The fact that this project contains an LLM is not a reason
to use one where a script is better.

## 5. Run order

1. Freeze the evidence-complete instrument and its generation identity.
2. Capture an immutable run manifest before every cell.
3. Run the scripted baseline on the procedural subset.
4. Run fresh 30B CPU cells on the frozen instrument.
5. Run fresh 8B CPU cells on the same frozen instrument.
6. Analyse the four endpoints under the thresholds above.
7. Only then choose a model configuration for deployment experiments.

Condition/model order should be counterbalanced where practical. The order is
recorded. No procedure is edited in response to one model's pilot rows and then
reused as if it were held out against the other.

## 6. NPU is a separate deployment experiment

Do not change the model and the deployment stack in the same comparison and
call the difference a device effect.

The cleanest available deployment decomposition is:

1. same exported OpenVINO model on **CPU**;
2. same exported OpenVINO model on **NPU**;
3. llama.cpp CPU remains the practical alternative, explicitly labelled as a
   different runtime/configuration.

OpenVINO CPU versus OpenVINO NPU is the closer device comparison. llama.cpp CPU
versus OpenVINO NPU is a deployment comparison, not an isolated device test.

For the pilot, an NPU path is operationally attractive only if verified
completion falls by no more than one task **and** it improves median end-to-end
episode latency by at least **20%**, or produces a separately defined and
measured energy advantage. Energy is not inferred from TOPS or device class.

## 7. Evidence required before a row is interpretable

A cell without its paired manifest is not accepted as new experimental data.
The manifest must preserve observations that cannot be reconstructed later:

- host OS/version/architecture and Python runtime;
- toolchain, CMake generator and compiler identity where observed;
- requested device separately from actual device, including source/quality;
- runtime, version, model, quantisation, parser/template, context/sampling settings;
- model-architecture metadata with its evidence quality;
- exact tool schemas stored once per schema hash;
- case/condition -> schema hash mapping;
- source, prompt-contract and outcome-contract identities.

Rendered transcripts remain archived by the evaluator. We do not rebuild that
machinery merely because a review raised a general archival warning.

Hashes identify artifacts. They do not replace the artifacts.

## 8. Invalid-before-analysis conditions

Do not interpret a run if any of these is true:

- manifest instrument identity disagrees with the bytes being executed;
- the requested cell's runtime tool-schema hash does not correspond to the archived schema;
- precondition/harness validity marks the row invalid;
- a verifier/instrument defect is discovered that can change the row's outcome;
- the experimenter changed a procedure/contract after seeing one side of the comparison and did not cut a new development/held-out boundary.

Archived rows may be re-scored later when the retained evidence supports the new
rule and the new scoring version is recorded. Observations that were never
captured cannot be reconstructed. That is why evidence capture comes before
packaging polish.

## 9. What this pilot cannot prove

The ten fixture tasks cannot establish a general model-size interaction, a
population-level procedure effect, or general real-repository usefulness.

If the pilot is useful enough to justify a real experiment, build fresh tasks
with a difficulty spread, freeze the procedures before evaluation, keep a true
held-out set, and choose the sample size from the smallest effect that would
change an engineering decision.

Until then, build the tool and make every claim smaller than the evidence.
