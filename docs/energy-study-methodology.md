# Energy study methodology

Status: **candidate, pre-row-one**. This document operationalises the decisions in
`docs/energy-study-decisions.md`. The decision record contains the reasoning and
rejected alternatives; this file states the executable contract.

Energy is an observational sidecar. It never changes E1, E2 or E3. Missing or
rejected telemetry removes an observation from the energy comparison and does
not turn a correct coding row into a model failure.

## Short-window sensor measurement

`measurement/energy.py` integrates an already-running HWiNFO CSV over an explicit
command window. The pilot quality floor is **2.0 Hz observed sampling**. This was
chosen before Panther Lake data are collected to reject the common 0.5 Hz
logging cadence for short inference episodes and to provide at least 30 samples
across a 15 second episode. It is a pilot floor, not a claim of metrological
sufficiency.

A run below the floor is rejected: `energy_joules` is null,
`comparison_eligible` is false, and the observed rate is retained. The maximum
gap check remains independent and also fails closed.

Every completion sidecar records power source, requested device and sensor-domain
state. Absolute paths and host names are not evidence and are not persisted.
Artifact references are relative where possible and content hashes identify the
linked bytes.

## Sensor-domain claim gate

The permitted states are:

`unverified`, `target_responsive`, `documented_contains_target`, `proxy_only`,
`documented_excludes_target`.

Only `documented_contains_target` with a non-empty documentation evidence reference may support a device-efficiency claim for the
selected sensor. A load-response experiment can establish responsiveness, not
containment. Documentation means vendor power-domain documentation, not an
operator judgement.

## Fixed workload block

`measurement/energy_study.py` owns the block-completeness trust boundary. A block
is complete only when all preregistered task-condition cells exist, attempt
indices are contiguous from zero, the declared attempt count matches the rows,
and every cell reaches one of the bounded protocol terminal states:

- the third non-replaceable decision draw, with no later attempt; or
- five attempts exhausted before three decision draws.

The block itself must declare `termination_reason="completed"`. Early kill,
missing cells, extra cells, duplicate attempts, post-third attempts and missing
attempt declarations all make `block_complete=false`.

A five-attempt exhausted cell can therefore be protocol-complete while lacking a
fixed three-draw decision set. The two energy views remain separate:

- protocol workload energy includes every attempt the deployment paid for;
- fixed decision-set energy includes the first three decision draws only and is
  null unless every task-condition cell has all three.

No standalone joules-per-success number is emitted. The summary carries total
energy, joules per attempt, E3 completion count/rate, and joules per E3 completion
when that denominator exists.

## Device-efficiency comparison gate

A selected-sensor block is eligible for a CPU/GPU/NPU efficiency comparison only
when all of the following are true:

- `block_complete` is true;
- every attempt has a sampled, comparison-eligible energy observation;
- every observation is from mains power;
- every sensor-domain state is `documented_contains_target`.

Proxy measurements are retained but cannot silently become device-efficiency
evidence.

## Thermal order

The device order rotates deterministically by repetition:

1. CPU, GPU, NPU
2. GPU, NPU, CPU
3. NPU, CPU, GPU

The physical run procedure still has to enforce the agreed cooldown or thermal
start criterion and record start/end temperature. The helper exposes the order;
it does not pretend software can establish hardware thermal equilibrium.

## Idle and projections

An all-day figure is derived, never measured directly. A projection requires at
least three **independent** active component runs and three independent idle
component runs, each with a distinct operator-declared `session_id`. Re-slicing
one long trace does not manufacture independent samples.

The projection composes measured active energy/time with measured resident-idle
power over a fixed wall-clock window. It reports a median and an empirical
5th-95th percentile bootstrap interval, never a precise point estimate. Arrival
rates are anchored to an observed pilot rate and expressed as 0.25x observed,
1x observed, or saturation.

At least one 60 minute idle characterisation per device is still required before
an all-day claim. Repeated long-idle sessions are required before the projection
has an uncertainty range. If long idle does not stabilise, no all-day projection
is published.

## Battery and contention

Battery-drain measurements are long-window, battery-powered observations and are
not blended with mains package-sensor measurements. Their delta is diagnostic,
not a qualification gate.

Contention requires inference to remain active across the compile interval and a
compile long enough to expose scheduler/power-budget effects. The report records
total host CPU utilisation, relevant process CPU utilisation where observable,
compile-process utilisation, compile wall time and package power where available.
The permitted mechanism claim remains bounded to the measured workload.

## Before the first Gen2 model row

1. merge this instrumentation with prompt/outcome hashes unchanged;
2. run the deterministic scripted workload baseline;
3. physically establish the Panther Lake sensor-domain status;
4. only then collect the first scored model row.
