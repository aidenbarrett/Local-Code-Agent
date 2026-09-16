# Generation-2 scripted baseline contract

Status: **binding before the first Generation-2 model row**.

This is the deterministic comparator required by `docs/next-experiment-preregistration.md`. It is deliberately narrower than the model experiment. Its job is to answer a cheap engineering question first: which of the existing pilot tasks are sufficiently procedural that a deterministic script can obtain the required evidence without inference at all?

## Frozen scope

The scripted baseline runs the eight non-repair pilot cases:

- `clean-build`
- `compile-error-locate`
- `link-error`
- `test-failure-diagnose`
- `segfault`
- `timeout`
- `navigation`
- `review-restraint`

`compile-error-fix` and `test-failure-fix` are excluded from this baseline. A fixture-specific replacement patch would encode the answer rather than measure a reusable procedure. Those two repair tasks remain model tasks until there is a genuinely generic deterministic repair algorithm worth testing.

The baseline may choose a deterministic procedure from the declared case name because the case name is part of the workload contract. It must not read the clean scenario, task rubric, expected-answer strings, oracle result, or any hidden fixture answer while producing its report. Scoring happens only after the report is frozen.

## Procedure

`scripts/run-gen2-scripted-baseline.py` works on a disposable copy of `benchmark_fixture/cpp_project`.

For each case it restores/applies the public scenario, executes the fixed procedure for that task class, captures exact command/return-code/stdout/stderr evidence, and emits a templated report. The script contains no model client and makes no inference request.

The report is then scored separately against the same observable facts used by the pilot task contracts. The scorer cannot change the frozen report.

A baseline result is valid only when:

- the source checkout is clean before the run;
- the fixture copy is disposable and isolated from the repository checkout;
- every invoked command and return code is archived;
- the baseline runner itself does not consume expected-answer data while generating the report;
- the output records repository commit and instrument identities for context, even though the baseline is not a model row.

## Permitted claims

The pilot is bounded to these ten fixture tasks. The strongest permitted capability wording is therefore of the form:

> On the preregistered set of ten repetitive C++ maintenance tasks in this benchmark fixture, configuration X completed N of 10 task majorities under condition Y, with the recorded latency, attempts and energy.

For the deterministic comparator, use the same shape but name the scripted subset explicitly, for example:

> On the eight preregistered non-repair fixture tasks, the deterministic baseline obtained the required result on N of 8 without model inference.

Do **not** convert either result into a percentage of real engineering work, Copilot usage, Claude usage, developer time, or production repositories. These fixture tasks do not sample those populations. A statement such as "the local agent can replace X% of premium-model work" is outside the evidence and is prohibited by this preregistration.

The experiment may motivate a later real-repository study. It does not become one by changing the slide wording.

## Decision use

Where the deterministic baseline reaches the same required result with equal or better reliability and materially lower inference cost, the script wins that task class for this pilot. That does not imply scripts are generally better; it means an LLM is not justified for that bounded procedure.

The two excluded repair tasks are not counted as scripted failures. They are outside the scripted baseline denominator by construction, and all summaries must show `N/8`, not `N/10`, for this comparator.
