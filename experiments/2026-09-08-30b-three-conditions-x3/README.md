# 2026-09-08, 30B, three conditions, three repeats

Nine cells: control, narrow and skill, each run three times, in a balanced
order so no condition sat in the same slot twice.

```
repeat 1   control, narrow, skill
repeat 2   narrow, skill, control
repeat 3   skill, control, narrow
```

Read [`findings.md`](findings.md) for the analysis.

## What is in `data/`

| file | what it is |
|---|---|
| `repeat-0N-posNN-<condition>.json` | one cell: the evaluator's dataset, ten case rows with full per-row provenance |
| `qualify.json` | the qualification gate, taken from repeat 1 cell 1 |
| `integrity-check.md` | the batch wrapper's own verdict, written before the data left the machine |
| `results.sqlite3` | selected metrics plus every case row, indexed by cell |
| `manifest.json`, `resume-manifest.json` | package and launcher hashes, verified at run time |

The qualification gate ran before every cell and passed every time, with the
same identity fields. One copy is kept here rather than nine near-identical
ones; the rest are in the batch archive on the run machine.

## Comparability

```
source_sha256       08d5e0fe6f2be291a0d62e14d664b4909932ee6041ecf5a78d4160af27a6641a
base_prompt_sha256  37f984092fed31eac321cf724946fcaffff590c1f624fb54777674e0c5cc4994
package_commit      c92a242b88c67bc0080018791df04fc9b69c0f3e
```

Identical to `2026-09-08-30b-three-conditions`, so the datasets are
instrument-compatible and may be analysed together as repeated observations.
They are repeated runs of the same ten fixtures, not independent tasks.
`findings.md` reports this dataset alone and gives combined descriptive figures
separately.

Two properties of any combined figure, recorded here so nobody has to rediscover
them:

- **The arms are unbalanced.** Combined it is 4 control cells, 4 narrow and 5
  skill, because the smoke ran an unplanned second skill cell.
- **The designs differ.** The smoke ran a fixed order, control then narrow then
  skill, in one sitting. This batch rotated the order every repeat. Combined,
  control is over-represented in early positions, and the server holds a prefix
  cache across a cell and was never restarted. That is a small systematic rather
  than a large one, and it is the reason combined numbers are described as
  descriptive rather than as an estimate of anything.

### The generation 2 control-leak defect postdates this data

`docs/experiment-design.md` records that generation 2 stopped the control
condition receiving `read_skill_reference`. Read on its own, that change list
makes the narrowing result here look contaminated. **It is not.** The defect
did not exist when this data was collected.

`read_skill_reference` arrived with the contracts layer in PR #1, which merged
after every row in this directory was written, and was fixed before any row was
collected under it. So it existed on `main` for a window in which no data was
taken.

Checked against the rows rather than against commit dates. Every control row in
both generation 1 datasets was offered exactly 20 tools, and the union of every
tool offered across all conditions and all rows is those same 20:

```
read_skill_reference in any offered toolset:  never
tools offered to control:                     20, on every control row
union of every tool ever offered:             20
```

No figure in this directory is inflated by that defect.

## Known issues in this data, recorded rather than corrected

Per the rules in [`../README.md`](../README.md), raw collected data under
`data/` is not edited after collection. Analysis documents may be corrected
without altering the collected evidence. Two things are wrong and are written
down instead.

**One proof-level verification disagreement**, `repeat-02/03-control`,
`link-error`. The runtime's `verified` flag survived an observed test failure
that the evaluator's oracle caught. The batch verdict is therefore FAIL. This
is a defect in the instrument, not in the data collection, it moves no headline
number, and the fix belongs to the next generation. `findings.md` has the
mechanism and the fix.

**One excluded row**, `repeat-02/02-skill`, `compile-error-locate`, returned
`INVALID_SERVER_UNAVAILABLE`. It is excluded from its denominator rather than
counted as a failure, which is what that validity type exists for. That cell's
denominator is 9, not 10.

**`navigation` failed 9/9** with the same claim mismatch in every condition and
every repeat. That is a case contract defect. The rows are kept as collected;
the case needs fixing or dropping before the pilot.
