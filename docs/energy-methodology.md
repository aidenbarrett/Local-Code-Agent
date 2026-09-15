# Energy and device-efficiency methodology

This is a separate observational study from the core Local Code Agent capability experiment. It may reuse the same workloads and run manifests, but energy telemetry never changes whether a coding task is correct, compliant or verified. A task can be valid E1/E2/E3 evidence with unavailable power telemetry; it simply cannot contribute to an energy claim.

The reason this instrumentation lands before generation-2 row one is practical rather than statistical: the work has to be rerun if the observations are not captured when the workload executes. Adding the measurement path now avoids silently changing the instrument after the pilot has started.

## Claims and measurements

Report four device-level observations separately.

1. **Joules per completed task.** This is the primary task-energy metric. Do not substitute average watts. If a whole cell is wrapped, the completion sidecar records total joules and the explicit completed-task denominator used to derive joules per completed task.
2. **Idle energy / server-up idle power.** Measure a fixed idle window with the server loaded but no inference request active. Idle power is derived only for an `idle` role window. Do not subtract it from task energy unless a later protocol explicitly pre-registers that transformation.
3. **Battery drain on battery power.** Record mWh drain and percentage-point drain separately. Package/sensor energy and battery drain are different observations and must not be blended. Display, memory, radios and the rest of the system affect battery drain.
4. **Compile contention.** Measure wall time of the same real compile while the agent workload runs concurrently. This is a user-visible interference measure and does not require a power sensor.

Dashboarding is deferred until real data exists. The dashboard, if built, reads manifests and sidecars rather than copied numbers.

## Selected-sensor power

`measurement/energy.py` integrates an explicitly named HWiNFO CSV power sensor over the wrapped command window using trapezoidal integration. The stored scope remains `selected sensor only; no baseline subtraction` unless the caller supplies a more specific observed scope.

A sampled value is not automatically eligible for a device-efficiency claim. Every run records one of these domain states:

- `unqualified`: domain coverage has not been established;
- `confirmed_contains_target`: the selected sensor is known to include the target device/domain;
- `confirmed_excludes_target`: the selected sensor is known not to include the target device/domain;
- `proxy_only`: useful supporting telemetry, but not a basis for a target-device energy claim.

For Panther Lake, physically establish whether HWiNFO `CPU Package Power [W]` includes the NPU and iGPU domains before using that sensor for CPU/GPU/NPU efficiency claims. If NPU energy lies outside the sensor, label package energy as CPU-side proxy evidence and do not describe the NPU as cheaper because its power is missing from the numerator.

No Panther Lake energy value has been measured by this project yet.

## Battery protocol

Battery observations are supplied explicitly to the completion sidecar with their source. A battery-life claim requires an unplugged run and mWh observations; percentage points remain a separate descriptive figure. Use the same starting-charge policy when comparing devices/configurations. A charging sample or an end value above the start value fails closed rather than being coerced into a drain result.

Do not combine battery mWh with selected-sensor joules into one number. They answer different questions.

## Idle protocol

Keep the model server loaded and ready, then wrap a fixed-duration no-inference command with `measurement/energy.py --role idle`. Use the same duration and server state across devices. The sidecar may report derived idle average watts because idle power itself is the requested measurement. Task comparisons remain energy-first.

## Contention protocol

`measurement/contention.py` starts an explicit agent workload command and measures an explicit compile command while they overlap. Commands are passed as JSON arrays so argument boundaries are preserved on Linux and Windows. Report compile wall time, overlap duration and both exit codes. Compare the same compile workload and repository state across devices.

A run with zero overlap, a compile start failure, or an agent timeout is not valid contention evidence.

## Privacy and provenance

Energy and contention sidecars are immutable outputs. They do not overwrite the pre-run manifest. Linked artifacts are identified by filename and SHA256 rather than persisted absolute paths. Commands and free-form errors pass through the same persistence sanitizer used by the experiment evidence.

Changes to `measurement/*.py` move `source_sha256`. This methodology addition is source-only: generation remains 2, while `base_prompt_sha256` and `outcome_contract_sha256` must remain unchanged.
