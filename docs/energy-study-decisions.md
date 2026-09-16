# Energy study: decision record and current state

Living document. The protocol was designed in conversation between ChatGPT and
Claude, which means it exists nowhere else and will be re-litigated or quietly
drifted from unless it is written down. This is the reference to check the
implementation PR against.

**Rejected alternatives are recorded with their reasons.** That is most of the
value here: a decision without its discarded options gets reopened.

Last updated: 2026-09-16.

---

## Where we are

| | |
|---|---|
| PR #7 | merged. Evidence contract, E1-E4, tamper terminal, attempt conservation, build proof causality |
| PR #8 | merged at `500af349`. Preset-driven serving controller, `energy.py`, HWiNFO capture |
| PR #10 | merged at `51ef8bc4`. `AGENTS.md`, serving reference, open methodology questions, review history rounds 9-11 |
| PR #11 | merged at `7078fed9`. Absolute-path leakage closed. Methodology item 9 shut before row one |
| in flight | `measurement/energy-study`, candidate branched from merged PR #11 (`7078fed9`) |

**Agreed order, unchanged:** item 9 → energy methodology and instrumentation →
deterministic scripted baseline → first generation-2 model row.

Generation 2 still has zero model rows. `base_prompt_sha256` and
`outcome_contract_sha256` are frozen; only `source_sha256` may move.

**Nothing on the NPU has been measured yet.** Every throughput figure in
`docs/bring-up.md` is an expectation, not an observation.

---

## The claim, stated precisely

A local coding agent running on the NPU does the same work for fewer joules than
the same agent on the iGPU or CPU, at comparable throughput, and leaves more of
the machine available for the developer's own build.

That is four separate assertions and each needs its own evidence. The study is
an **observational sidecar**, not part of the coding-capability experiment.
Energy telemetry never affects E1, E2 or E3. A correct task with failed
telemetry stays correct and becomes unavailable for the energy comparison.

---

## Six protections

### 1. Block completeness

Total joules for a fixed workload block is only comparable if the work
attempted was comparable. The aggregate record carries attempted tasks, valid
decision draws, infrastructure-invalid attempts, E3 completions, early-stop
reason and `block_complete`. A block that hits a kill condition or fails to
execute the preregistered workload is archived but **cannot enter the
device-to-device comparison**.

Without this, a device that terminates early looks efficient because it did
less.

Two views are kept and never collapsed:

- **protocol workload energy** over the actual bounded collection, which is what
  the deployment genuinely paid, including replacement attempts after
  infrastructure-invalid draws
- **fixed decision-set energy** over the first three non-replaceable decision
  draws, which is what the device intrinsically cost, where that comparison is
  defined

Five attempts because two were infrastructure-invalid is real energy spent and
is not evidence that the device needs more inference.

### 2. Denominator transparency

Never a standalone "joules per completed task" headline. A device that fails
more tasks scores better on that metric, because failures burn energy and then
vanish from the divisor.

Always reported together: **total joules for the block**, **joules per attempted
task** with failed attempts in the numerator, and **joules per E3 completion
beside the completion rate**.

### 3. Sensor-domain qualification

*Claude proposed confirming domain coverage by driving a known load on the
target device and checking the sensor responds. ChatGPT rejected this and was
right: responsiveness proves correlation, not containment.*

Five states, and only `documented_contains_target` is eligible for a device-efficiency claim:

```
unverified  ->  target_responsive  ->  documented_contains_target
                proxy_only
                documented_excludes_target
```

"Documented" means Intel's own power-domain documentation, not an experiment. A
responsive but undocumented package sensor stays useful telemetry, described as
a proxy, never reported as "NPU joules".

**No Panther Lake energy claim before the domain is physically established.**

### 4. Power-state separation

Package-sensor runs on mains. Battery-drain runs on battery. These are different
power states with different PL1/PL2 limits, so they are two experiments rather
than two views of one. Every observation records its power source. No
averaging, normalising or stitching into a single energy number.

Battery telemetry is coarse and noisy, so battery is used for long fixed-duration
workloads and sustained arrival-rate windows. Sensor integration handles short
episode-level measurement.

**The paired battery/package run is a diagnostic, not a gate.** Record both over
the same wall-clock interval, derive `system_minus_selected_sensor_joules`,
estimate its variance across repeats, flag instability or sign pathologies and
investigate before using the data. Never require equality or a device-independent
offset: moving between CPU, GPU and NPU legitimately changes memory traffic, fan
behaviour, run duration and residency states, so that delta can vary by device
with both instruments working perfectly.

*Claude originally proposed requiring the delta to be stable across devices.
Rejected: it bakes a false physical assumption into a qualification gate and
would reject good data.*

### 5. Thermal and order control

Counterbalanced rotation rather than randomisation. With three devices and a
small run count, randomisation does not balance, it makes the imbalance
unpredictable.

```
CPU -> GPU -> NPU
GPU -> NPU -> CPU
NPU -> CPU -> GPU
```

Repeat only after a defined cooldown or thermal-start criterion. Record start and
end temperature, device order and power source per run.

### 6. Sampling quality

`measurement/energy.py` in the candidate keeps the maximum-gap check and adds a
**2.0 Hz observed minimum** chosen before Panther Lake data are collected. The
common two-second / 0.5 Hz HWiNFO cadence is therefore rejected for short
episodes. Below the floor, `energy_joules` is null and the observation is not
comparison-eligible; the observed rate and interval remain recorded so the
rejection is auditable.

---

## Idle, and the all-day claim

Idle power multiplied by hours is the largest term in any all-day number and the
least validated, because it is measured over a minute and extrapolated several
hundredfold.

**Idle state is defined precisely:** server running, model fully loaded and
resident, zero requests, fixed settling period followed by a fixed measurement
window. Record resident memory, temperature and power source.

**Raw idle is retained and never automatically subtracted from task energy.**
Baseline subtraction creates its own assumptions. Incremental energy may be
reported separately where the design justifies it.

Two distinct questions, and they need different evidence:

1. **Does idle power evolve over tens of minutes?** At least one 60-minute
   resident-idle characterisation per device before any all-day claim. It is
   cheap and it directly tests whether the short-window estimate is nonsense.
2. **How reproducible is long-window idle between sessions?** Repeated long-idle
   sessions on different runs. **One 60-minute trace chopped into 60 windows is
   pseudo-replication, not n=60.**

If short and long idle disagree materially, the long characterisation governs.
**If long idle never stabilises, no all-day projection is published.** That is
better than inventing confidence.

No magic hour: the requirement is demonstrated steady state over a materially
longer window than the short sample. Sixty minutes is the practical starting
point, not a scientific constant.

---

## CPU occupancy, and what may be claimed

"The CPU stayed free" is half the pitch and is currently evidenced only
indirectly through compile wall time. The mechanism underneath is an untested
assumption, since the OpenVINO NPU path still has a host-side component.

Measured per device, and "CPU utilisation" means all of these rather than a Task
Manager percentage:

- total system CPU utilisation during inference
- agent and server process CPU time or utilisation
- compile-process CPU utilisation during contention
- wall-clock compile time
- host CPU package power where available, because a low CPU percentage does not
  imply negligible CPU energy

**Permitted claim:** "Under this workload, NPU inference coincided with X host
CPU utilisation and Y compile slowdown."

**Not permitted:** "The NPU leaves the CPU free." The evidence does not reach
that far.

Contention is only measured if the two things actually contend: the inference
workload must stay active across the whole compile interval, and the compile must
be long enough for scheduler and power-budget effects to emerge. Not the
1.3-second fixture build.

---

## Result hierarchy

Deliberately ordered so a presentable number cannot outrank the evidence beneath
it. **Total block energy is the primary result. The all-day projection is not.**

**Measured**

1. total joules for a complete fixed workload block
2. joules per attempted task
3. joules per E3 completion, with the completion rate
4. wall time and throughput
5. resident-idle power
6. CPU utilisation and compile contention
7. long-window battery drain

**Derived**

8. fixed-window deployment energy under explicitly declared arrival scenarios,
   with uncertainty propagated

A projection is a derived analysis and never a measured result. Each projection
states which measured components it used.

### Projection rules

- No projection below a preregistered minimum number of **independent component
  runs**.
- Resample the observed active and idle measurements and compose them under the
  declared workload model. **No parametric distribution assumed**; the sample
  sizes do not support one.
- Output is a median plus an empirical interval. Never `37.428 Wh`.

### Arrival scenarios

Anchored to measurement, not invented so the graph looks good. The pilot yields
an observed reference rate from real task durations and pacing. Scenarios are
defined relative to it:

- **0.25× observed** and **1× observed**, explicit what-ifs anchored to measured
  behaviour
- **saturation**, defined as continuous demand with no idle gap, rather than an
  arbitrary tasks-per-hour figure

---

## Still open

- The HWiNFO `CPU Package Power [W]` domain on Panther Lake. Nothing can be
  claimed until this is established, and it is a hardware check, not a code
  change.
- Whether the Panther Lake NPU throughput expectation in `docs/bring-up.md`
  survives contact with the machine.
- The dashboard. Deferred until real data exists, and it will read run manifests
  directly rather than carrying its own copy of any number.
- The frozen generation-1 dataset publication decision: stays private, or
  redacted at publication with the unredacted original retained and its hash
  recorded.

## Next step

The candidate lives on `measurement/energy-study`. Claude reviews the complete
branch package by recomputing identities, running the bad-input gates and
checking that code enforces this record. Real defects are fixed before merge.
After merge, the deterministic scripted baseline is the last methodological gate
before the first generation-2 model row.
