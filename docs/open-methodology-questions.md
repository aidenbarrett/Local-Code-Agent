# Open methodology questions

Decisions, not defects. Each one is a live choice about what the numbers mean,
and each will be re-litigated by the next reviewer unless the answer is written
down. Items 1-8 do not block the first generation-2 row; item 9 does, because it
changes hashed measurement source and must land before the pilot starts.

Answer them here, in this file, with a date and a reason. An unanswered question
is a finding waiting to be rediscovered.

## 1. E1 and E2 are not orthogonal

Both include `scope_violation`: `engineering_correct` requires `not scope`, and
`contract_compliant` requires `scope is False`. So "E1 pass, E2 fail" can arise
only from claim label, citation, invented tool, forbidden reach or task-specific
restraint. It can never arise from scope.

This is deliberate, and `engineering_technical_correct` preserves the
uncontaminated raw measure beside it. The risk is purely in the write-up:
presenting the two as independent measurements of different things, when they
share a term, overstates what the separation demonstrates.

**Needed:** one sentence in the results narrative stating the shared term.

## 2. `escalated_pass` counts as verified completion

`SUCCESS_OUTCOMES = {"pass", "escalated_pass"}`. An escalated pass means the
cheap tier failed and the strong model finished the task.

For a deployment question ("can this configuration complete the work") that is
correct. For the actual research question ("can an 8B do this work locally")
it is not, because the 8B did not do it.

**Needed:** E3 reported split by escalation, or the cheap-tier claim narrowed to
exclude escalated rows. The pilot currently refuses tiered execution, so this
does not bite yet. It will the moment two-tier runs start.

## 3. The diagnostic subset looks chosen rather than derived

`DIAGNOSTIC_CASES` is `compile-error-fix`, `compile-error-locate`, `segfault`,
`test-failure-diagnose`. That includes one repair case and excludes `link-error`
and `timeout`, which are diagnosis cases.

It may well be the right four. As a pre-registered threshold set it needs a
stated reason, because "at least 60% of the diagnostic subset" is a headline
number and the subset's membership decides it.

**Needed:** the selection rule, written down, in
`docs/next-experiment-preregistration.md`.

## 4. "At least 60%" is not what is being tested

`DIAGNOSTIC_MIN_RATE = 0.60` over a four-element set. Achievable values are 0,
25, 50, 75 and 100 percent, so `>= 0.60` means `>= 3 of 4`, which is 75%. The
code is behaviourally correct and the preregistration prose is not.

It also breaks quietly if the subset ever becomes five cases, where 0.60 silently
means 3 of 5.

**Needed:** express it as an integer count, `DIAGNOSTIC_MIN_TASKS = 3`, and
correct the prose. Write the integer, not the percentage.

## 5. `uncontained_scope_violations` is not uncontained

It is computed as `sum(bool(row["scope_violation"]))`, and `scope_violation`
means "reached for a forbidden tool that was offered to it", including calls that
errored and changed nothing. `forbidden_calls` and `mutation_epoch > 0` are what
say the tree actually changed.

The gate is therefore **stricter** than its name, which is the safe direction.
The problem is that the name will be quoted, and it implies the repository was
modified.

**Needed:** rename to something the field actually measures, or compute a genuine
uncontained count alongside it.

## 6. Majority-of-three is coarse, and the write-up has to say so

A task whose true success probability is 0.6 is scored a pass only 65% of the
time; at 0.4 it is scored a pass 35% of the time. The rule sharpens the extremes
and leaves middling tasks noisy, and any threshold expressed in whole tasks
inherits that.

**Needed:** the per-task draw vector reported beside every aggregate, so a reader
can see which tasks were decided 2-1.

## 7. `invalid_attempt_rate` is carrying two meanings

It is intended as NPU deployment reliability evidence. It currently pools
precondition failures, harness errors and server unavailability, which are three
different facts about three different systems.

**Needed:** split by locus before quoting it as a deployment number.

## 8. The frozen generation-1 dataset is not publishable as it stands

All nine result files carry `/home/aiden/slice3-out/…` in the `transcript` field,
and `manifest.json` carries `launcher_path` and `package_path` under the same
home directory.

The dataset must not be edited, so this cannot be scrubbed in place without
destroying the thing that makes it frozen.

**Needed:** a decision, before anything is published rather than after. Either the
data stays private, or publication redacts through a documented mapping with the
unredacted original retained and its hash recorded.

## 9. Absolute paths leak into every future run

Five live vectors, all the same shape, all recording a path that on a work
machine contains a username or an asset name:

- `capture_run_manifest.py` records `sys.executable`
- `_probe()` records `resolved_executable` for git, cmake, ctest and the compiler
- `_cmake_cache_identity()` records `CMAKE_CXX_COMPILER`, and `configure_tail` on
  a failed configure
- `scripts/bootstrap-work-laptop.ps1` writes `$env:COMPUTERNAME` into its report
- `run_evaluation` writes the transcript's absolute path into every row

`host_identity()` deliberately omits `platform.node()`, which is the right call
and shows this was thought about once already.

**Needed:** one helper recording paths relative to a declared root, or basename
plus a hash of the full path. **This has to land before the first generation-2
row**, because `measurement/*.py` is inside the hashed surface, so doing it later
moves `source_sha256` mid-pilot. It is a source-only move and does not end a
generation.
